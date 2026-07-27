"""
Plan activation — ERPNext billing artifact creation + lifecycle actions.

Handles:
  - Activation readiness validation
  - ERPNext Item + Subscription Plan creation for each pricing row
  - Rate syncing to existing Items/Subscription Plans
  - Pricing Rule sync for grant rules
  - Delete protection (active grants, subscriptions, invoices)
  - Cascade delete of orphaned billing artifacts
  - Whitelisted actions: activate, deactivate, reactivate
"""

import frappe
from frappe.utils import now

from msuite.constants import PlanTier
from msuite.exceptions import MSuiteError
from msuite.utils.validators import require_system_manager_or_msuite_manager

# Billing interval → item code suffix
INTERVAL_SUFFIX = {"Monthly": "MONTHLY", "Quarterly": "QUARTERLY", "Annual": "ANNUAL"}

# Billing interval → ERPNext Subscription Plan fields
INTERVAL_TO_ERPNEXT = {
    "Monthly": ("Month", 1),
    "Quarterly": ("Month", 3),
    "Annual": ("Year", 1),
}


# ── Activation readiness ─────────────────────────────────────────────────


def validate_activation_readiness(doc):
    """
    Validates that the plan is fully configured and ready for activation.
    Called by activate_plan before creating Items.
    """
    if doc.is_active:
        frappe.throw("Plan is already active", frappe.ValidationError)

    if not doc.plan_code or not doc.product:
        frappe.throw(
            "Plan Code and Product are required before activation",
            frappe.ValidationError,
        )

    # Tier uniqueness: only one active plan per product+tier
    existing = frappe.db.get_value(
        "MSuite Plan",
        {"product": doc.product, "tier": doc.tier, "is_active": 1, "name": ["!=", doc.name]},
        "name",
    )
    if existing:
        frappe.throw(
            f"Only one {doc.tier} plan allowed per product. Active plan: {existing}",
            frappe.ValidationError,
        )

    is_trial = doc.tier == PlanTier.TRIAL

    if not is_trial and not doc.pricing:
        frappe.throw(
            "At least one pricing row is required before activation. "
            "Trial-tier plans are exempt.",
            frappe.ValidationError,
        )

    if not doc.features:
        frappe.throw(
            "At least one feature is required before activation",
            frappe.ValidationError,
        )


# ── Item + Subscription Plan creation ────────────────────────────────────


def create_items_and_subscription_plans(doc):
    """
    Creates ERPNext Item + Subscription Plan for each pricing row.
    Called once during activation. Idempotent — skips rows that
    already have Items. Trial-tier plans skip entirely (no billing).
    """
    if doc.tier == PlanTier.TRIAL:
        return

    if not doc.plan_code or not doc.product:
        return

    product_name = (
        frappe.db.get_value("MSuite Product", doc.product, "product_name")
        or "All Item Groups"
    )
    item_group = (
        product_name
        if frappe.db.exists("Item Group", product_name)
        else "All Item Groups"
    )
    created = []

    for row in doc.pricing or []:
        suffix = INTERVAL_SUFFIX.get(row.billing_interval, "MONTHLY")
        item_code = f"{doc.plan_code}-{suffix}"
        rate = float(row.rate or 0)

        # Skip if already created (idempotent)
        if row.item and frappe.db.exists("Item", row.item):
            sync_rate(row.item, rate)
            continue

        # Create Item
        if not frappe.db.exists("Item", item_code):
            item = frappe.new_doc("Item")
            item.item_code = item_code
            item.item_name = f"{doc.plan_name} ({row.billing_interval})"
            item.item_group = item_group
            item.stock_uom = "Nos"
            item.is_stock_item = 0
            item.include_item_in_manufacturing = 0
            item.standard_rate = rate
            item.insert(ignore_permissions=True)

        # Create Subscription Plan
        if not frappe.db.exists("Subscription Plan", item_code):
            interval, count = INTERVAL_TO_ERPNEXT.get(
                row.billing_interval, ("Month", 1)
            )
            sp = frappe.new_doc("Subscription Plan")
            sp.plan_name = item_code
            sp.item = item_code
            sp.price_determination = "Fixed Rate"
            sp.cost = rate
            sp.billing_interval = interval
            sp.billing_interval_count = count
            sp.insert(ignore_permissions=True)

        # Write back to the pricing row
        row.item = item_code
        row.subscription_plan = item_code
        created.append(f"{item_code} (₹{rate}/{row.billing_interval})")

    if created:
        frappe.msgprint(
            f"Created: {', '.join(created)}",
            alert=True,
            indicator="green",
        )


# ── Rate syncing ─────────────────────────────────────────────────────────


def sync_existing_rates(doc):
    """Sync pricing rates to existing Items and Subscription Plans."""
    for row in doc.pricing or []:
        if row.item and frappe.db.exists("Item", row.item):
            sync_rate(row.item, float(row.rate or 0))


def sync_rate(item_code, rate):
    """Update Item standard_rate and Subscription Plan cost if changed."""
    sp_name = frappe.db.get_value("Subscription Plan", {"item": item_code}, "name")
    if sp_name:
        current = frappe.db.get_value("Subscription Plan", sp_name, "cost")
        if float(current or 0) != rate:
            frappe.db.set_value("Subscription Plan", sp_name, "cost", rate)
            frappe.db.set_value("Item", item_code, "standard_rate", rate)


def sync_legacy_item_field(doc):
    """Keep the legacy item field in sync with the first pricing row."""
    if doc.pricing:
        first_item = doc.pricing[0].item
        if first_item:
            doc.item = first_item


# ── Pricing Rule sync for grant rules ────────────────────────────────────


def sync_pricing_rules(doc):
    """
    Ensures a Pricing Rule exists for each grant rule on this plan.
    Removes Pricing Rules for grant rules that were deleted.
    """
    from msuite.services.customer_group_service import (
        sync_pricing_rule_for_grant_rule,
        _make_pricing_rule_title,
    )
    from msuite.constants import MSUITE_PRICING_RULE_PREFIX

    current_grant_titles = set()
    for row in doc.grants:
        granted_code = frappe.db.get_value("MSuite Plan", row.granted_plan, "plan_code")
        if not granted_code:
            continue

        title = _make_pricing_rule_title(doc.plan_code, granted_code, row.grant_type)
        current_grant_titles.add(title)

        sync_pricing_rule_for_grant_rule(
            trigger_plan_name=doc.name,
            granted_plan_name=row.granted_plan,
            grant_type=row.grant_type,
            discount_pct=100.0 if row.grant_type == "Complimentary" else 50.0,
        )

    # Delete Pricing Rules for removed grant rules
    existing_rules = frappe.get_all(
        "Pricing Rule",
        filters={"title": ["like", f"{MSUITE_PRICING_RULE_PREFIX} - {doc.plan_code} →%"]},
        fields=["name", "title"],
    )
    for rule in existing_rules:
        if rule.title not in current_grant_titles:
            try:
                frappe.delete_doc("Pricing Rule", rule.name, ignore_permissions=True, force=True)
            except Exception:
                frappe.db.set_value("Pricing Rule", rule.name, "disable", 1)


# ── Delete protection ────────────────────────────────────────────────────


def validate_no_active_grants(doc):
    """Block deletion if customers have active entitlements from this plan."""
    active_grants = frappe.db.count(
        "MSuite Customer Grant",
        {"granted_plan": doc.name, "status": "Active"},
    )
    if active_grants:
        frappe.throw(
            f"Cannot delete {doc.plan_name}. "
            f"{active_grants} customer(s) have active entitlements. Revoke all grants first.",
            MSuiteError,
        )


def validate_no_active_subscriptions(doc):
    """Block deletion if active subscriptions reference this plan's items."""
    item_codes = [r.item for r in (doc.pricing or []) if r.item]
    if doc.item and doc.item not in item_codes:
        item_codes.append(doc.item)
    if not item_codes:
        return

    sp_names = frappe.get_all(
        "Subscription Plan", filters={"item": ["in", item_codes]}, pluck="name"
    )
    if not sp_names:
        return

    active_subs = frappe.get_all(
        "Subscription Plan Detail",
        filters={"plan": ["in", sp_names], "parenttype": "Subscription"},
        fields=["parent"],
        group_by="parent",
    )
    active_sub_names = []
    for s in active_subs:
        status = frappe.db.get_value("Subscription", s.parent, ["docstatus", "status"], as_dict=True)
        if status and status.status not in ("Cancelled", "Completed"):
            active_sub_names.append(s.parent)

    if active_sub_names:
        display = ", ".join(active_sub_names[:3])
        if len(active_sub_names) > 3:
            display += "..."
        frappe.throw(
            f"Cannot delete {doc.plan_name}. "
            f"{len(active_sub_names)} active subscription(s): {display}. Cancel all first.",
            MSuiteError,
        )


def validate_no_historical_invoices(doc):
    """Block deletion if Sales Invoices exist for this plan's items."""
    item_codes = [r.item for r in (doc.pricing or []) if r.item]
    if doc.item and doc.item not in item_codes:
        item_codes.append(doc.item)
    for item_code in item_codes:
        if frappe.db.exists("Sales Invoice Item", {"item_code": item_code, "docstatus": ["!=", 2]}):
            frappe.throw(
                f"Cannot delete {doc.plan_name} — Sales Invoices exist for {item_code}. "
                "Deactivate the plan instead.",
                MSuiteError,
            )


def cascade_delete_if_safe(doc):
    """Delete orphaned grants, Subscription Plans, and Items."""
    for g in frappe.get_all(
        "MSuite Customer Grant", filters={"granted_plan": doc.name}, pluck="name"
    ):
        frappe.delete_doc("MSuite Customer Grant", g, ignore_permissions=True, force=True)

    item_codes = [r.item for r in (doc.pricing or []) if r.item]
    if doc.item and doc.item not in item_codes:
        item_codes.append(doc.item)

    for item_code in item_codes:
        for sp in frappe.get_all("Subscription Plan", filters={"item": item_code}, pluck="name"):
            frappe.delete_doc("Subscription Plan", sp, ignore_permissions=True, force=True)
        if frappe.db.exists("Item", item_code):
            try:
                frappe.delete_doc("Item", item_code, ignore_permissions=True, force=True)
            except Exception as e:
                frappe.log_error(f"Could not delete Item {item_code}: {e}", "MSuite Plan Delete")


# ── Whitelisted lifecycle actions ────────────────────────────────────────


@frappe.whitelist()
def activate_plan(plan_name: str) -> dict:
    """
    Activate an MSuite Plan. Validates readiness, creates ERPNext
    Items + Subscription Plans, sets is_active=1.
    """
    require_system_manager_or_msuite_manager()
    doc = frappe.get_doc("MSuite Plan", plan_name)
    validate_activation_readiness(doc)
    create_items_and_subscription_plans(doc)
    doc.is_active = 1
    doc.activated_on = now()
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "status": "success",
        "plan": doc.name,
        "message": f"Plan '{doc.plan_name}' activated successfully",
    }


@frappe.whitelist()
def deactivate_plan(plan_name: str) -> dict:
    """Deactivate an MSuite Plan. Existing subscriptions continue."""
    require_system_manager_or_msuite_manager()
    doc = frappe.get_doc("MSuite Plan", plan_name)
    if not doc.is_active:
        frappe.throw("Plan is already inactive", frappe.ValidationError)

    doc.is_active = 0
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {"status": "success", "message": f"Plan '{doc.plan_name}' deactivated"}


@frappe.whitelist()
def reactivate_plan(plan_name: str) -> dict:
    """Reactivate a previously deactivated plan. Items must still exist."""
    require_system_manager_or_msuite_manager()
    doc = frappe.get_doc("MSuite Plan", plan_name)
    if doc.is_active:
        frappe.throw("Plan is already active", frappe.ValidationError)

    if doc.tier != PlanTier.TRIAL:
        for row in doc.pricing or []:
            if row.item and not frappe.db.exists("Item", row.item):
                frappe.throw(
                    f"Item {row.item} no longer exists. Cannot reactivate.",
                    frappe.ValidationError,
                )

    doc.is_active = 1
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {"status": "success", "message": f"Plan '{doc.plan_name}' reactivated"}
