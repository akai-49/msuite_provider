"""
Shared helpers for Meta Facebook-Login OAuth flows.

Both `meta_social` (Pages + Instagram, organic) and `meta_ads` (Ad Accounts)
authenticate against the *same* Meta app via Facebook Login for Business.
Each handler requests a different scope subset and discovers a different
asset class, but the auth-URL construction, token exchange, and business
discovery are identical — they live here.

Design rule: nothing in this module is scope-aware. Handlers pass their
scope list to `build_facebook_login_url` and their token to the discovery
helpers; this file has no knowledge of Pages, Instagram, or Ads.

Not a handler itself — never registered in the OAuth registry. Only
imported by the platform-specific handlers.
"""
from urllib.parse import urlencode

import frappe
import requests

from msuite.constants import (
    GRAPH_API_BASE,
    GRAPH_API_VERSION,
    MSUITE_LOGGER_NAME,
)
from msuite.exceptions import TokenExchangeError

from .base import get_msuite_app, upsert_auth_account

logger = frappe.logger(MSUITE_LOGGER_NAME)

# Single MSuite App record backs both social and ads flows.
META_APP_PLATFORM = "Meta Social"

# Meta's long-lived token lifetime in seconds (~60 days).
LONG_LIVED_TOKEN_TTL = 5_184_000


# ---------------------------------------------------------------------------
# Auth URL
# ---------------------------------------------------------------------------


def build_facebook_login_url(scopes: list[str], state: str) -> str:
    """Build the Facebook OAuth dialog URL for an arbitrary scope list.

    Callers own their scope list so Meta's consent dialog only requests
    what the product actually uses.
    """
    app = get_msuite_app(META_APP_PLATFORM)
    params = {
        "client_id":     app.app_id,
        "redirect_uri":  app.redirect_uri,
        "state":         state,
        "scope":         ",".join(scopes),
        "response_type": "code",
    }
    return (
        f"https://www.facebook.com/{GRAPH_API_VERSION}"
        f"/dialog/oauth?{urlencode(params)}"
    )


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------


def refresh_long_lived_token(account_name: str) -> None:
    """In-place extension of a Meta long-lived user token.

    Meta does not issue refresh tokens. Instead, an unexpired long-lived
    token (60-day lifetime) can be exchanged for a NEW long-lived token
    via `grant_type=fb_exchange_token`, which resets the 60-day clock.
    Run this once every ~50 days per account and the customer's
    publishing flow never sees a token-expired error.

    Failure modes:
      • Token already expired → Meta returns 4xx with error code 190;
        we let the exception bubble so the cron marks `needs_reauth=1`.
      • App credentials misconfigured → same error path; needs ops
        attention regardless.

    Caller is `services.oauth.refresh.refresh_all_tokens` which writes
    the new access_token + token_expiry back onto the Connected Account
    and pushes the updated credentials to the client site.
    """
    ca = frappe.get_doc("MSuite Connected Account", account_name)
    current = ca.get_password("access_token") if ca.access_token else ""
    if not current:
        frappe.throw("No access_token to refresh", TokenExchangeError)

    app = get_msuite_app(META_APP_PLATFORM)
    app_secret = app.get_password("app_secret")

    refreshed = _request_token({
        "grant_type":        "fb_exchange_token",
        "client_id":         app.app_id,
        "client_secret":     app_secret,
        "fb_exchange_token": current,
    })

    # `expires_in` is in seconds; fall back to the documented 60-day
    # lifetime when Meta omits it (newer endpoints sometimes do).
    expires_in = int(refreshed.get("expires_in") or LONG_LIVED_TOKEN_TTL)
    ca.access_token = refreshed["access_token"]
    ca.token_expiry = frappe.utils.add_to_date(
        frappe.utils.now_datetime(), seconds=expires_in
    )
    ca.last_refreshed = frappe.utils.now_datetime()
    ca.save(ignore_permissions=True)


def exchange_code_for_long_lived_token(code: str) -> dict:
    """Code → short-lived → long-lived (60 days).

    Falls back to the short-lived token if the second hop fails: callers
    still get a usable access_token and can prompt a reconnect later.
    """
    app = get_msuite_app(META_APP_PLATFORM)
    app_secret = app.get_password("app_secret")

    short = _request_token({
        "client_id":     app.app_id,
        "client_secret": app_secret,
        "redirect_uri":  app.redirect_uri,
        "code":          code,
    })
    short_token = short["access_token"]

    try:
        long_lived = _request_token({
            "grant_type":        "fb_exchange_token",
            "client_id":         app.app_id,
            "client_secret":     app_secret,
            "fb_exchange_token": short_token,
        })
    except TokenExchangeError:
        logger.warning("Long-lived token exchange failed; using short-lived")
        return {
            "access_token": short_token,
            "expires_in":   short.get("expires_in", 3600),
            "app_name":     app.name,
        }

    return {
        "access_token": long_lived["access_token"],
        "expires_in":   long_lived.get("expires_in", LONG_LIVED_TOKEN_TTL),
        "app_name":     app.name,
    }


def _request_token(params: dict) -> dict:
    """POST Meta's token endpoint; raise on missing access_token."""
    resp = requests.get(
        f"{GRAPH_API_BASE}/oauth/access_token",
        params=params,
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        logger.error(f"Meta token exchange failed: {data}")
        frappe.throw("Failed to exchange authorization code", TokenExchangeError)
    return data


# ---------------------------------------------------------------------------
# Business discovery (shared between organic + ads flows)
# ---------------------------------------------------------------------------


def discover_businesses(user_token: str) -> list[dict]:
    """Fetch `/me/businesses` — Meta Business accounts the user manages.

    Safe on failure: returns []. Callers should treat an empty result as
    "personal / non-business account" and fall back to `/me/accounts` or
    `/me/adaccounts` accordingly.
    """
    try:
        resp = requests.get(
            f"{GRAPH_API_BASE}/me/businesses",
            params={
                "access_token": user_token,
                "fields":       "id,name,profile_picture_uri,verification_status",
            },
            timeout=30,
        )
        return resp.json().get("data", [])
    except Exception as e:
        logger.error(f"Failed to discover Meta businesses: {e}")
        return []


def upsert_businesses_as_auth_accounts(
    client_name: str,
    businesses: list[dict],
) -> dict[str, str]:
    """Create/update MSuite Auth Accounts for each business.

    Returns `{business_id: auth_account_name}` so downstream Connected
    Account rows can link back to their parent business.
    """
    mapping: dict[str, str] = {}
    for biz in businesses:
        auth_name = upsert_auth_account(client_name, "Meta", biz["id"], {
            "account_name":        biz.get("name", ""),
            "verification_status": biz.get("verification_status", ""),
            "profile_picture_url": biz.get("profile_picture_uri", ""),
        })
        mapping[biz["id"]] = auth_name
    return mapping


# ---------------------------------------------------------------------------
# Debug Token / Granular Scope inspection
# ---------------------------------------------------------------------------


def inspect_debug_token(user_token: str) -> dict:
    """Call debug_token to inspect granted permissions and granular_scopes target_ids.

    When users select specific pages/ad accounts/businesses in Meta's OAuth dialog,
    Meta returns their IDs in `granular_scopes[].target_ids`.

    Returns dict with:
      "data": raw response dict from debug_token
      "target_ids": set of string asset IDs explicitly selected by the user
    """
    try:
        app = get_msuite_app(META_APP_PLATFORM)
        app_secret = app.get_password("app_secret")
        resp = requests.get(
            f"{GRAPH_API_BASE}/debug_token",
            params={
                "input_token":  user_token,
                "access_token": f"{app.app_id}|{app_secret}",
            },
            timeout=30,
        )
        data = resp.json().get("data", {})
        target_ids: set[str] = set()
        for item in data.get("granular_scopes", []):
            for tid in item.get("target_ids", []):
                if tid:
                    tid_str = str(tid)
                    target_ids.add(tid_str)
                    if tid_str.isdigit():
                        target_ids.add(f"act_{tid_str}")
                    elif tid_str.startswith("act_"):
                        target_ids.add(tid_str[4:])
        return {"data": data, "target_ids": target_ids}
    except Exception as e:
        logger.error(f"debug_token inspection failed: {e}")
        return {"data": {}, "target_ids": set()}

