"""Redis cache utilities for MSuite."""
import json

import frappe
from msuite.constants import (
    ENTITLEMENT_CACHE_KEY_PREFIX,
    ENTITLEMENT_CACHE_TTL_SECONDS,
    TRIAL_PLAN_CACHE_KEY_PREFIX,
    TRIAL_PLAN_CACHE_TTL_SECONDS,
)


def _make_entitlement_key(customer: str) -> str:
    """Returns Redis key for customer entitlement."""
    return f"{ENTITLEMENT_CACHE_KEY_PREFIX}:{frappe.scrub(customer)}"


def _make_trial_plan_key(product_name: str) -> str:
    """Returns Redis key for trial plan."""
    return f"{TRIAL_PLAN_CACHE_KEY_PREFIX}:{frappe.scrub(product_name)}"


def get_entitlement_cache(customer: str) -> dict | None:
    """Returns cached entitlement dict. None on miss."""
    key = _make_entitlement_key(customer)
    data = frappe.cache().get_value(key)
    if data:
        return json.loads(data) if isinstance(data, str) else data
    return None


def set_entitlement_cache(customer: str, data: dict) -> None:
    """Sets entitlement cache with TTL."""
    key = _make_entitlement_key(customer)
    frappe.cache().set_value(key, json.dumps(data), expires_in_sec=ENTITLEMENT_CACHE_TTL_SECONDS)


def invalidate_entitlement_cache(customer: str) -> None:
    """Deletes entitlement cache key for customer."""
    key = _make_entitlement_key(customer)
    frappe.cache().delete_value(key)


def get_trial_plan_cache(product_name: str) -> str | None:
    """Returns cached trial plan name for product. None on miss."""
    key = _make_trial_plan_key(product_name)
    return frappe.cache().get_value(key)


def set_trial_plan_cache(product_name: str, plan_name: str) -> None:
    """Sets trial plan cache with TTL."""
    key = _make_trial_plan_key(product_name)
    frappe.cache().set_value(key, plan_name, expires_in_sec=TRIAL_PLAN_CACHE_TTL_SECONDS)


def invalidate_trial_plan_cache(product_name: str) -> None:
    """Deletes trial plan cache for product."""
    key = _make_trial_plan_key(product_name)
    frappe.cache().delete_value(key)
