"""Bundle management service."""
import frappe

from msuite.constants import PlanTier, MSUITE_LOGGER_NAME
from msuite.exceptions import BundleNotFoundError

logger = frappe.logger(MSUITE_LOGGER_NAME)


def get_bundle_for_item(item_code: str) -> str | None:
    """
    Returns MSuite Bundle name if item has is_msuite_bundle=1.

    Args:
        item_code: ERPNext Item name

    Returns:
        MSuite Bundle name or None
    """
    item_data = frappe.db.get_value(
        "Item", item_code, ["is_msuite_bundle", "msuite_bundle"], as_dict=True
    )
    if not item_data or not item_data.is_msuite_bundle:
        return None
    return item_data.msuite_bundle


def is_bundle_item(item_code: str) -> bool:
    """
    Returns True if item_code is tagged as a bundle item.

    Args:
        item_code: ERPNext Item name

    Returns:
        bool
    """
    return bool(frappe.db.get_value("Item", item_code, "is_msuite_bundle"))


def get_bundle_components(bundle_name: str) -> list[dict]:
    """
    Returns list of component plan dicts from MSuite Bundle.

    Args:
        bundle_name: MSuite Bundle name

    Returns:
        List of component dicts

    Raises:
        BundleNotFoundError: if bundle not found or inactive
    """
    if not frappe.db.exists("MSuite Bundle", bundle_name):
        frappe.throw(f"Bundle {bundle_name} not found", BundleNotFoundError)

    bundle_doc = frappe.get_doc("MSuite Bundle", bundle_name)
    if not bundle_doc.is_active:
        frappe.throw(f"Bundle {bundle_name} is inactive", BundleNotFoundError)

    return [
        {"plan": row.plan, "quantity": row.quantity, "notes": row.notes or ""}
        for row in bundle_doc.components
    ]


def get_bundle_merged_features(bundle_name: str) -> dict:
    """
    Returns merged feature map for all component plans.

    Args:
        bundle_name: MSuite Bundle name

    Returns:
        Merged feature map dict
    """
    from msuite.services.entitlement_service import _build_feature_map_for_plan, _merge_feature_maps

    components = get_bundle_components(bundle_name)
    merged: dict = {}

    for comp in components:
        plan_features = _build_feature_map_for_plan(comp["plan"])
        merged = _merge_feature_maps(merged, plan_features)

    return merged


def validate_bundle(bundle_name: str) -> list[str]:
    """
    Validates bundle configuration.

    Args:
        bundle_name: MSuite Bundle name

    Returns:
        List of error strings (empty list = valid)
    """
    errors: list[str] = []

    bundle_doc = frappe.get_doc("MSuite Bundle", bundle_name)

    if len(bundle_doc.components) < 2:
        errors.append("Bundle must have at least 2 components")

    plans = [row.plan for row in bundle_doc.components]
    if len(plans) != len(set(plans)):
        errors.append("Duplicate plans in bundle components")

    for row in bundle_doc.components:
        plan_data = frappe.db.get_value(
            "MSuite Plan", row.plan, ["is_active", "tier"], as_dict=True
        )
        if not plan_data:
            errors.append(f"Plan {row.plan} not found")
            continue
        if not plan_data.is_active:
            errors.append(f"Plan {row.plan} is not active")
        if plan_data.tier == PlanTier.TRIAL:
            errors.append(f"Trial-tier plan {row.plan} cannot be a bundle component")

    return errors
