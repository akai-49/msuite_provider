"""
Meta Social OAuth handler — Facebook Pages + Instagram Business (organic).

This handler covers organic posting only. Meta Ads lives in a sibling
handler (`meta_ads.py`) so each MSuite product (SOCIAL vs ADS) can
request exactly the scopes its features need. Both handlers authenticate
against the same Meta app — see `meta_base.py` for the shared flow.

Flow:
  1. build_auth_url()        → Facebook OAuth dialog with Pages + IG scopes
  2. exchange_token()        → short → long-lived user token
  3. discover_accounts()     → /me/businesses → /me/accounts → linked IG
  4. Upsert MSuite Auth + Connected + platform-specific Facebook/Instagram
     Account rows, push credentials downstream to the client.

Scope note (important, easy to get wrong):
  Uses the `instagram_*` scope family (not `instagram_business_*`).
  The `_business_` prefixed names only exist in the separate
  Instagram Business Login flow (instagram.com/oauth/authorize) and are
  rejected by Facebook Login with "Invalid Scopes".
"""
import frappe
import requests
from frappe.utils import add_to_date, now

from msuite.constants import GRAPH_API_BASE, MSUITE_LOGGER_NAME, Platform

from .base import (
    push_account_to_client,
    upsert_connected_account,
    upsert_facebook_account,
    upsert_instagram_account,
)
from .meta_base import (
    LONG_LIVED_TOKEN_TTL,
    build_facebook_login_url,
    discover_businesses,
    exchange_code_for_long_lived_token,
    upsert_businesses_as_auth_accounts,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)

META_SOCIAL_SCOPES = [
    "pages_manage_posts",
    "pages_read_engagement",
    "pages_show_list",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_comments",
    "instagram_manage_insights",
]


# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------


def build_auth_url(client_name: str, state: str) -> str:
    """Facebook OAuth dialog URL scoped to Pages + Instagram."""
    return build_facebook_login_url(META_SOCIAL_SCOPES, state)


def exchange_token(code: str, state_data: dict) -> dict:
    """Code → long-lived user token (shared with meta_ads)."""
    return exchange_code_for_long_lived_token(code)


def discover_accounts(client_name: str, token_data: dict) -> list[dict]:
    """Discover Facebook Pages + linked Instagram Business accounts."""
    user_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", LONG_LIVED_TOKEN_TTL)
    app_name   = token_data.get("app_name", "")

    businesses   = discover_businesses(user_token)
    biz_auth_map = upsert_businesses_as_auth_accounts(client_name, businesses)
    page_biz_map = _build_page_business_map(businesses, user_token, biz_auth_map)

    return _discover_pages_and_instagram(
        client_name, user_token, app_name, expires_in, page_biz_map
    )


# ---------------------------------------------------------------------------
# Page ↔ Business mapping
# ---------------------------------------------------------------------------


def _build_page_business_map(
    businesses: list[dict],
    user_token: str,
    biz_auth_map: dict[str, str],
) -> dict[str, dict]:
    """Map `page_name` → `{business_id, business_name, auth_account}`.

    Uses `/{biz_id}/owned_pages` for grouping only. Page access tokens
    must still come from `/me/accounts` (the owned_pages endpoint does
    not return them).
    """
    page_biz_map: dict[str, dict] = {}
    for biz in businesses:
        try:
            resp = requests.get(
                f"{GRAPH_API_BASE}/{biz['id']}/owned_pages",
                params={"access_token": user_token, "fields": "id,name"},
                timeout=30,
            )
            for p in resp.json().get("data", []):
                page_name = p.get("name", "")
                if page_name:
                    page_biz_map[page_name] = {
                        "business_id":   biz["id"],
                        "business_name": biz.get("name", ""),
                        "auth_account":  biz_auth_map.get(biz["id"]),
                    }
        except Exception as e:
            logger.warning(f"owned_pages lookup failed for biz {biz['id']}: {e}")
    return page_biz_map


# ---------------------------------------------------------------------------
# Page + Instagram discovery
# ---------------------------------------------------------------------------


def _discover_pages_and_instagram(
    client_name: str,
    user_token: str,
    app_name: str,
    expires_in: int,
    page_biz_map: dict,
) -> list[dict]:
    """Discover Facebook Pages via `/me/accounts` and their linked IG."""
    connected: list[dict] = []

    try:
        resp = requests.get(
            f"{GRAPH_API_BASE}/me/accounts",
            params={
                "access_token": user_token,
                "fields":       "id,name,access_token,category",
                "limit":        200,
            },
            timeout=30,
        )

        for page in resp.json().get("data", []):
            page_id    = page["id"]
            page_name  = page.get("name", "")
            page_token = page.get("access_token", "")

            biz_info     = page_biz_map.get(page_name, {})
            biz_id       = biz_info.get("business_id", "")
            biz_name     = biz_info.get("business_name", "")
            auth_account = biz_info.get("auth_account")

            ca_name = upsert_connected_account(
                client_name, Platform.FACEBOOK, page_id, {
                    "display_name":  page_name,
                    "auth_account":  auth_account,
                    "access_token":  page_token,
                    "token_type":    "Page Token",
                    "msuite_app":    app_name,
                    "token_expiry":  add_to_date(now(), seconds=expires_in),
                },
            )
            upsert_facebook_account(ca_name, {
                "page_id":       page_id,
                "page_name":     page_name,
                "page_category": page.get("category", ""),
            })
            connected.append({"platform": "Facebook", "name": page_name})

            push_account_to_client(client_name, Platform.FACEBOOK, {
                "page_id":           page_id,
                "page_name":         page_name,
                "page_access_token": page_token,
                "business_id":       biz_id,
                "business_name":     biz_name,
            })

            ig_result = _discover_instagram_for_page(
                client_name, page_id, page_name, page_token, user_token,
                app_name, expires_in, biz_id, auth_account,
            )
            if ig_result:
                connected.append(ig_result)

    except Exception as e:
        logger.error(f"Failed to discover Facebook Pages: {e}")

    return connected


def _discover_instagram_for_page(
    client_name: str,
    page_id: str,
    page_name: str,
    page_token: str,
    user_token: str,
    app_name: str,
    expires_in: int,
    biz_id: str,
    auth_account: str | None = None,
) -> dict | None:
    """Check a page for a linked Instagram Business Account; upsert + push."""
    try:
        resp = requests.get(
            f"{GRAPH_API_BASE}/{page_id}",
            params={
                "fields":       "instagram_business_account{id,username,name}",
                "access_token": page_token or user_token,
            },
            timeout=30,
        )
        ig_account = resp.json().get("instagram_business_account")
        if not ig_account:
            return None

        ig_id       = ig_account["id"]
        ig_username = ig_account.get("username", "")
        ig_name     = ig_account.get("name", ig_username)

        ca_name = upsert_connected_account(
            client_name, Platform.INSTAGRAM, ig_id, {
                "display_name":  f"@{ig_username}" if ig_username else ig_name,
                "auth_account":  auth_account,
                "access_token":  page_token,
                "token_type":    "Page Token",
                "msuite_app":    app_name,
                "token_expiry":  add_to_date(now(), seconds=expires_in),
            },
        )
        upsert_instagram_account(ca_name, {
            "instagram_user_id": ig_id,
            "username":          ig_username,
            "name_field":        ig_name,
            "linked_page_id":    page_id,
            "linked_page_name":  page_name,
        })

        push_account_to_client(client_name, Platform.INSTAGRAM, {
            "user_id":          ig_id,
            "username":         ig_username,
            "access_token":     page_token,
            "linked_page_id":   page_id,
            "linked_page_name": page_name,
            "business_id":      biz_id,
        })

        return {"platform": "Instagram", "name": f"@{ig_username}"}

    except Exception as e:
        logger.error(f"Instagram check failed for page {page_id}: {e}")
        return None
