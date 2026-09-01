"""
Meta Catalogue OAuth handler — Meta Product Catalogs.

Authenticates against Meta via Facebook Login with catalog_management and business_management scopes.

Flow:
  1. build_auth_url()    → Facebook OAuth dialog with catalog scopes
  2. exchange_token()    → short → long-lived user token (shared)
  3. discover_accounts() → /me/businesses → /{biz}/owned_product_catalogs
  4. Upsert MSuite Connected Account rows & push catalog connection to client.
"""
from __future__ import annotations

import frappe
import requests
from frappe.utils import add_to_date, now

from msuite.constants import GRAPH_API_BASE, MSUITE_LOGGER_NAME, Platform

from .base import (
    push_account_to_client,
    upsert_connected_account,
)
from .meta_base import (
    LONG_LIVED_TOKEN_TTL,
    build_facebook_login_url,
    discover_businesses,
    exchange_code_for_long_lived_token,
    inspect_debug_token,
    upsert_businesses_as_auth_accounts,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)

META_CATALOGUE_SCOPES = [
    "catalog_management",
    "business_management",
]


def build_auth_url(client_name: str, state: str) -> str:
    """Facebook OAuth dialog URL scoped to Catalogue management."""
    return build_facebook_login_url(META_CATALOGUE_SCOPES, state)


def exchange_token(code: str, state_data: dict) -> dict:
    """Code → long-lived user token (shared with meta_social)."""
    return exchange_code_for_long_lived_token(code)


def discover_accounts(client_name: str, token_data: dict) -> list[dict]:
    """Discover Meta Catalogs across the user's Businesses."""
    user_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", LONG_LIVED_TOKEN_TTL)
    app_name   = token_data.get("app_name", "")

    debug_info = inspect_debug_token(user_token)
    catalog_target_ids = debug_info.get("catalog_target_ids") or set()
    business_target_ids = debug_info.get("business_target_ids") or set()

    businesses = discover_businesses(user_token)
    if business_target_ids:
        businesses = [b for b in businesses if str(b.get("id")) in business_target_ids]

    biz_auth_map = upsert_businesses_as_auth_accounts(client_name, businesses)

    return _discover_catalogs(
        client_name, user_token, app_name, expires_in, businesses, biz_auth_map,
        catalog_target_ids=catalog_target_ids
    )


def _discover_catalogs(
    client_name: str,
    user_token: str,
    app_name: str,
    expires_in: int,
    businesses: list[dict],
    biz_auth_map: dict[str, str],
    catalog_target_ids: set[str] | None = None,
) -> list[dict]:
    connected: list[dict] = []
    sources = _catalog_sources(businesses)

    for url, biz_id, biz_name in sources:
        try:
            resp = requests.get(
                url,
                params={
                    "access_token": user_token,
                    "fields": "id,name,vertical,product_count,default_currency",
                },
                timeout=30,
            )
            if resp.status_code != 200:
                logger.error(f"Failed to discover catalogs at {url}: {resp.text}")
                continue
        except Exception as e:
            logger.error(f"Exception during catalog discovery at {url}: {e}")
            continue

        for cat in resp.json().get("data", []):
            catalog_id   = cat.get("id", "")
            catalog_name = cat.get("name", "")
            if not catalog_id:
                continue

            if catalog_target_ids and str(catalog_id) not in catalog_target_ids:
                logger.info(f"Skipping catalog {catalog_name} ({catalog_id}): not in selected catalog target IDs.")
                continue

            ca_name = upsert_connected_account(
                client_name, "Meta Catalogue", catalog_id, {
                    "display_name": catalog_name,
                    "auth_account": biz_auth_map.get(biz_id) if biz_id else None,
                    "access_token": user_token,
                    "token_type":   "User Token",
                    "msuite_app":   app_name,
                    "token_expiry": add_to_date(now(), seconds=expires_in),
                },
            )
            connected.append({"platform": "Meta Catalogue", "name": catalog_name, "catalog_id": catalog_id})

            push_account_to_client(client_name, "Meta Catalogue", {
                "catalog_id":       catalog_id,
                "catalog_name":     catalog_name,
                "access_token":     user_token,
                "business_id":      biz_id,
                "business_name":    biz_name,
                "default_currency": cat.get("default_currency", ""),
                "product_count":    cat.get("product_count", 0),
            })

    return connected


def _catalog_sources(businesses: list[dict]) -> list[tuple[str, str, str]]:
    if not businesses:
        return [(f"{GRAPH_API_BASE}/me/product_catalogs", "", "")]

    return [
        (f"{GRAPH_API_BASE}/{biz['id']}/owned_product_catalogs", biz["id"], biz.get("name", ""))
        for biz in businesses
    ]
