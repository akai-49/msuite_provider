"""
Google OAuth handler — for YouTube (and future Google Ads).

Auth flow: standard OAuth 2.0 Authorization Code.
Auth URL: https://accounts.google.com/o/oauth2/v2/auth
Token exchange: https://oauth2.googleapis.com/token
Token refresh: same endpoint with grant_type=refresh_token

Access token: 1 hour. Refresh token: long-lived (no expiry as long as not revoked).

Scopes for YouTube:
  https://www.googleapis.com/auth/youtube.upload    — upload videos
  https://www.googleapis.com/auth/youtube.readonly   — read channel info + analytics
  https://www.googleapis.com/auth/userinfo.profile   — user name/picture

Discovery: /youtube/v3/channels?mine=true → channel_id, title, handle
"""
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

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_API = "https://www.googleapis.com/youtube/v3"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
]

ACCESS_TOKEN_LIFETIME = 3600  # 1 hour


def build_auth_url(client_name: str, state: str) -> str:
    app = get_msuite_app("Google")
    params = {
        "response_type": "code",
        "client_id": app.app_id,
        "redirect_uri": app.redirect_uri,
        "scope": " ".join(GOOGLE_SCOPES),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_token(code: str, state_data: dict) -> dict:
    app = get_msuite_app("Google")
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": app.redirect_uri,
            "client_id": app.app_id,
            "client_secret": app.get_password("app_secret"),
        },
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        logger.error(f"Google token exchange failed: {data}")
        frappe.throw("Failed to exchange Google authorization code", TokenExchangeError)

    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_in": data.get("expires_in", ACCESS_TOKEN_LIFETIME),
        "app_name": app.name,
    }


def discover_accounts(client_name: str, token_data: dict) -> list[dict]:
    token = token_data["access_token"]
    expires_in = token_data.get("expires_in", ACCESS_TOKEN_LIFETIME)
    app_name = token_data.get("app_name", "")
    refresh_token = token_data.get("refresh_token", "")
    connected = []

    user_info = _get_user_info(token)
    google_id = user_info.get("id", "")
    google_name = user_info.get("name", "")
    google_email = user_info.get("email", "")

    if google_id:
        auth_name = upsert_auth_account(client_name, "Google", google_id, {
            "account_name": google_name or google_email,
            "profile_picture_url": user_info.get("picture", ""),
        })
    else:
        auth_name = None

    channels = _get_youtube_channels(token)
    for ch in channels:
        channel_id = ch.get("id", "")
        snippet = ch.get("snippet", {})
        channel_title = snippet.get("title", "")
        channel_handle = snippet.get("customUrl", "")

        upsert_connected_account(client_name, Platform.YOUTUBE, channel_id, {
            "display_name": channel_title,
            "auth_account": auth_name,
            "access_token": token,
            "token_type": "User Token",
            "msuite_app": app_name,
            "token_expiry": add_to_date(now(), seconds=expires_in),
        })

        push_account_to_client(client_name, Platform.YOUTUBE, {
            "channel_id": channel_id,
            "channel_title": channel_title,
            "channel_handle": channel_handle,
            "access_token": token,
            "refresh_token": refresh_token,
            "token_expires_at": str(add_to_date(now(), seconds=expires_in)),
            "google_account_id": google_id,
            "google_account_name": google_name or google_email,
        })
        connected.append({"platform": "YouTube", "name": channel_title})

    return connected


def refresh_token_fn(connected_account_name: str) -> None:
    """Refresh a Google access token using the refresh token."""
    ca = frappe.get_doc("MSuite Connected Account", connected_account_name)
    stored_refresh = ca.get_password("refresh_token") if hasattr(ca, "refresh_token") and ca.refresh_token else ""
    if not stored_refresh:
        return

    app = get_msuite_app("Google")
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": stored_refresh,
            "client_id": app.app_id,
            "client_secret": app.get_password("app_secret"),
        },
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        logger.warning(f"Google token refresh failed for {connected_account_name}: {data}")
        return

    ca.access_token = data["access_token"]
    ca.token_expiry = add_to_date(now(), seconds=data.get("expires_in", ACCESS_TOKEN_LIFETIME))
    ca.save(ignore_permissions=True)
    frappe.db.commit()
    logger.info(f"Google token refreshed for {connected_account_name}")


def _get_user_info(token: str) -> dict:
    try:
        resp = requests.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        return resp.json()
    except Exception as e:
        logger.error(f"Google userinfo failed: {e}")
        return {}


def _get_youtube_channels(token: str) -> list[dict]:
    try:
        resp = requests.get(
            f"{YT_API}/channels",
            headers={"Authorization": f"Bearer {token}"},
            params={"part": "snippet", "mine": "true"},
            timeout=30,
        )
        return resp.json().get("items", [])
    except Exception as e:
        logger.error(f"YouTube channel discovery failed: {e}")
        return []
