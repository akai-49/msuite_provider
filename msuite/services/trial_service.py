"""Trial lifecycle service."""
import frappe
from frappe.utils import getdate, today

from msuite.constants import (
    PlanTier,
    GrantType,
    GrantStatus,
    MSUITE_LOGGER_NAME,
)
from msuite.utils.cache import (
    get_trial_plan_cache,
    set_trial_plan_cache,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)


def get_trial_plan_for_product(product_name: str) -> str | None:
    """
    Returns MSuite Plan name with tier=Trial for given product.

    Args:
        product_name: MSuite Product document name

    Returns:
        MSuite Plan name or None
    """
    cached = get_trial_plan_cache(product_name)
    if cached:
        return cached

    plan_name = frappe.db.get_value(
        "MSuite Plan",
        {"product": product_name, "tier": PlanTier.TRIAL, "is_active": 1},
        "name",
    )

    if plan_name:
        set_trial_plan_cache(product_name, plan_name)

    return plan_name


def is_subscription_in_trial(subscription_name: str) -> bool:
    """
    Returns True if subscription is currently in trial period.

    ERPNext does not have a 'Trialing' status. During trial, status is 'Active'
    and ERPNext simply does not generate invoices because trial_period_end is
    in the future. Trial detection is purely date-based.

    Args:
        subscription_name: ERPNext Subscription name

    Returns:
        bool
    """
    sub_data = frappe.db.get_value(
        "Subscription",
        subscription_name,
        ["trial_period_end", "docstatus"],
        as_dict=True,
    )

    if not sub_data or not sub_data.trial_period_end:
        return False

    # ERPNext v16: Subscription is not submittable (docstatus stays 0)
    # Check status instead of docstatus
    if sub_data.docstatus not in (0, 1):
        return False

    return getdate(today()) <= getdate(sub_data.trial_period_end)


def transition_trial_to_paid(subscription_name: str) -> None:
    """
    Called when trial expires and Subscription remains Active.

    Args:
        subscription_name: ERPNext Subscription name
    """
    from msuite.services.grant_service import revoke_grant, apply_grants_for_subscription

    # Find and revoke all trial grants for this subscription
    trial_grants = frappe.get_list(
        "MSuite Customer Grant",
        filters={
            "source_subscription": subscription_name,
            "grant_type": GrantType.TRIAL,
            "status": GrantStatus.ACTIVE,
        },
        fields=["name"],
    )

    for grant in trial_grants:
        revoke_grant(grant.name, "Trial period ended")

    # Apply paid grants
    apply_grants_for_subscription(subscription_name)

    logger.info(f"Transitioned subscription {subscription_name} from trial to paid")


def get_trial_expiry(subscription_name: str) -> str | None:
    """
    Returns trial_period_end as ISO date string, or None if no trial.

    Args:
        subscription_name: ERPNext Subscription name

    Returns:
        Date string "YYYY-MM-DD" or None
    """
    trial_end = frappe.db.get_value("Subscription", subscription_name, "trial_period_end")
    return str(trial_end) if trial_end else None
