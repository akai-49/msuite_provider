"""
X (Twitter) OAuth handler — OAuth 2.0 with PKCE.

Auth URL: https://x.com/i/oauth2/authorize (NOT twitter.com)
Token exchange: https://api.x.com/2/oauth2/token
Token refresh: same endpoint with grant_type=refresh_token

Token: access token has no expiry (until revoked).
Refresh token: 6-month validity.

Scopes: tweet.read, tweet.write, users.read, offline.access, media.write
"""
import hashlib
import secrets

import frappe
import requests
from frappe.utils import now
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

X_AUTH_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_API = "https://api.x.com/2"

X_SCOPES = [
    "tweet.read",
    "tweet.write",
    "users.read",
    "offline.access",
    "media.write",
]


def build_auth_url(client_name: str, state: str) -> str:
    """Build X OAuth 2.0 PKCE authorization URL.

    PKCE requires code_verifier (stored in cache) and code_challenge
    (sent to X). The verifier is retrieved during token exchange.
    """
    app = get_msuite_app("Twitter")

    code_verifier = secrets.token_urlsafe(64)[:128]
    code_challenge = hashlib.sha256(code_verifier.encode()).hexdigest()

    frappe.cache.set_value(
        f"msuite:x_pkce:{state}",
        code_verifier,
        expires_in_sec=600,
    )

    params = {
        "response_type": "code",
        "client_id": app.app_id,
        "redirect_uri": app.redirect_uri,
        "scope": " ".join(X_SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{X_AUTH_URL}?{urlencode(params)}"


def exchange_token(code: str, state_data: dict) -> dict:
    """Exchange authorization code for access + refresh tokens.

    Retrieves the PKCE code_verifier from cache (stored during build_auth_url).
    """
    app = get_msuite_app("Twitter")
    state = state_data.get("state", "")

    code_verifier = frappe.cache.get_value(f"msuite:x_pkce:{state}")
    if not code_verifier:
        frappe.throw("PKCE code_verifier expired. Please retry.", TokenExchangeError)
    frappe.cache.delete_value(f"msuite:x_pkce:{state}")

    resp = requests.post(
        X_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": app.redirect_uri,
            "client_id": app.app_id,
            "code_verifier": code_verifier,
        },
        auth=(app.app_id, app.get_password("app_secret")),
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        logger.error(f"X token exchange failed: {data}")
        frappe.throw("Failed to exchange X authorization code", TokenExchangeError)

    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_in": data.get("expires_in", 0),
        "app_name": app.name,
    }


def discover_accounts(client_name: str, token_data: dict) -> list[dict]:
    token = token_data["access_token"]
    app_name = token_data.get("app_name", "")
    refresh_token = token_data.get("refresh_token", "")
    connected = []

    user = _get_current_user(token)
    if not user:
        return connected

    user_id = user.get("id", "")
    username = user.get("username", "")
    name = user.get("name", username)
    avatar = user.get("profile_image_url", "")

    auth_name = upsert_auth_account(client_name, "Twitter", user_id, {
        "account_name": name,
        "profile_picture_url": avatar,
    })

    upsert_connected_account(client_name, Platform.TWITTER, user_id, {
        "display_name": f"@{username}",
        "auth_account": auth_name,
        "access_token": token,
        "token_type": "User Token",
        "msuite_app": app_name,
    })

    push_account_to_client(client_name, Platform.TWITTER, {
        "user_id": user_id,
        "name": name,
        "username": username,
        "access_token": token,
        "refresh_token": refresh_token,
        "avatar_url": avatar,
    })
    connected.append({"platform": "Twitter", "name": f"@{username}"})

    return connected


def refresh_token_fn(connected_account_name: str) -> None:
    """Refresh an X access token using the refresh token."""
    ca = frappe.get_doc("MSuite Connected Account", connected_account_name)
    stored_refresh = ca.get_password("refresh_token") if hasattr(ca, "refresh_token") and ca.refresh_token else ""
    if not stored_refresh:
        return

    app = get_msuite_app("Twitter")
    resp = requests.post(
        X_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": stored_refresh,
            "client_id": app.app_id,
        },
        auth=(app.app_id, app.get_password("app_secret")),
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        logger.warning(f"X token refresh failed for {connected_account_name}: {data}")
        return

    ca.access_token = data["access_token"]
    ca.save(ignore_permissions=True)
    frappe.db.commit()
    logger.info(f"X token refreshed for {connected_account_name}")


def _get_current_user(token: str) -> dict:
    try:
        resp = requests.get(
            f"{X_API}/users/me",
            headers={"Authorization": f"Bearer {token}"},
            params={"user.fields": "id,name,username,profile_image_url"},
            timeout=30,
        )
        return resp.json().get("data", {})
    except Exception as e:
        logger.error(f"X user lookup failed: {e}")
        return {}
