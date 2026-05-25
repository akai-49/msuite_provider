"""
Subscription event hooks.

ERPNext v16 Subscription is NOT submittable (is_submittable=0).
Status transitions are: Active, Trialing, Past Due Date, Cancelled, Completed.
Grants are applied on after_insert (when status becomes Active/Trialing).
Grants are revoked on on_update when status changes to Cancelled.

All hooks wrapped in try/except. Failures logged but never re-raised.
"""
import frappe
from frappe.utils import getdate, today

from msuite.constants import GrantStatus, GrantType, MSUITE_LOGGER_NAME

logger = frappe.logger(MSUITE_LOGGER_NAME)


def on_subscription_created(doc, method):
    """
    Fires on Subscription after_insert.
    Applies grants when a new subscription is created (status=Active or Trialing).
    """
    try:
        if doc.party_type != "Customer":
            return
        if not _is_msuite_managed_subscription(doc):
            return

        from msuite.services.trial_service import is_subscription_in_trial
        from msuite.services.grant_service import (
            apply_trial_grants_for_subscription,
            apply_grants_for_subscription,
        )
        from msuite.services.customer_group_service import sync_groups_for_subscription
        from msuite.utils.cache import invalidate_entitlement_cache

        if is_subscription_in_trial(doc.name):
            grants = apply_trial_grants_for_subscription(doc.name)
            logger.info(f"Applied {len(grants)} trial grants for {doc.name}")
        else:
            grants = apply_grants_for_subscription(doc.name)
            logger.info(f"Applied {len(grants)} paid grants for {doc.name}")

        sync_groups_for_subscription(doc.name, "add")
        invalidate_entitlement_cache(doc.party)

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"MSuite: on_subscription_created error for {doc.name}",
        )


def on_subscription_update(doc, method):
    """
    Fires on Subscription on_update.
    Detects:
      1. Status change to Cancelled → revoke grants
      2. Trial-to-paid transition (trial_period_end passed, still Active)
    """
    try:
        if doc.party_type != "Customer":
            return
        if not _is_msuite_managed_subscription(doc):
            return

        # Detect cancellation: status changed to Cancelled
        old_status = doc.get_doc_before_save()
        if old_status:
            old_status = old_status.status
        else:
            old_status = None

        if doc.status == "Cancelled" and old_status != "Cancelled":
            _handle_cancellation(doc)
            return

        # Detect trial-to-paid transition
        if (
            doc.trial_period_end
            and getdate(today()) > getdate(doc.trial_period_end)
            and doc.status == "Active"
            and _has_active_trial_grants(doc.name)
        ):
            from msuite.services.trial_service import transition_trial_to_paid
            transition_trial_to_paid(doc.name)
            logger.info(f"Trial-to-paid transition for {doc.name}")

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"MSuite: on_subscription_update error for {doc.name}",
        )


def _handle_cancellation(doc):
    """Revoke grants and remove customer groups on subscription cancellation."""
    from msuite.services.grant_service import revoke_grants_for_subscription
    from msuite.services.customer_group_service import sync_groups_for_subscription
    from msuite.utils.cache import invalidate_entitlement_cache

    revoked = revoke_grants_for_subscription(doc.name)
    logger.info(f"Revoked {len(revoked)} grants for cancelled subscription {doc.name}")

    sync_groups_for_subscription(doc.name, "remove")
    invalidate_entitlement_cache(doc.party)


def _is_msuite_managed_subscription(doc) -> bool:
    """Returns True if at least one plan resolves to an MSuite Plan or bundle item."""
    from msuite.services.entitlement_service import get_msuite_plan_for_subscription_plan
    from msuite.services.bundle_service import is_bundle_item

    for plan_row in doc.plans:
        if get_msuite_plan_for_subscription_plan(plan_row.plan):
            return True
        item = frappe.db.get_value("Subscription Plan", plan_row.plan, "item")
        if item and is_bundle_item(item):
            return True
    return False


def _has_active_trial_grants(subscription_name: str) -> bool:
    """Returns True if Active Trial grants exist for this subscription."""
    return bool(frappe.db.exists(
        "MSuite Customer Grant",
        {
            "source_subscription": subscription_name,
            "grant_type": GrantType.TRIAL,
            "status": GrantStatus.ACTIVE,
        },
    ))
