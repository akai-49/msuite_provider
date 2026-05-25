"""
Grant lifecycle service.

All public methods are idempotent.
All mutations invalidate entitlement cache.
Multi-document operations use frappe.db.savepoint for atomicity.
"""
import frappe
from frappe.utils import today, add_days, getdate

from msuite.constants import (
    GrantType,
    GrantStatus,
    PlanTier,
    MSUITE_LOGGER_NAME,
)
from msuite.exceptions import (
    PlanNotFoundError,
    InvalidGrantStateError,
    TrialConfigurationError,
)
from msuite.utils.cache import invalidate_entitlement_cache

logger = frappe.logger(MSUITE_LOGGER_NAME)


def apply_grants_for_subscription(subscription_name: str) -> list[str]:
    """
    Called on Subscription submit for paid (non-trial) subscriptions.

    Args:
        subscription_name: ERPNext Subscription name

    Returns:
        List of created MSuite Customer Grant names

    Raises:
        PlanNotFoundError: if Item has no MSuite Plan and is not a bundle
    """
    sub_doc = frappe.get_doc("Subscription", subscription_name)
    if sub_doc.party_type != "Customer":
        return []

    customer = sub_doc.party
    created_grants: list[str] = []

    from msuite.services.entitlement_service import get_msuite_plan_for_subscription_plan
    from msuite.services.bundle_service import is_bundle_item, get_bundle_for_item

    for plan_row in sub_doc.plans:
        item = frappe.db.get_value("Subscription Plan", plan_row.plan, "item")
        if not item:
            continue

        if is_bundle_item(item):
            bundle_name = get_bundle_for_item(item)
            if bundle_name:
                grants = _apply_bundle_grants(customer, bundle_name, subscription_name)
                created_grants.extend(grants)
        else:
            msuite_plan = get_msuite_plan_for_subscription_plan(plan_row.plan)
            if not msuite_plan:
                logger.warning(f"No MSuite Plan found for Subscription Plan {plan_row.plan}")
                continue

            grant_name = _apply_single_plan_grant(
                customer, msuite_plan, subscription_name, GrantType.PAID
            )
            if grant_name:
                created_grants.append(grant_name)

            comp_grants = _apply_complimentary_grants(customer, msuite_plan, subscription_name)
            created_grants.extend(comp_grants)

    from msuite.services.customer_group_service import sync_groups_for_subscription
    sync_groups_for_subscription(subscription_name, "add")
    invalidate_entitlement_cache(customer)

    logger.info(f"Applied {len(created_grants)} grants for subscription {subscription_name}")
    return created_grants


def apply_trial_grants_for_subscription(subscription_name: str) -> list[str]:
    """
    Called on Subscription submit when trial period is active.

    Args:
        subscription_name: ERPNext Subscription name

    Returns:
        List of created grant names

    Raises:
        TrialConfigurationError: if no Trial plan configured for product
    """
    sub_doc = frappe.get_doc("Subscription", subscription_name)
    if sub_doc.party_type != "Customer":
        return []

    customer = sub_doc.party
    created_grants: list[str] = []

    from msuite.services.entitlement_service import get_msuite_plan_for_subscription_plan
    from msuite.services.trial_service import get_trial_plan_for_product

    for plan_row in sub_doc.plans:
        msuite_plan = get_msuite_plan_for_subscription_plan(plan_row.plan)
        if not msuite_plan:
            continue

        product = frappe.db.get_value("MSuite Plan", msuite_plan, "product")
        trial_plan = get_trial_plan_for_product(product)

        if not trial_plan:
            frappe.throw(
                f"No Trial plan configured for product {product}",
                TrialConfigurationError,
            )

        expires_on = str(sub_doc.trial_period_end) if sub_doc.trial_period_end else None

        grant_name = _apply_single_plan_grant(
            customer, trial_plan, subscription_name, GrantType.TRIAL,
            expires_on=expires_on,
        )
        if grant_name:
            created_grants.append(grant_name)

    invalidate_entitlement_cache(customer)
    logger.info(f"Applied {len(created_grants)} trial grants for subscription {subscription_name}")
    return created_grants


def revoke_grants_for_subscription(
    subscription_name: str,
    reason: str = "Subscription cancelled",
) -> list[str]:
    """
    Called on Subscription cancellation.

    Args:
        subscription_name: ERPNext Subscription name
        reason: Human-readable revocation reason

    Returns:
        List of revoked MSuite Customer Grant names
    """
    grants = frappe.get_list(
        "MSuite Customer Grant",
        filters={"source_subscription": subscription_name, "status": GrantStatus.ACTIVE},
        fields=["name", "customer", "granted_plan"],
    )

    revoked: list[str] = []
    customer = None

    for grant in grants:
        customer = grant.customer
        if _customer_has_other_entitlement(grant.customer, grant.granted_plan, subscription_name):
            logger.info(
                f"Skipping revocation of {grant.name}: customer has other entitlement"
            )
            continue
        revoke_grant(grant.name, reason)
        revoked.append(grant.name)

    if customer:
        from msuite.services.customer_group_service import sync_groups_for_subscription
        sync_groups_for_subscription(subscription_name, "remove")
        invalidate_entitlement_cache(customer)

    logger.info(f"Revoked {len(revoked)} grants for subscription {subscription_name}")
    return revoked


def apply_grants_after_amendment(
    old_subscription_name: str,
    new_subscription_name: str,
) -> list[str]:
    """
    Called after Subscription amendment (upgrade/downgrade).

    Args:
        old_subscription_name: The original Subscription name
        new_subscription_name: The amended Subscription name

    Returns:
        List of newly created grant names
    """
    revoke_grants_for_subscription(old_subscription_name, reason="Subscription amended")
    return apply_grants_for_subscription(new_subscription_name)


def expire_stale_trial_grants() -> list[str]:
    """
    Called by daily scheduler.

    Returns:
        List of expired/revoked grant names
    """
    expired: list[str] = []
    grants = frappe.get_list(
        "MSuite Customer Grant",
        filters={
            "grant_type": GrantType.TRIAL,
            "status": GrantStatus.ACTIVE,
            "expires_on": ["<=", today()],
        },
        fields=["name", "source_subscription", "customer"],
    )

    from msuite.services.trial_service import transition_trial_to_paid

    for grant in grants:
        try:
            sub_status = frappe.db.get_value("Subscription", grant.source_subscription, "status")
            if sub_status in ("Active", "Trialing"):
                transition_trial_to_paid(grant.source_subscription)
            else:
                revoke_grant(grant.name, "Trial expired, subscription inactive")
            expired.append(grant.name)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"MSuite: Error expiring trial grant {grant.name}",
            )

    return expired


def revoke_grant(grant_name: str, reason: str) -> None:
    """
    Revokes a single Customer Grant.

    Args:
        grant_name: MSuite Customer Grant name
        reason: Revocation reason string

    Raises:
        InvalidGrantStateError: if grant status is not Active
    """
    grant_doc = frappe.get_doc("MSuite Customer Grant", grant_name)

    if grant_doc.status != GrantStatus.ACTIVE:
        frappe.throw(
            f"Cannot revoke grant {grant_name}: status is {grant_doc.status}, expected Active",
            InvalidGrantStateError,
        )

    grant_doc.status = GrantStatus.REVOKED
    grant_doc.revoked_on = today()
    grant_doc.revocation_reason = reason
    grant_doc.save(ignore_permissions=True)

    invalidate_entitlement_cache(grant_doc.customer)
    logger.info(f"Revoked grant {grant_name}: {reason}")


def get_active_grants_for_customer(customer: str) -> list[dict]:
    """
    Returns all active grants for a customer with plan details.

    Returns:
        List of grant dicts with plan details
    """
    grants = frappe.get_list(
        "MSuite Customer Grant",
        filters={"customer": customer, "status": GrantStatus.ACTIVE},
        fields=["name", "granted_plan", "grant_type", "trigger_plan", "granted_on", "expires_on"],
    )

    result: list[dict] = []
    for grant in grants:
        plan_data = frappe.db.get_value(
            "MSuite Plan", grant.granted_plan, ["product", "tier"], as_dict=True
        )
        result.append({
            "grant_name": grant.name,
            "granted_plan": grant.granted_plan,
            "grant_type": grant.grant_type,
            "trigger_plan": grant.trigger_plan,
            "granted_on": str(grant.granted_on) if grant.granted_on else None,
            "expires_on": str(grant.expires_on) if grant.expires_on else None,
            "product": plan_data.product if plan_data else None,
            "tier": plan_data.tier if plan_data else None,
        })

    return result


def _apply_single_plan_grant(
    customer: str,
    plan_name: str,
    subscription_name: str,
    grant_type: str,
    trigger_plan: str | None = None,
    expires_on: str | None = None,
) -> str | None:
    # Creates one MSuite Customer Grant (idempotent)
    if _grant_exists(customer, plan_name, subscription_name):
        logger.warning(
            f"Grant already exists for customer={customer}, plan={plan_name}, sub={subscription_name}"
        )
        existing = frappe.db.get_value(
            "MSuite Customer Grant",
            {"customer": customer, "granted_plan": plan_name, "source_subscription": subscription_name, "status": GrantStatus.ACTIVE},
            "name",
        )
        return existing

    savepoint = "msuite_grant_create"
    frappe.db.savepoint(savepoint)
    try:
        doc = frappe.new_doc("MSuite Customer Grant")
        doc.customer = customer
        doc.granted_plan = plan_name
        doc.source_subscription = subscription_name
        doc.grant_type = grant_type
        doc.trigger_plan = trigger_plan
        doc.status = GrantStatus.ACTIVE
        doc.granted_on = today()
        doc.expires_on = expires_on
        doc.insert(ignore_permissions=True)
        logger.info(f"Created grant {doc.name} for customer={customer}, plan={plan_name}")
        return doc.name
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        frappe.log_error(frappe.get_traceback(), f"MSuite: Failed to create grant for {customer}")
        return None


def _apply_bundle_grants(
    customer: str,
    bundle_name: str,
    subscription_name: str,
) -> list[str]:
    # Creates one Bundle grant per component plan, then fires complimentary
    # grant rules for each component plan
    from msuite.services.bundle_service import get_bundle_components

    components = get_bundle_components(bundle_name)
    created: list[str] = []

    # First pass: create Bundle grants for each component
    for comp in components:
        grant_name = _apply_single_plan_grant(
            customer, comp["plan"], subscription_name, GrantType.BUNDLE
        )
        if grant_name:
            created.append(grant_name)

    # Second pass: fire complimentary grant rules for each component plan
    for comp in components:
        comp_grants = _apply_complimentary_grants(customer, comp["plan"], subscription_name)
        created.extend(comp_grants)

    return created


def _apply_complimentary_grants(
    customer: str,
    plan_name: str,
    subscription_name: str,
) -> list[str]:
    # Reads MSuite Plan Grant child table and applies complimentary grants.
    # Skips if customer already has a higher or equal tier of the same product
    # via another active grant (avoids redundant lower-tier grants).
    plan_doc = frappe.get_doc("MSuite Plan", plan_name)
    created: list[str] = []

    if not plan_doc.grants:
        return created

    tier_order = {
        PlanTier.TRIAL: 0,
        PlanTier.BASIC: 1,
        PlanTier.PRO: 2,
        PlanTier.BUSINESS: 3,
        PlanTier.ENTERPRISE: 4,
    }

    # Batch-load: all active grants for this customer (once, not per loop iteration)
    existing_grants = frappe.get_list(
        "MSuite Customer Grant",
        filters={"customer": customer, "status": GrantStatus.ACTIVE},
        fields=["granted_plan"],
    )
    # Batch-load: product+tier for all existing grant plans + all grant rule targets
    all_plan_names = [eg.granted_plan for eg in existing_grants]
    all_plan_names.extend([row.granted_plan for row in plan_doc.grants])
    plan_data_map: dict = {}
    if all_plan_names:
        for pd in frappe.get_list(
            "MSuite Plan",
            filters={"name": ["in", list(set(all_plan_names))]},
            fields=["name", "product", "tier"],
        ):
            plan_data_map[pd.name] = pd

    for row in plan_doc.grants:
        granted_plan_data = plan_data_map.get(row.granted_plan)
        if granted_plan_data:
            granted_tier_rank = tier_order.get(granted_plan_data.tier, 0)
            skip = False
            for eg in existing_grants:
                eg_data = plan_data_map.get(eg.granted_plan)
                if (
                    eg_data
                    and eg_data.product == granted_plan_data.product
                    and tier_order.get(eg_data.tier, 0) >= granted_tier_rank
                ):
                    logger.info(
                        f"Skipping complimentary grant {row.granted_plan} for {customer}: "
                        f"already has {eg.granted_plan} ({eg_data.tier}) which is >= "
                        f"{granted_plan_data.tier} for product {granted_plan_data.product}"
                    )
                    skip = True
                    break
            if skip:
                continue

        expires_on = None
        if row.expires_after_days and row.expires_after_days > 0:
            expires_on = str(add_days(today(), row.expires_after_days))

        grant_name = _apply_single_plan_grant(
            customer,
            row.granted_plan,
            subscription_name,
            GrantType.COMPLIMENTARY,
            trigger_plan=plan_name,
            expires_on=expires_on,
        )
        if grant_name:
            created.append(grant_name)

    return created


def _grant_exists(
    customer: str,
    granted_plan: str,
    source_subscription: str,
) -> bool:
    # Returns True if active grant exists for (customer, granted_plan, source_subscription)
    return bool(frappe.db.exists(
        "MSuite Customer Grant",
        {
            "customer": customer,
            "granted_plan": granted_plan,
            "source_subscription": source_subscription,
            "status": GrantStatus.ACTIVE,
        },
    ))


def _customer_has_other_entitlement(
    customer: str,
    plan_name: str,
    exclude_subscription: str,
) -> bool:
    # Returns True if customer has another active entitlement to plan_name
    other_grants = frappe.db.exists(
        "MSuite Customer Grant",
        {
            "customer": customer,
            "granted_plan": plan_name,
            "source_subscription": ["!=", exclude_subscription],
            "status": GrantStatus.ACTIVE,
        },
    )
    return bool(other_grants)
