"""Bundle API endpoints."""
import frappe

from msuite.constants import ErrorCode
from msuite.utils.validators import success_response, error_response


@frappe.whitelist()
def get_bundles() -> dict:
    """Returns all active bundles with components and merged features."""
    try:
        from msuite.services.bundle_service import get_bundle_components, get_bundle_merged_features

        bundles = frappe.get_list(
            "MSuite Bundle",
            filters={"is_active": 1},
            fields=["name", "bundle_name", "bundle_code", "description"],
        )

        result = []
        for bundle in bundles:
            components = get_bundle_components(bundle.name)
            features = get_bundle_merged_features(bundle.name)
            result.append({
                "bundle": bundle.name,
                "bundle_name": bundle.bundle_name,
                "bundle_code": bundle.bundle_code,
                "description": bundle.description,
                "components": components,
                "features": features,
            })

        return success_response(result)
    except Exception as e:
        return error_response(ErrorCode.BUNDLE_NOT_FOUND, str(e))


@frappe.whitelist()
def get_bundle_features(bundle_name: str) -> dict:
    """Returns merged feature set for a bundle."""
    try:
        from msuite.services.bundle_service import get_bundle_merged_features
        features = get_bundle_merged_features(bundle_name)
        return success_response(features)
    except Exception as e:
        return error_response(ErrorCode.BUNDLE_NOT_FOUND, str(e))


@frappe.whitelist()
def get_bundle_details(bundle_name: str) -> dict:
    """Returns bundle details including components and features."""
    try:
        from msuite.services.bundle_service import get_bundle_components, get_bundle_merged_features
        from msuite.services.entitlement_service import _build_feature_map_for_plan

        bundle_doc = frappe.get_doc("MSuite Bundle", bundle_name)
        components = get_bundle_components(bundle_name)
        merged = get_bundle_merged_features(bundle_name)

        component_details = []
        for comp in components:
            features = _build_feature_map_for_plan(comp["plan"])
            component_details.append({
                "plan": comp["plan"],
                "quantity": comp["quantity"],
                "features": features,
            })

        items = frappe.get_list(
            "Item",
            filters={"is_msuite_bundle": 1, "msuite_bundle": bundle_name},
            fields=["name", "item_name", "standard_rate"],
        )

        return success_response({
            "bundle": bundle_name,
            "bundle_name": bundle_doc.bundle_name,
            "bundle_code": bundle_doc.bundle_code,
            "description": bundle_doc.description,
            "components": component_details,
            "merged_features": merged,
            "items": items,
        })
    except Exception as e:
        return error_response(ErrorCode.BUNDLE_NOT_FOUND, str(e))
