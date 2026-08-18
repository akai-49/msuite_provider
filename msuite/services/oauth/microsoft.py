"""
Microsoft OAuth handler — Outlook / Microsoft 365 mailboxes via Microsoft Graph.

Auth flow: standard OAuth 2.0 Authorization Code.
Auth URL:       https://login.microsoftonline.com/common/oauth2/v2.0/authorize
Token exchange: https://login.microsoftonline.com/common/oauth2/v2.0/token
Token refresh:  same endpoint with grant_type=refresh_token

Access token: ~1 hour. Refresh token: long-lived BUT rotated on every
refresh (see refresh_token_fn — this is the one real divergence from
google.py) and subject to a 90-day inactivity expiry.

`/common` is deliberate: it accepts BOTH Entra ID work/school accounts and
personal Microsoft accounts (outlook.com / hotmail.com / live.com).
`/organizations` would reject personal accounts and `/consumers` would
reject work/school ones — the product requires both. The Azure app
registration must match, with supported account types set to
"Accounts in any organizational directory and personal Microsoft accounts"
(AzureADandPersonalMicrosoftAccount).

Scopes (identical for both account types — every operation below is
documented as supported for `Delegated (personal Microsoft account)`):
  Mail.ReadWrite  — read mail + create/patch reply drafts (message: delta, createReply)
  Mail.Send       — send a draft (message: send)
  User.Read       — /me, to learn the mailbox address and display name
  offline_access  — issue a refresh token at all

Discovery:
  GET /me → id (oid), mail | userPrincipalName, displayName
"""
import json
import frappe
import requests
from frappe.utils import now, add_to_date
from urllib.parse import urlencode

from msuite.constants import MSUITE_LOGGER_NAME, Platform
from msuite.exceptions import TokenExchangeError

from .base import (
    get_msuite_app,
    upsert_auth_account,
    upsert_connected_account,
    push_account_to_client,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)

MS_AUTHORITY = "https://login.microsoftonline.com/common"
MS_AUTH_URL = f"{MS_AUTHORITY}/oauth2/v2.0/authorize"
MS_TOKEN_URL = f"{MS_AUTHORITY}/oauth2/v2.0/token"
GRAPH = "https://graph.microsoft.com/v1.0"

OUTLOOK_SCOPES = [
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/User.Read",
    "offline_access",
]

ACCESS_TOKEN_LIFETIME = 3600  # 1 hour


def build_auth_url(client_name: str, state: str) -> str:
    app = get_msuite_app("Microsoft")

    # Read the product key back out of the cached state the same way
    # google.build_auth_url does, so a future Microsoft product (Bing Ads)
    # can register the same builder instead of forking this module.
    from msuite.constants import OAUTH_STATE_CACHE_PREFIX
    cached = frappe.cache.get_value(f"{OAUTH_STATE_CACHE_PREFIX}:{state}")
    platform = "microsoft_outlook"
    if cached:
        platform = json.loads(cached).get("platform") or "microsoft_outlook"

    scopes = OUTLOOK_SCOPES

    params = {
        "response_type": "code",
        "client_id": app.app_id,
        "redirect_uri": app.redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "response_mode": "query",
        # select_account, not consent: a user with several mailboxes signed
        # in gets to pick which one they are connecting, and an already
        # consented account isn't re-prompted for permissions it granted.
        "prompt": "select_account",
    }
    return f"{MS_AUTH_URL}?{urlencode(params)}"


def exchange_token(code: str, state_data: dict) -> dict:
    app = get_msuite_app("Microsoft")
    resp = requests.post(
        MS_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": app.redirect_uri,
            "client_id": app.app_id,
            "client_secret": app.get_password("app_secret"),
            "scope": " ".join(OUTLOOK_SCOPES),
        },
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        logger.error(f"Microsoft token exchange failed: {data}")
        frappe.throw("Failed to exchange Microsoft authorization code", TokenExchangeError)

    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_in": data.get("expires_in", ACCESS_TOKEN_LIFETIME),
        "app_name": app.name,
        "platform": state_data.get("platform", "microsoft_outlook"),
    }


def discover_accounts(client_name: str, token_data: dict) -> list[dict]:
    token = token_data["access_token"]
    expires_in = token_data.get("expires_in", ACCESS_TOKEN_LIFETIME)
    app_name = token_data.get("app_name", "")
    refresh_token = token_data.get("refresh_token", "")
    connected = []

    me = _get_me(token)
    ms_user_id = me.get("id", "")
    display_name = me.get("displayName", "")
    # `mail` is null on some personal Microsoft accounts (and on work
    # accounts with no Exchange licence) — userPrincipalName is the fallback.
    address = (me.get("mail") or me.get("userPrincipalName") or "").strip()

    if not ms_user_id or not address:
        logger.error(
            f"Microsoft discovery got no usable identity for {client_name}: "
            f"id={ms_user_id!r} address={address!r}"
        )
        return connected

    # Keyed on the user's `id` (the oid) — NEVER on tenant id. Every personal
    # Microsoft account reports the same tenant (9188040d-6c67-4c5b-b112-
    # 36a304b66dad, the MSA tenant), and upsert_auth_account dedups on
    # (client, platform, account_id), so a tenant key would collapse all of a
    # client's personal mailboxes into one row and let each new connect
    # overwrite the last. google.py keys on the per-user google_id for the
    # same reason.
    auth_name = upsert_auth_account(client_name, "Microsoft", ms_user_id, {
        "account_name": display_name or address,
    })

    upsert_connected_account(client_name, Platform.OUTLOOK, address, {
        "display_name": display_name or address,
        "auth_account": auth_name,
        "access_token": token,
        "refresh_token": refresh_token,   # stored encrypted on provider; NEVER pushed to client
        "token_type": "User Token",
        "msuite_app": app_name,
        "token_expiry": add_to_date(now(), seconds=expires_in),
    })

    # Push stripped credentials to client — no refresh_token, no app_secret.
    push_account_to_client(client_name, Platform.OUTLOOK, {
        "outlook_address": address,
        "ms_user_id": ms_user_id,
        "display_name": display_name or address,
        "access_token": token,
        "token_expires_at": str(add_to_date(now(), seconds=expires_in)),
        # refresh_token intentionally omitted — provider holds it
    })
    connected.append({"platform": "Outlook", "name": address})

    # Register Graph push so notifications start immediately (best-effort —
    # the client's delta poll cron still covers ingestion when the
    # notification URL isn't publicly reachable or the call fails). Mirrors
    # how google.discover_accounts registers the Gmail watch.
    try:
        from msuite.services.mail.graph_subscriptions import create_subscription

        ca_name = frappe.db.get_value(
            "MSuite Connected Account",
            {"client": client_name, "platform": Platform.OUTLOOK, "account_id": address},
            "name",
        )
        if ca_name:
            create_subscription(ca_name)
    except Exception as e:
        logger.warning(f"Graph subscription registration skipped for {address}: {e}")

    return connected


def refresh_token_fn(connected_account_name: str) -> None:
    """Refresh a Microsoft access token using the refresh token.

    Raises on failure, matching google.refresh_token_fn — callers depend on
    the exception rather than a silent no-op (the mail relay must not call
    Graph with a stale token, and refresh_all_tokens must record the failure
    so it can eventually flag needs_reauth).

    Unlike Google, **Microsoft rotates the refresh token**: the response
    carries a NEW refresh_token that replaces the one we sent. Failing to
    persist it leaves the old token working only until it falls out of the
    rotation window, after which every refresh fails and the mailbox goes
    dark. This is why the two handlers can't share one refresher.
    """
    ca = frappe.get_doc("MSuite Connected Account", connected_account_name)
    stored_refresh = ca.get_password("refresh_token") if hasattr(ca, "refresh_token") and ca.refresh_token else ""
    if not stored_refresh:
        raise RuntimeError(f"No refresh token stored for {connected_account_name} — reconnect required")

    app = get_msuite_app("Microsoft")
    resp = requests.post(
        MS_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": stored_refresh,
            "client_id": app.app_id,
            "client_secret": app.get_password("app_secret"),
            "scope": " ".join(OUTLOOK_SCOPES),
        },
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        logger.warning(f"Microsoft token refresh failed for {connected_account_name}: {data}")
        raise RuntimeError(
            f"Microsoft token refresh failed: {data.get('error', 'unknown')} "
            f"{data.get('error_description', '')}".strip()
        )

    ca.access_token = data["access_token"]
    ca.token_expiry = add_to_date(now(), seconds=data.get("expires_in", ACCESS_TOKEN_LIFETIME))
    # Persist the rotated refresh token. Guarded because Microsoft only
    # omits it in edge cases — when it's absent the previous one stays valid.
    if data.get("refresh_token"):
        ca.refresh_token = data["refresh_token"]
    ca.save(ignore_permissions=True)
    frappe.db.commit()
    logger.info(f"Microsoft token refreshed for {connected_account_name}")


def _get_me(token: str) -> dict:
    try:
        resp = requests.get(
            f"{GRAPH}/me",
            headers={"Authorization": f"Bearer {token}"},
            params={"$select": "id,mail,userPrincipalName,displayName"},
            timeout=30,
        )
        return resp.json()
    except Exception as e:
        logger.error(f"Microsoft /me lookup failed: {e}")
        return {}
