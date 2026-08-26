"""
Add ai_analytics_bot feature to Social Post product and plans.
"""
import frappe
from msuite.seeds.social_post import (
    SOCIAL_PRODUCT_FEATURES,
    SOCIAL_PLANS,
    SOCIAL_PLAN_FEATURES,
)


def execute():
    product_name = frappe.db.get_value(
        "MSuite Product", {"product_code": "SOCIAL"}, "name"
    )
    if not product_name:
        return  # Product not installed yet — installer will seed it.

    _reseed_product_features(product_name)
    feature_name_by_key = _feature_name_map(product_name)

    for plan in SOCIAL_PLANS:
        _reseed_plan_features(
            plan["plan_code"],
            SOCIAL_PLAN_FEATURES[plan["plan_code"]],
            feature_name_by_key,
        )

    frappe.db.commit()


def _reseed_product_features(product_name: str) -> None:
    frappe.db.delete("MSuite Product Feature", {"parent": product_name})
    product = frappe.get_doc("MSuite Product", product_name)
    for feature in SOCIAL_PRODUCT_FEATURES:
        product.append("features", feature)
    product.save(ignore_permissions=True)


def _feature_name_map(product_name: str) -> dict[str, str]:
    product = frappe.get_doc("MSuite Product", product_name)
    return {row.feature_key: row.name for row in product.features}


def _reseed_plan_features(
    plan_code: str,
    feature_values: dict[str, tuple[bool, int | None, str | None]],
    feature_name_by_key: dict[str, str],
) -> None:
    plan_name = frappe.db.get_value("MSuite Plan", {"plan_code": plan_code}, "name")
    if not plan_name:
        return

    frappe.db.delete("MSuite Plan Feature", {"parent": plan_name})
    plan = frappe.get_doc("MSuite Plan", plan_name)

    for feature_key, (is_enabled, limit_value, limit_label) in feature_values.items():
        product_feature = feature_name_by_key.get(feature_key)
        if not product_feature:
            continue

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
