"""
Google OAuth handler — for YouTube (and future Google Ads), and Gmail.

Auth flow: standard OAuth 2.0 Authorization Code.
Auth URL: https://accounts.google.com/o/oauth2/v2/auth
Token exchange: https://oauth2.googleapis.com/token
Token refresh: same endpoint with grant_type=refresh_token

Access token: 1 hour. Refresh token: long-lived (no expiry as long as not revoked).

Scopes for YouTube:
  https://www.googleapis.com/auth/youtube.upload    — upload videos
  https://www.googleapis.com/auth/youtube.readonly   — read channel info + analytics
  https://www.googleapis.com/auth/userinfo.profile   — user name/picture

Scopes for Gmail:
  https://www.googleapis.com/auth/gmail.modify       — read + mark read
  https://www.googleapis.com/auth/gmail.send         — send emails

Discovery:
  YouTube: /youtube/v3/channels?mine=true → channel_id, title, handle
  Gmail:   /oauth2/v2/userinfo → gmail_address (same call as YouTube)
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

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_API = "https://www.googleapis.com/youtube/v3"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# YouTube-only scopes
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    # comments.insert / setModerationStatus (inbox comment replies) and
    # channel Analytics reports. Accounts connected before these were
    # added must reconnect to grant them.
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

# Gmail-only scopes
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

# Always included — shared identity scopes
IDENTITY_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
]

# Full scope list requested at consent — user can grant/deny YouTube and Gmail independently
GOOGLE_SCOPES = YOUTUBE_SCOPES + GMAIL_SCOPES + IDENTITY_SCOPES

ACCESS_TOKEN_LIFETIME = 3600  # 1 hour


def build_auth_url(client_name: str, state: str) -> str:
    app = get_msuite_app("Google")
    
    # Extract platform from state to selectively request scopes
    from msuite.constants import OAUTH_STATE_CACHE_PREFIX
    cached = frappe.cache.get_value(f"{OAUTH_STATE_CACHE_PREFIX}:{state}")
    platform = "google"
    if cached:
        platform = json.loads(cached).get("platform") or "google"

    if platform == "google_gmail":
        scopes = GMAIL_SCOPES + IDENTITY_SCOPES
    elif platform == "google_youtube":
        scopes = YOUTUBE_SCOPES + IDENTITY_SCOPES
    else:
        scopes = GOOGLE_SCOPES

    params = {
        "response_type": "code",
        "client_id": app.app_id,
        "redirect_uri": app.redirect_uri,
        "scope": " ".join(scopes),
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
        "platform": state_data.get("platform", "google"),
    }


def discover_accounts(client_name: str, token_data: dict) -> list[dict]:
    token = token_data["access_token"]
    expires_in = token_data.get("expires_in", ACCESS_TOKEN_LIFETIME)
    app_name = token_data.get("app_name", "")
    refresh_token = token_data.get("refresh_token", "")
    platform = token_data.get("platform", "google")
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

    # ── YouTube channel discovery ────────────────────────────────────────
    if platform in ["google", "google_youtube"]:
        try:
            channels = _get_youtube_channels(token)
            for ch in channels:
                channel_id = ch.get("id", "")
                snippet = ch.get("snippet", {})
                channel_title = snippet.get("title", "")
                channel_handle = snippet.get("customUrl", "")
                channel_avatar = ((snippet.get("thumbnails") or {}).get("default") or {}).get("url", "")

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
                    "avatar_url": channel_avatar,
                })
                connected.append({"platform": "YouTube", "name": channel_title})
        except Exception as e:
            logger.warning(f"YouTube discovery failed or skipped: {e}")

    # ── Gmail account discovery ──────────────────────────────────────────
    if google_email and platform in ["google", "google_gmail"]:
        upsert_connected_account(client_name, Platform.GMAIL, google_email, {
            "display_name": google_name or google_email,
            "auth_account": auth_name,
            "access_token": token,
            "refresh_token": refresh_token,   # stored encrypted on provider; NEVER pushed to client
            "token_type": "User Token",
            "msuite_app": app_name,
            "token_expiry": add_to_date(now(), seconds=expires_in),
        })

        # Push stripped credentials to client — no refresh_token, no app_secret.
        push_account_to_client(client_name, Platform.GMAIL, {
            "gmail_address": google_email,
            "google_account_id": google_id,
            "google_account_name": google_name or google_email,
            "access_token": token,
            "token_expires_at": str(add_to_date(now(), seconds=expires_in)),
            "avatar_url": user_info.get("picture", ""),
            # refresh_token intentionally omitted — provider holds it
        })
        connected.append({"platform": "Gmail", "name": google_email})

        # Register the Gmail push watch so Pub/Sub notifications start
        # flowing immediately (best-effort — polling still covers ingestion
        # when no gmail_pubsub_topic is configured or the call fails).
        try:
            from msuite.api.v1.gmail_relay import register_gmail_watch
            ca_name = frappe.db.get_value(
                "MSuite Connected Account",
                {"client": client_name, "platform": Platform.GMAIL, "account_id": google_email},
                "name",
            )
            if ca_name:
                register_gmail_watch(ca_name)
        except Exception as e:
            logger.warning(f"Gmail watch registration skipped for {google_email}: {e}")

    return connected



def refresh_token_fn(connected_account_name: str) -> None:
    """Refresh a Google access token using the refresh token.

    Raises on failure — callers depend on the exception:
      * gmail_relay returns TOKEN_REFRESH_FAILED instead of calling the
        Gmail API with a stale token;
      * refresh_all_tokens records the failure (and eventually flags
        needs_reauth + notifies the client) instead of counting a silent
        no-op as a success.
    """
    ca = frappe.get_doc("MSuite Connected Account", connected_account_name)
    stored_refresh = ca.get_password("refresh_token") if hasattr(ca, "refresh_token") and ca.refresh_token else ""
    if not stored_refresh:
        raise RuntimeError(f"No refresh token stored for {connected_account_name} — reconnect required")

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
        raise RuntimeError(
            f"Google token refresh failed: {data.get('error', 'unknown')} "
            f"{data.get('error_description', '')}".strip()
        )

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
