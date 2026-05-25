"""Plan API endpoints."""
import json

import frappe

from msuite.constants import ErrorCode, PlanTier
from msuite.utils.validators import success_response, error_response


@frappe.whitelist()
def get_plans(
    product: str | None = None,
    tier: str | None = None,
    include_trial: bool = False,
) -> dict:
    """Returns active plans, optionally filtered."""
    try:
        filters: dict = {"is_active": 1}
        if product:
            filters["product"] = product
        if tier:
            filters["tier"] = tier
        if not include_trial and not tier:
            filters["tier"] = ["!=", PlanTier.TRIAL]

        plans = frappe.get_list(
            "MSuite Plan",
            filters=filters,
            fields=["name", "plan_name", "plan_code", "product", "tier", "item", "sort_order"],
            order_by="sort_order asc",
        )
        return success_response(plans)
    except Exception as e:
        return error_response(ErrorCode.INVALID_INPUT, str(e))


@frappe.whitelist()
def get_plan_features(plan_name: str) -> dict:
    """Returns full feature set for a plan with limits."""
    try:
        from msuite.services.entitlement_service import _build_feature_map_for_plan

        plan_doc = frappe.get_doc("MSuite Plan", plan_name)
        features = _build_feature_map_for_plan(plan_name)

        grants = [
            {
                "granted_plan": row.granted_plan,
                "grant_type": row.grant_type,
                "expires_after_days": row.expires_after_days,
            }
            for row in plan_doc.grants
        ]

        return success_response({
            "plan": plan_name,
            "product": plan_doc.product,
            "tier": plan_doc.tier,
            "features": features,
            "grants": grants,
        })
    except Exception as e:
        return error_response(ErrorCode.PLAN_NOT_FOUND, str(e))


@frappe.whitelist()
def compare_plans(plan_names: str) -> dict:
    """Returns side-by-side feature comparison with labels and types."""
    try:
        from msuite.services.entitlement_service import _build_feature_map_for_plan

        names = json.loads(plan_names)

        # Get plan display names
        plan_display_names = []
        for name in names:
            display = frappe.db.get_value("MSuite Plan", name, "plan_name") or name
            plan_display_names.append(display)

        all_features: set = set()
        plan_features_map: dict = {}

        for name in names:
            features = _build_feature_map_for_plan(name)
            plan_features_map[name] = features
            all_features.update(features.keys())

        # Build feature metadata (label, type) from Product Feature definitions
        feature_meta: dict = {}
        for feature_key in all_features:
            pf = frappe.db.get_value(
                "MSuite Product Feature",
                {"feature_key": feature_key},
                ["feature_label", "feature_type"],
                as_dict=True,
            )
            feature_meta[feature_key] = {
                "label": pf.feature_label if pf else feature_key,
                "type": pf.feature_type if pf else "Boolean",
            }

        comparison: dict = {}
        for feature_key in sorted(all_features):
            entry = {
                "label": feature_meta[feature_key]["label"],
                "type": feature_meta[feature_key]["type"],
            }
            for i, name in enumerate(names):
                display = plan_display_names[i]
                feat = plan_features_map.get(name, {}).get(feature_key)
                if feat:
                    entry[display] = {
                        "enabled": feat["enabled"],
                        "limit": feat["limit"],
                        "limit_label": feat.get("limit_label"),
                    }
                else:
                    entry[display] = {
                        "enabled": False,
                        "limit": None,
                        "limit_label": None,
                    }
            comparison[feature_key] = entry

        return success_response({
            "plans": plan_display_names,
            "features": comparison,
        })
    except Exception as e:
        return error_response(ErrorCode.PLAN_NOT_FOUND, str(e))
