"""Redis cache utilities for MSuite."""
import json

import frappe
from msuite.constants import (
    ENTITLEMENT_CACHE_KEY_PREFIX,
    ENTITLEMENT_CACHE_TTL_SECONDS,
    RELAY_ROUTE_CACHE_KEY_PREFIX,
    RELAY_ROUTE_CACHE_TTL_SECONDS,
    TRIAL_PLAN_CACHE_KEY_PREFIX,
    TRIAL_PLAN_CACHE_TTL_SECONDS,
)

# Cached negative result. A stale webhook URL or a probe against the public
# relay endpoint would otherwise hit the database on every single request —
# caching the miss is what keeps that from being a cheap way to load the DB.
_ROUTE_MISS = ""


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


def _make_relay_route_key(client_code: str) -> str:
    """Returns Redis key for a webhook relay route lookup."""
    return f"{RELAY_ROUTE_CACHE_KEY_PREFIX}:{frappe.scrub(client_code)}"


def get_relay_route_cache(client_code: str):
    """Cached MSuite Client name for a client_code.

    Three-way return, because "not cached" and "cached as unknown" must not
    look the same:
      * a name  — cached hit
      * ""      — cached MISS, do not re-query
      * None    — nothing cached, go look
    """
    return frappe.cache().get_value(_make_relay_route_key(client_code))


def set_relay_route_cache(client_code: str, client_name: str) -> None:
    """Caches a route lookup, including a miss (pass an empty string).

    Only the doc NAME is cached — never `client_url`, `api_key` or the
    secret. Those are read from the Document at forward time, where Frappe's
    own document cache and password handling already apply.
    """
    frappe.cache().set_value(
        _make_relay_route_key(client_code),
        client_name or _ROUTE_MISS,
        expires_in_sec=RELAY_ROUTE_CACHE_TTL_SECONDS,
    )


def invalidate_relay_route_cache(client_code: str) -> None:
    """Deletes the route cache for a client_code."""
    if client_code:
        frappe.cache().delete_value(_make_relay_route_key(client_code))
