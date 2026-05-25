"""
Meta Ads OAuth handler — Ad Accounts only.

Organic Pages + Instagram access comes from `meta_social.py`. Both
handlers authenticate against the same Meta app (see `meta_base.py`)
but request different scope subsets so each MSuite product (SOCIAL vs
ADS) can ask Meta only for what its features actually use.

Flow:
  1. build_auth_url()    → Facebook OAuth dialog with ads scopes only
  2. exchange_token()    → short → long-lived user token (shared)
  3. discover_accounts() → /me/businesses → /{biz}/owned_ad_accounts
                           (falls back to /me/adaccounts if no businesses)
  4. Upsert MSuite Auth + Connected + Meta Ads Account rows, push
     credentials downstream.
"""
import frappe
import requests
from frappe.utils import add_to_date, now

from msuite.constants import GRAPH_API_BASE, MSUITE_LOGGER_NAME, Platform

from .base import (
    push_account_to_client,
    upsert_connected_account,
    upsert_meta_ads_account,
)
from .meta_base import (
    LONG_LIVED_TOKEN_TTL,
    build_facebook_login_url,
    discover_businesses,
    exchange_code_for_long_lived_token,
    upsert_businesses_as_auth_accounts,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)

META_ADS_SCOPES = [
    "ads_management",
    "ads_read",
    "business_management",
]


# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------


def build_auth_url(client_name: str, state: str) -> str:
    """Facebook OAuth dialog URL scoped to Ads only."""
    return build_facebook_login_url(META_ADS_SCOPES, state)


def exchange_token(code: str, state_data: dict) -> dict:
    """Code → long-lived user token (shared with meta_social)."""
    return exchange_code_for_long_lived_token(code)


def discover_accounts(client_name: str, token_data: dict) -> list[dict]:
    """Discover Meta Ad Accounts across the user's Businesses."""
    user_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", LONG_LIVED_TOKEN_TTL)
    app_name   = token_data.get("app_name", "")

    businesses   = discover_businesses(user_token)
    biz_auth_map = upsert_businesses_as_auth_accounts(client_name, businesses)

    return _discover_ad_accounts(
        client_name, user_token, app_name, expires_in, businesses, biz_auth_map
    )


# ---------------------------------------------------------------------------
# Ad Account discovery
# ---------------------------------------------------------------------------


def _discover_ad_accounts(
    client_name: str,
    user_token: str,
    app_name: str,
    expires_in: int,
    businesses: list[dict],
    biz_auth_map: dict[str, str],
) -> list[dict]:
    """Upsert every Ad Account the user can access + push to client.

    If the user has Meta Businesses we iterate `/{biz}/owned_ad_accounts`
    per business so each Ad Account is linked to its parent auth account.
    Otherwise we fall back to `/me/adaccounts` for personal-tier users.
    """
    connected: list[dict] = []

    sources = _ad_account_sources(businesses)
    for url, biz_id, biz_name in sources:
        try:
            resp = requests.get(
                url,
                params={
                    "access_token": user_token,
                    "fields":       "account_id,name,currency,timezone_name",
                },
                timeout=30,
            )
        except Exception as e:
            logger.error(f"Failed to discover ad accounts at {url}: {e}")
            continue

        for ad in resp.json().get("data", []):
            account_id   = ad.get("account_id", "")
            account_name = ad.get("name", "")
            if not account_id:
                continue

            ca_name = upsert_connected_account(
                client_name, Platform.META_ADS, account_id, {
                    "display_name":  account_name,
                    "auth_account":  biz_auth_map.get(biz_id) if biz_id else None,
                    "access_token":  user_token,
                    "token_type":    "User Token",
                    "msuite_app":    app_name,
                    "token_expiry":  add_to_date(now(), seconds=expires_in),
                },
            )
            upsert_meta_ads_account(ca_name, {
                "ad_account_id":   account_id,
                "ad_account_name": account_name,
                "currency":        ad.get("currency", ""),
                "timezone":        ad.get("timezone_name", ""),
            })
            connected.append({"platform": "Meta Ads", "name": account_name})

            push_account_to_client(client_name, Platform.META_ADS, {
                "ad_account_id":   account_id,
                "ad_account_name": account_name,
                "access_token":    user_token,
                "currency":        ad.get("currency", ""),
                "timezone":        ad.get("timezone_name", ""),
                "business_id":     biz_id,
                "business_name":   biz_name,
            })

    return connected


def _ad_account_sources(businesses: list[dict]) -> list[tuple[str, str, str]]:
    """Build the list of `(url, biz_id, biz_name)` to query for Ad Accounts.

    Business-tier users get one query per Meta Business. Personal-tier users
    fall back to `/me/adaccounts` with empty biz fields.
    """
    if not businesses:
        return [(f"{GRAPH_API_BASE}/me/adaccounts", "", "")]

    return [
        (f"{GRAPH_API_BASE}/{biz['id']}/owned_ad_accounts", biz["id"], biz.get("name", ""))
        for biz in businesses
    ]
