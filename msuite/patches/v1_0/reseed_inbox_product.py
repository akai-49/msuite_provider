"""
Reseed the Inbox product + plans in-place.

Wipes old feature rows on the Inbox product and on every INBOX-* plan,
and builds them from the seeds definition.
"""
import frappe
from frappe.utils import now

from msuite.seeds.inbox import (
    INBOX_PRODUCT_FEATURES,
    INBOX_PLANS,
    INBOX_PLAN_FEATURES,
)


def execute():
    product_name = frappe.db.get_value(
        "MSuite Product", {"product_code": "INBOX"}, "name"
    )
    if not product_name:
        product = frappe.new_doc("MSuite Product")
        product.product_code = "INBOX"
        product.product_name = "Inbox"
        product.description = "Unified omnichannel inbox"
        product.is_active = 1
        product.insert(ignore_permissions=True)
        product_name = product.name

    _reseed_product_features(product_name)
    feature_name_by_key = _feature_name_map(product_name)

    for plan in INBOX_PLANS:
        _ensure_plan_exists(plan, product_name)
        _reseed_plan_features(
            plan["plan_code"],
            INBOX_PLAN_FEATURES[plan["plan_code"]],
            feature_name_by_key,
        )

    frappe.db.commit()


def _reseed_product_features(product_name: str) -> None:
    """Replace the product's feature child table with the canonical set."""
    frappe.db.delete("MSuite Product Feature", {"parent": product_name})

    product = frappe.get_doc("MSuite Product", product_name)
    for feature in INBOX_PRODUCT_FEATURES:
        product.append("features", feature)
    product.save(ignore_permissions=True)


def _feature_name_map(product_name: str) -> dict[str, str]:
    """Return `{feature_key: product_feature_row_name}` for plan linking."""
    product = frappe.get_doc("MSuite Product", product_name)
    return {row.feature_key: row.name for row in product.features}


def _reseed_plan_features(
    plan_code: str,
    feature_values: dict[str, tuple[bool, int | None, str | None]],
    feature_name_by_key: dict[str, str],
) -> None:
    """Replace a single plan's feature table with the seed values."""
    plan_name = frappe.db.get_value("MSuite Plan", {"plan_code": plan_code}, "name")
    if not plan_name:
        return

    frappe.db.delete("MSuite Plan Feature", {"parent": plan_name})
    plan = frappe.get_doc("MSuite Plan", plan_name)

    for feature_key, (is_enabled, limit_value, limit_label) in feature_values.items():
        product_feature = feature_name_by_key.get(feature_key)
        if not product_feature:
            continue  # Orphan reference — silently skip so the patch stays idempotent.

        row = {
            "product_feature": product_feature,
            "is_enabled": 1 if is_enabled else 0,
        }
        if limit_value is not None:
            row["limit_value"] = limit_value
        if limit_label:
            row["limit_label"] = limit_label
        plan.append("features", row)

    plan.save(ignore_permissions=True)


def _ensure_plan_exists(plan_cfg: dict, product_name: str) -> None:
    """Create the MSuite Plan + backing ERPNext Item/Subscription Plan if missing."""
    if frappe.db.exists("MSuite Plan", {"plan_code": plan_cfg["plan_code"]}):
        return

    _ensure_item(plan_cfg)
    _ensure_subscription_plan(plan_cfg)

    plan = frappe.new_doc("MSuite Plan")
    plan.plan_name = plan_cfg["plan_name"]
    plan.plan_code = plan_cfg["plan_code"]
    plan.product = product_name
    plan.tier = plan_cfg["tier"]
    plan.item = plan_cfg["item_code"]
    plan.is_active = 1
    plan.activated_on = now()
    plan.insert(ignore_permissions=True)


def _ensure_item(plan_cfg: dict) -> None:
    """Create the ERPNext Item backing a plan (no-op if present)."""
    if frappe.db.exists("Item", plan_cfg["item_code"]):
        return

    _ensure_item_group("Inbox")

    item = frappe.new_doc("Item")
    item.item_code = plan_cfg["item_code"]
    item.item_name = plan_cfg["plan_name"]
    item.item_group = "Inbox"
    item.stock_uom = "Nos"
    item.is_stock_item = 0
    item.include_item_in_manufacturing = 0
    item.standard_rate = plan_cfg["rate"]
    item.insert(ignore_permissions=True)


def _ensure_item_group(group_name: str) -> None:
    """Ensure the backing Item Group exists (no-op if present)."""
    if not frappe.db.exists("Item Group", group_name):
        doc = frappe.new_doc("Item Group")
        doc.item_group_name = group_name
        doc.parent_item_group = "All Item Groups"
        doc.insert(ignore_permissions=True)


def _ensure_subscription_plan(plan_cfg: dict) -> None:
    """Create the ERPNext Subscription Plan backing a plan (no-op if present)."""
    if frappe.db.exists("Subscription Plan", plan_cfg["item_code"]):
        return

    sub = frappe.new_doc("Subscription Plan")
    sub.plan_name = plan_cfg["item_code"]
    sub.item = plan_cfg["item_code"]
    sub.price_determination = "Fixed Rate"
    sub.cost = plan_cfg["rate"]
    sub.billing_interval = "Month"
    sub.billing_interval_count = 1
    sub.insert(ignore_permissions=True)
