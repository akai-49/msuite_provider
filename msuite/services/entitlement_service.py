"""
Entitlement resolution service.

The single source of truth for what a customer can access right now.
All results are Redis-cached. Cache is invalidated on any grant change.

Merge strategy when multiple plans provide the same feature:
- Boolean: True wins (any plan enabling it = enabled)
- Numeric: Maximum value wins
- Source tracking: records which plan provided the winning value
"""
from typing import Any

import frappe
from frappe.utils import now_datetime

from msuite.constants import GrantStatus, MSUITE_LOGGER_NAME
from msuite.exceptions import EntitlementError
from msuite.utils.cache import (
    get_entitlement_cache,
    set_entitlement_cache,
    invalidate_entitlement_cache,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)


def get_customer_entitlements(customer: str) -> dict:
    """
    Returns complete merged entitlement map for a customer.

    Args:
        customer: ERPNext Customer document name

    Returns:
        Dict with customer, resolved_at, active_plans, active_grants, features

    Raises:
        EntitlementError: if customer does not exist in ERPNext
    """
    if not frappe.db.exists("Customer", customer):
        frappe.throw(f"Customer {customer} does not exist", EntitlementError)

    cached = get_entitlement_cache(customer)
    if cached:
        return cached

    merged_features: dict[str, Any] = {}
    active_plans: list[str] = []
    active_grants_list: list[dict] = []

    # Collect all MSuite Plan names we need to resolve
    msuite_plan_names: set[str] = set()

    # Load Active Subscriptions (ignore_permissions=True because client API calls run under Guest)
    subscriptions = frappe.get_all(
        "Subscription",
        filters={"party_type": "Customer", "party": customer, "status": ["in", ["Active", "Trialing"]]},
        fields=["name"],
        ignore_permissions=True,
    )

    # Resolve each subscription's MSuite plans in a single pass — load each
    # Subscription doc once and memoize the Subscription Plan → MSuite Plan
    # lookup (previously both were done twice, once per merge phase).
    sub_msuite_plans: list[str] = []  # ordered, drives the feature merge below
    sp_to_msuite: dict[str, str | None] = {}
    for sub in subscriptions:
        sub_doc = frappe.get_doc("Subscription", sub.name)
        for plan_row in sub_doc.plans:
            if plan_row.plan not in sp_to_msuite:
                sp_to_msuite[plan_row.plan] = get_msuite_plan_for_subscription_plan(plan_row.plan)
            msuite_plan = sp_to_msuite[plan_row.plan]
            if msuite_plan:
                msuite_plan_names.add(msuite_plan)
                sub_msuite_plans.append(msuite_plan)

    # Load Active Customer Grants
    grants = frappe.get_all(
        "MSuite Customer Grant",
        filters={"customer": customer, "status": GrantStatus.ACTIVE},
        fields=["name", "granted_plan", "grant_type", "expires_on"],
        ignore_permissions=True,
    )

    for grant in grants:
        msuite_plan_names.add(grant.granted_plan)

    # Batch-load all plan_name + product values in one query. `product` is a
    # Link to MSuite Product, which is autonamed `field:product_code` — so the
    # value IS the product code the client gates on ("WA", "ADS", …).
    plan_name_map: dict[str, str] = {}
    plan_product_map: dict[str, str] = {}
    if msuite_plan_names:
        plan_rows = frappe.get_all(
            "MSuite Plan",
            filters={"name": ["in", list(msuite_plan_names)]},
            fields=["name", "plan_name", "product"],
            ignore_permissions=True,
        )
        plan_name_map = {r.name: r.plan_name for r in plan_rows}
        plan_product_map = {r.name: r.product for r in plan_rows if r.product}

    # Build feature maps for all unique plans (each _build_feature_map_for_plan is cached-friendly)
    plan_feature_cache: dict[str, dict] = {}
    for plan in msuite_plan_names:
        plan_feature_cache[plan] = _build_feature_map_for_plan(plan)

    # Merge subscription plan features (order preserved from the pass above)
    for msuite_plan in sub_msuite_plans:
        if msuite_plan in plan_feature_cache:
            merged_features = _merge_feature_maps(merged_features, plan_feature_cache[msuite_plan])
            pn = plan_name_map.get(msuite_plan)
            if pn and pn not in active_plans:
                active_plans.append(pn)

    # Merge grant features
    for grant in grants:
        if grant.granted_plan in plan_feature_cache:
            merged_features = _merge_feature_maps(merged_features, plan_feature_cache[grant.granted_plan])
        pn = plan_name_map.get(grant.granted_plan)
        if pn and pn not in active_plans:
            active_plans.append(pn)
        active_grants_list.append({
            "grant_name": grant.name,
            "granted_plan": grant.granted_plan,
            "grant_type": grant.grant_type,
            "expires_on": str(grant.expires_on) if grant.expires_on else None,
        })

    # Products the customer holds. Every plan in msuite_plan_names contributed
    # to the merge above (subscription or grant), so its product is entitled.
    # Gating on the plan's product — not on the feature rows' parent product —
    # is what keeps the six cross-product feature_key collisions
    # (templates, analytics, inbox_whatsapp, …) unambiguous.
    active_products = sorted({plan_product_map[p] for p in msuite_plan_names if p in plan_product_map})

    result = {
        "customer": customer,
        "resolved_at": str(now_datetime()),
        "active_plans": active_plans,
        "active_products": active_products,
        "active_grants": active_grants_list,
        "features": merged_features,
    }

    set_entitlement_cache(customer, result)
    return result


def get_msuite_plan_for_subscription_plan(subscription_plan_name: str) -> str | None:
    """
    Resolves: Subscription Plan -> Item -> MSuite Plan name.

    Checks MSuite Plan Pricing child table first (new multi-interval model),
    falls back to legacy MSuite Plan.item field.

    Args:
        subscription_plan_name: ERPNext Subscription Plan name

    Returns:
        MSuite Plan name or None
    """
    item = frappe.db.get_value("Subscription Plan", subscription_plan_name, "item")
    if not item:
        return None

    # Check pricing child table: find MSuite Plan that has this item in its pricing rows
    pricing_row = frappe.db.get_value(
        "MSuite Plan Pricing",
        {"item": item},
        "parent",
    )
    if pricing_row:
        is_active = frappe.db.get_value("MSuite Plan", pricing_row, "is_active")
        if is_active:
            return pricing_row

    # Fallback: legacy item field on MSuite Plan
    return frappe.db.get_value(
        "MSuite Plan", {"item": item, "is_active": 1}, "name"
    )


def _build_feature_map_for_plan(plan_name: str) -> dict:
    # Builds complete feature dict for one MSuite Plan
    plan_doc = frappe.get_doc("MSuite Plan", plan_name)
    feature_map: dict = {}

    # Batch-resolve product_feature → feature_key in one query instead of a
    # frappe.get_doc per feature row.
    pf_names = [row.product_feature for row in plan_doc.features if row.product_feature]
    key_map: dict[str, str] = {}
    if pf_names:
        key_map = {
            r.name: r.feature_key
            for r in frappe.get_all(
                "MSuite Product Feature",
                filters={"name": ["in", pf_names]},
                fields=["name", "feature_key"],
            )
        }

    for row in plan_doc.features:
        feature_key = key_map.get(row.product_feature)
        if not feature_key:
            continue
        feature_map[feature_key] = {
            "enabled": bool(row.is_enabled),
            "limit": row.limit_value if row.limit_value else None,
            "limit_label": row.limit_label,
            "source_plan": plan_doc.plan_name,
        }

    return feature_map


def _merge_feature_maps(base: dict, addition: dict) -> dict:
    # Merges addition into base without mutating either
    result = {k: dict(v) for k, v in base.items()}

    for key, add_val in addition.items():
        if key not in result:
            result[key] = dict(add_val)
            continue

        base_val = result[key]

        # Boolean merge: True wins
        if add_val["enabled"] and not base_val["enabled"]:
            result[key] = dict(add_val)
        elif add_val["enabled"] and base_val["enabled"]:
            # Both enabled: compare limits (Numeric merge)
            base_limit = base_val.get("limit")
            add_limit = add_val.get("limit")

            if add_limit is None and base_limit is not None:
                # None = unlimited beats any number
                result[key] = dict(add_val)
            elif add_limit is not None and base_limit is not None:
                if add_limit > base_limit:
                    result[key] = dict(add_val)

    return result


def check_feature_access(customer: str, feature_key: str) -> bool:
    """
    Returns True if customer has feature_key enabled.

    Args:
        customer: ERPNext Customer name
        feature_key: e.g. "messaging", "calling"

    Returns:
        bool
    """
    entitlements = get_customer_entitlements(customer)
    feature = entitlements.get("features", {}).get(feature_key)
    if not feature:
        return False
    return feature.get("enabled", False)


def get_feature_limit(customer: str, feature_key: str) -> float | None:
    """
    Returns numeric limit for a feature. None = unlimited.

    Args:
        customer: ERPNext Customer name
        feature_key: e.g. "templates", "broadcast"

    Returns:
        float limit, or None for unlimited/disabled/not found
    """
    entitlements = get_customer_entitlements(customer)
    feature = entitlements.get("features", {}).get(feature_key)
    if not feature or not feature.get("enabled"):
        return None
    return feature.get("limit")
