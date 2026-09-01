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
    inspect_debug_token,
    upsert_businesses_as_auth_accounts,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)

META_ADS_SCOPES = [
    "ads_management",
    "ads_read",
    "business_management",
]


# Meta Ad Account status codes
# 1 = ACTIVE, 2 = DISABLED, 3 = UNSETTLED, 7 = PENDING_RISK_REVIEW, 100 = CLOSURE_PENDING, 101 = CLOSED
DISABLED_ACCOUNT_STATUSES = {2, 100, 101}


# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------


def build_auth_url(client_name: str, state: str) -> str:
    """Facebook OAuth dialog URL scoped to Ads only."""
    return build_facebook_login_url(META_ADS_SCOPES, state)


def exchange_token(code: str, state_data: dict) -> dict:
    """Code → long-lived user token (shared with meta_social)."""
    token_data = exchange_code_for_long_lived_token(code)
    if state_data:
        token_data["state_data"] = state_data
    return token_data


def discover_accounts(client_name: str, token_data: dict) -> list[dict]:
    """Discover Meta Ad Accounts across the user's Businesses."""
    user_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", LONG_LIVED_TOKEN_TTL)
    app_name   = token_data.get("app_name", "")
    state_data = token_data.get("state_data") or {}

    target_business_id = state_data.get("business_id") or state_data.get("target_business_id")

    # Inspect token to check if user selected specific ad accounts / assets in Meta's OAuth dialog
    debug_info = inspect_debug_token(user_token)
    ad_target_ids = debug_info.get("ad_target_ids") or set()
    business_target_ids = debug_info.get("business_target_ids") or set()
    granular_target_ids = debug_info.get("target_ids") or set()

    businesses = discover_businesses(user_token)

    # If user selected specific businesses in Meta's consent popup, filter business list
    if business_target_ids:
        businesses = [b for b in businesses if str(b.get("id")) in business_target_ids]

    biz_auth_map = upsert_businesses_as_auth_accounts(client_name, businesses)

    return _discover_ad_accounts(
        client_name,
        user_token,
        app_name,
        expires_in,
        businesses,
        biz_auth_map,
        target_business_id=target_business_id,
        ad_target_ids=ad_target_ids,
        business_target_ids=business_target_ids,
        granular_target_ids=granular_target_ids,
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
    target_business_id: str | None = None,
    ad_target_ids: set[str] | None = None,
    business_target_ids: set[str] | None = None,
    granular_target_ids: set[str] | None = None,
) -> list[dict]:
    """Upsert active Ad Accounts the user can access + push to client site."""
    connected: list[dict] = []
    # Key: account_id -> {account_id, name, currency, timezone_name, business_id, business_name}
    discovered: dict[str, dict] = {}

    def _collect_from_url(url: str, src_biz_id: str, src_biz_name: str):
        try:
            resp = requests.get(
                url,
                params={
                    "access_token": user_token,
                    "fields":       "account_id,name,currency,timezone_name,business,account_status,user_tasks",
                },
                timeout=30,
            )
            if resp.status_code != 200:
                logger.warning(f"Ad account discovery at {url} returned HTTP {resp.status_code}: {resp.text}")
                return
        except Exception as e:
            logger.error(f"Failed to discover ad accounts at {url}: {e}")
            return

        for ad in resp.json().get("data", []):
            account_id   = ad.get("account_id", "")
            account_name = ad.get("name", "")
            status       = ad.get("account_status")
            user_tasks   = ad.get("user_tasks", []) or []

            if not account_id:
                continue

            # Skip disabled or closed ad accounts
            if status in DISABLED_ACCOUNT_STATUSES:
                logger.info(f"Skipping ad account {account_id} ({account_name}): status {status} is disabled/closed.")
                continue

            # Skip read-only accounts where user cannot advertise or manage
            if user_tasks and not any(t in user_tasks for t in ("ADVERTISE", "MANAGE", "DRAFT")):
                logger.info(f"Skipping ad account {account_id} ({account_name}): user_tasks {user_tasks} has only read-only access.")
                continue

            # Skip accounts explicitly labelled read-only
            if "(read-only)" in account_name.lower() or "[read-only]" in account_name.lower():
                logger.info(f"Skipping read-only ad account {account_id} ({account_name}).")
                continue

            ad_biz = ad.get("business") or {}
            biz_id = src_biz_id or str(ad_biz.get("id") or "")
            biz_name = src_biz_name or ad_biz.get("name") or ""

            # If user selected specific ad accounts in Meta's consent popup, restrict strictly to those IDs
            if ad_target_ids:
                matches_account = (
                    account_id in ad_target_ids or
                    f"act_{account_id}" in ad_target_ids or
                    (account_id.startswith("act_") and account_id[4:] in ad_target_ids)
                )
                if not matches_account:
                    logger.info(f"Skipping ad account {account_id} ({account_name}): not selected by user in Meta consent dialog.")
                    continue
            elif business_target_ids:
                matches_business = bool(biz_id and biz_id in business_target_ids)
                if not matches_business:
                    logger.info(f"Skipping ad account {account_id} ({account_name}): business {biz_id} not in selected businesses.")
                    continue

            # If target business specified, filter out ad accounts from other businesses
            if target_business_id and biz_id and biz_id != target_business_id:
                continue

            if account_id not in discovered:
                discovered[account_id] = {
                    "account_id":      account_id,
                    "account_name":    account_name,
                    "currency":        ad.get("currency", ""),
                    "timezone_name":   ad.get("timezone_name", ""),
                    "business_id":     biz_id,
                    "business_name":   biz_name,
                }
            else:
                # Enrich business metadata if previously missing
                if biz_id and not discovered[account_id]["business_id"]:
                    discovered[account_id]["business_id"] = biz_id
                    discovered[account_id]["business_name"] = biz_name

    # 1. Query owned_ad_accounts for each business
    for biz in businesses:
        biz_id = biz.get("id")
        biz_name = biz.get("name", "")
        if biz_id:
            url = f"{GRAPH_API_BASE}/{biz_id}/owned_ad_accounts"
            _collect_from_url(url, str(biz_id), biz_name)

    # 2. Query client_ad_accounts for each business
    for biz in businesses:
        biz_id = biz.get("id")
        biz_name = biz.get("name", "")
        if biz_id:
            url = f"{GRAPH_API_BASE}/{biz_id}/client_ad_accounts"
            _collect_from_url(url, str(biz_id), biz_name)

    # 3. Query direct/personal ad accounts from /me/adaccounts
    url = f"{GRAPH_API_BASE}/me/adaccounts"
    _collect_from_url(url, "", "")

    # 4. If granular_target_ids has ad account IDs that weren't discovered yet, query /act_{id} directly
    if granular_target_ids:
        for tid in granular_target_ids:
            clean_id = tid[4:] if tid.startswith("act_") else tid
            if clean_id not in discovered:
                act_endpoint = f"{GRAPH_API_BASE}/act_{clean_id}"
                try:
                    resp = requests.get(
                        act_endpoint,
                        params={
                            "access_token": user_token,
                            "fields": "account_id,name,currency,timezone_name,business,account_status,user_tasks",
                        },
                        timeout=15,
                    )
                    if resp.status_code == 200:
                        ad = resp.json()
                        account_name = ad.get("name") or f"Ad Account {clean_id}"
                        user_tasks = ad.get("user_tasks", []) or []
                        if ad.get("account_id") and ad.get("account_status") not in DISABLED_ACCOUNT_STATUSES:
                            if user_tasks and not any(t in user_tasks for t in ("ADVERTISE", "MANAGE", "DRAFT")):
                                continue
                            if "(read-only)" in account_name.lower() or "[read-only]" in account_name.lower():
                                continue
                            ad_biz = ad.get("business") or {}
                            discovered[clean_id] = {
                                "account_id": clean_id,
                                "account_name": account_name,
                                "currency": ad.get("currency", ""),
                                "timezone_name": ad.get("timezone_name", ""),
                                "business_id": str(ad_biz.get("id") or ""),
                                "business_name": ad_biz.get("name") or "",
                            }
                except Exception as e:
                    logger.warning(f"Direct ad account fetch for {act_endpoint} failed: {e}")

    # Upsert discovered active accounts
    for acc in discovered.values():
        account_id   = acc["account_id"]
        account_name = acc["account_name"]
        biz_id       = acc["business_id"]
        biz_name     = acc["business_name"]

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
            "currency":        acc["currency"],
            "timezone":        acc["timezone_name"],
        })
        connected.append({"platform": "Meta Ads", "name": account_name})

        push_account_to_client(client_name, Platform.META_ADS, {
            "ad_account_id":   account_id,
            "ad_account_name": account_name,
            "access_token":    user_token,
            "currency":        acc["currency"],
            "timezone":        acc["timezone_name"],
            "business_id":     biz_id,
            "business_name":   biz_name,
        })

    return connected


