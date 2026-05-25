"""
Customer Group membership management.

Maintains MSuite Customer Group Memberships on Customer documents.
ERPNext Pricing Rules reference this child table via Python conditions.
"""
import frappe
from frappe.utils import today, add_to_date, now_datetime

from msuite.constants import (
    MSUITE_PARENT_CUSTOMER_GROUP,
    GRANT_RECONCILIATION_WINDOW_HOURS,
    GrantStatus,
    MSUITE_LOGGER_NAME,
    MSUITE_PRICING_RULE_PREFIX,
    PRICING_RULE_PRIORITY_COMPLIMENTARY,
    PRICING_RULE_PRIORITY_DISCOUNT,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)


def get_group_name_for_plan(plan_name: str) -> str:
    """
    Returns Customer Group name for a plan.

    Args:
        plan_name: MSuite Plan document name (the plan_name field)

    Returns:
        str: e.g. "MSuite - WhatsApp Business Active"
    """
    return f"MSuite - {plan_name} Active"


def ensure_group_exists(group_name: str) -> None:
    """
    Creates Customer Group if it does not exist. Idempotent.

    Args:
        group_name: Customer Group name to ensure
    """
    if not frappe.db.exists("Customer Group", MSUITE_PARENT_CUSTOMER_GROUP):
        parent = frappe.new_doc("Customer Group")
        parent.customer_group_name = MSUITE_PARENT_CUSTOMER_GROUP
        parent.parent_customer_group = "All Customer Groups"
        parent.insert(ignore_permissions=True)

    if not frappe.db.exists("Customer Group", group_name):
        doc = frappe.new_doc("Customer Group")
        doc.customer_group_name = group_name
        doc.parent_customer_group = MSUITE_PARENT_CUSTOMER_GROUP
        doc.insert(ignore_permissions=True)
        logger.info(f"Created Customer Group: {group_name}")


def add_customer_to_group(customer: str, group_name: str) -> None:
    """
    Adds group to customer's MSuite Customer Group Membership child table. Idempotent.

    Args:
        customer: ERPNext Customer name
        group_name: Customer Group name to add
    """
    ensure_group_exists(group_name)

    customer_doc = frappe.get_doc("Customer", customer)
    memberships = customer_doc.get("msuite_group_memberships") or []

    for row in memberships:
        if row.group_name == group_name:
            return  # Already exists

    customer_doc.append("msuite_group_memberships", {
        "group_name": group_name,
        "joined_on": today(),
    })
    customer_doc.save(ignore_permissions=True)
    logger.info(f"Added customer {customer} to group {group_name}")


def remove_customer_from_group(customer: str, group_name: str) -> None:
    """
    Removes group from customer's MSuite Customer Group Membership. Idempotent.

    Args:
        customer: ERPNext Customer name
        group_name: Customer Group name to remove
    """
    customer_doc = frappe.get_doc("Customer", customer)
    memberships = customer_doc.get("msuite_group_memberships") or []

    to_remove = [row for row in memberships if row.group_name == group_name]
    if not to_remove:
        return

    for row in to_remove:
        customer_doc.remove(row)

    customer_doc.save(ignore_permissions=True)
    logger.info(f"Removed customer {customer} from group {group_name}")


def get_customer_msuite_groups(customer: str) -> list[str]:
    """
    Returns list of MSuite group names customer currently belongs to.

    Args:
        customer: ERPNext Customer name

    Returns:
        List of group_name strings
    """
    customer_doc = frappe.get_doc("Customer", customer)
    memberships = customer_doc.get("msuite_group_memberships") or []
    return [row.group_name for row in memberships]


def sync_groups_for_subscription(subscription_name: str, action: str) -> None:
    """
    Adds or removes customer from Customer Groups for all plans in a subscription.

    Args:
        subscription_name: Subscription document name
        action: "add" | "remove"
    """
    from msuite.services.entitlement_service import get_msuite_plan_for_subscription_plan

    sub_doc = frappe.get_doc("Subscription", subscription_name)
    if sub_doc.party_type != "Customer":
        return

    customer = sub_doc.party

    from msuite.services.bundle_service import is_bundle_item, get_bundle_for_item, get_bundle_components

    for plan_row in sub_doc.plans:
        item = frappe.db.get_value("Subscription Plan", plan_row.plan, "item")

        # Check if this is a bundle item — if so, sync groups for each component
        if item and is_bundle_item(item):
            bundle_name = get_bundle_for_item(item)
            if bundle_name:
                components = get_bundle_components(bundle_name)
                for comp in components:
                    _sync_group_for_plan(customer, comp["plan"], subscription_name, action)
            continue

        msuite_plan = get_msuite_plan_for_subscription_plan(plan_row.plan)
        if not msuite_plan:
            continue
        _sync_group_for_plan(customer, msuite_plan, subscription_name, action)


def _sync_group_for_plan(
    customer: str,
    msuite_plan: str,
    subscription_name: str,
    action: str,
) -> None:
    """Syncs customer group membership for a single MSuite Plan + its complimentary grants."""
    plan_name = frappe.db.get_value("MSuite Plan", msuite_plan, "plan_name")
    if not plan_name:
        return

    group_name = get_group_name_for_plan(plan_name)

    if action == "add":
        add_customer_to_group(customer, group_name)
    elif action == "remove":
        other_active = frappe.db.exists(
            "MSuite Customer Grant",
            {
                "customer": customer,
                "granted_plan": msuite_plan,
                "source_subscription": ["!=", subscription_name],
                "status": GrantStatus.ACTIVE,
            },
        )
        if not other_active:
            remove_customer_from_group(customer, group_name)

    # Also sync for complimentary grants triggered by this plan — but only if the
    # grant actually exists (it may have been skipped due to higher-tier presence)
    plan_doc = frappe.get_doc("MSuite Plan", msuite_plan)
    for grant_rule in plan_doc.grants:
        granted_plan_name = frappe.db.get_value(
            "MSuite Plan", grant_rule.granted_plan, "plan_name"
        )
        if granted_plan_name:
            comp_group = get_group_name_for_plan(granted_plan_name)
            if action == "add":
                # Only add to group if the complimentary grant was actually created
                grant_exists = frappe.db.exists(
                    "MSuite Customer Grant",
                    {
                        "customer": customer,
                        "granted_plan": grant_rule.granted_plan,
                        "status": GrantStatus.ACTIVE,
                    },
                )
                if grant_exists:
                    add_customer_to_group(customer, comp_group)
            elif action == "remove":
                other_comp = frappe.db.exists(
                    "MSuite Customer Grant",
                    {
                        "customer": customer,
                        "granted_plan": grant_rule.granted_plan,
                        "source_subscription": ["!=", subscription_name],
                        "status": GrantStatus.ACTIVE,
                    },
                )
                if not other_comp:
                    remove_customer_from_group(customer, comp_group)


def reconcile_all_groups() -> dict:
    """
    Daily reconciliation for recently changed grants.

    Returns:
        Summary dict with customers_checked, additions, removals, errors
    """
    cutoff = add_to_date(now_datetime(), hours=-GRANT_RECONCILIATION_WINDOW_HOURS)

    recent_grants = frappe.get_list(
        "MSuite Customer Grant",
        filters={"modified": [">=", cutoff]},
        fields=["customer"],
        group_by="customer",
    )

    summary = {"customers_checked": 0, "additions": 0, "removals": 0, "errors": []}

    # Batch-load all plan_name values for the reconciliation
    all_customers = [r.customer for r in recent_grants]
    all_active_grants = frappe.get_list(
        "MSuite Customer Grant",
        filters={"customer": ["in", all_customers], "status": GrantStatus.ACTIVE},
        fields=["customer", "granted_plan"],
    ) if all_customers else []
    all_plan_names_needed = list({g.granted_plan for g in all_active_grants})
    plan_name_map: dict[str, str] = {}
    if all_plan_names_needed:
        for p in frappe.get_list("MSuite Plan", filters={"name": ["in", all_plan_names_needed]}, fields=["name", "plan_name"]):
            plan_name_map[p.name] = p.plan_name

    for row in recent_grants:
        try:
            customer = row.customer
            summary["customers_checked"] += 1

            customer_grants = [g for g in all_active_grants if g.customer == customer]

            expected_groups = set()
            for grant in customer_grants:
                plan_name = plan_name_map.get(grant.granted_plan)
                if plan_name:
                    expected_groups.add(get_group_name_for_plan(plan_name))

            current_groups = set(get_customer_msuite_groups(customer))

            # Add missing
            for group in expected_groups - current_groups:
                add_customer_to_group(customer, group)
                summary["additions"] += 1

            # Remove stale
            msuite_groups = {g for g in current_groups if g.startswith(f"{MSUITE_PARENT_CUSTOMER_GROUP} -")}
            for group in msuite_groups - expected_groups:
                remove_customer_from_group(customer, group)
                summary["removals"] += 1

        except Exception as e:
            summary["errors"].append(f"Customer {row.customer}: {str(e)}")
            frappe.log_error(
                frappe.get_traceback(),
                f"MSuite: Group reconciliation error for {row.customer}",
            )

    logger.info(
        f"Group reconciliation complete: {summary['customers_checked']} checked, "
        f"{summary['additions']} added, {summary['removals']} removed"
    )
    return summary


# ======================================================================
# Pricing Rule management for grant rules
# ======================================================================


def _make_pricing_rule_title(trigger_plan_code: str, granted_plan_code: str, grant_type: str) -> str:
    """
    Builds a deterministic, human-readable Pricing Rule title.

    Example: "MSuite - WA-BIZ → SOCIAL-BASIC (Complimentary)"
    """
    return f"{MSUITE_PRICING_RULE_PREFIX} - {trigger_plan_code} → {granted_plan_code} ({grant_type})"


def sync_pricing_rule_for_grant_rule(
    trigger_plan_name: str,
    granted_plan_name: str,
    grant_type: str,
    discount_pct: float,
) -> str | None:
    """
    Creates an ERPNext Pricing Rule for a plan's complimentary/discount grant rule.
    Idempotent — skips if the rule already exists.

    Args:
        trigger_plan_name: MSuite Plan name that triggers the grant (e.g., "WA-BIZ")
        granted_plan_name: MSuite Plan name being granted (e.g., "SOCIAL-BASIC")
        grant_type: "Complimentary" or "Discount"
        discount_pct: Discount percentage (100 for complimentary)

    Returns:
        Pricing Rule name if created, None if already exists or skipped
    """
    # Resolve plan codes and items
    trigger_data = frappe.db.get_value(
        "MSuite Plan", trigger_plan_name, ["plan_code", "plan_name"], as_dict=True
    )
    granted_data = frappe.db.get_value(
        "MSuite Plan", granted_plan_name, ["plan_code", "plan_name", "item"], as_dict=True
    )
    if not trigger_data or not granted_data:
        return None

    # Find the granted plan's item code (from pricing table or legacy field)
    granted_item = granted_data.item
    if not granted_item:
        granted_item = frappe.db.get_value(
            "MSuite Plan Pricing", {"parent": granted_plan_name}, "item"
        )
    if not granted_item:
        logger.warning(
            f"Cannot create Pricing Rule: granted plan {granted_plan_name} has no Item"
        )
        return None

    title = _make_pricing_rule_title(trigger_data.plan_code, granted_data.plan_code, grant_type)

    # Idempotent: skip if already exists
    existing = frappe.db.get_value("Pricing Rule", {"title": title}, "name")
    if existing:
        return existing

    # Ensure the Customer Group exists before creating the Pricing Rule
    group_name = get_group_name_for_plan(trigger_data.plan_name)
    ensure_group_exists(group_name)

    # Determine priority
    priority = (
        PRICING_RULE_PRIORITY_COMPLIMENTARY
        if discount_pct == 100
        else PRICING_RULE_PRIORITY_DISCOUNT
    )

    # Create the Pricing Rule
    pr = frappe.new_doc("Pricing Rule")
    pr.title = title
    pr.apply_on = "Item Code"
    pr.applicable_for = "Customer Group"
    pr.customer_group = group_name
    pr.price_or_product_discount = "Price"
    pr.rate_or_discount = "Discount Percentage"
    pr.discount_percentage = discount_pct
    pr.selling = 1
    pr.priority = str(priority)
    pr.append("items", {"item_code": granted_item})
    pr.insert(ignore_permissions=True)

    logger.info(f"Created Pricing Rule '{title}' ({discount_pct}% off {granted_item})")
    return pr.name


def delete_pricing_rule_for_grant_rule(
    trigger_plan_name: str,
    granted_plan_name: str,
    grant_type: str,
) -> None:
    """
    Deletes (or disables) the Pricing Rule for a removed grant rule.

    If the Pricing Rule has been applied to historical invoices,
    ERPNext will block deletion — in that case, disable it instead.

    Args:
        trigger_plan_name: MSuite Plan name that triggers the grant
        granted_plan_name: MSuite Plan name being granted
        grant_type: "Complimentary" or "Discount"
    """
    trigger_code = frappe.db.get_value("MSuite Plan", trigger_plan_name, "plan_code")
    granted_code = frappe.db.get_value("MSuite Plan", granted_plan_name, "plan_code")
    if not trigger_code or not granted_code:
        return

    title = _make_pricing_rule_title(trigger_code, granted_code, grant_type)
    pr_name = frappe.db.get_value("Pricing Rule", {"title": title}, "name")
    if not pr_name:
        return

    try:
        frappe.delete_doc("Pricing Rule", pr_name, ignore_permissions=True, force=True)
        logger.info(f"Deleted Pricing Rule '{title}'")
    except Exception:
        # Cannot delete (referenced by invoices) — disable instead
        frappe.db.set_value("Pricing Rule", pr_name, "disable", 1)
        logger.info(f"Disabled Pricing Rule '{title}' (cannot delete, has references)")
