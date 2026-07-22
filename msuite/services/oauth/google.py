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

# Google Ads-only scopes
GOOGLE_ADS_SCOPES = [
    "https://www.googleapis.com/auth/adwords",
]

# Always included — shared identity scopes
IDENTITY_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
]

# Full scope list requested at consent — user can grant/deny YouTube and Gmail independently
GOOGLE_SCOPES = YOUTUBE_SCOPES + GMAIL_SCOPES + GOOGLE_ADS_SCOPES + IDENTITY_SCOPES

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
    elif platform == "google_ads":
        scopes = GOOGLE_ADS_SCOPES + IDENTITY_SCOPES
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

    # ── Google Ads account discovery ────────────────────────────────────
    if platform in ["google", "google_ads"]:
        try:
            ads_customers = _get_google_ads_customers(token)
            for cust in ads_customers:
                cid = cust["account_id"]
                cname = cust["account_name"]

                upsert_connected_account(client_name, "Google Ads", cid, {
                    "display_name": cname,
                    "auth_account": auth_name,
                    "access_token": token,
                    "refresh_token": refresh_token,
                    "token_type": "User Token",
                    "msuite_app": app_name,
                    "token_expiry": add_to_date(now(), seconds=expires_in),
                })

                push_account_to_client(client_name, "Google Ads", {
                    "ad_account_id": cid,
                    "ad_account_name": cname,
                    "access_token": token,
                    "token_expires_at": str(add_to_date(now(), seconds=expires_in)),
                    "google_account_id": google_id,
                    "google_account_name": google_name or google_email,
                })
                connected.append({"platform": "Google Ads", "name": cname})
        except Exception as e:
            logger.warning(f"Google Ads discovery failed: {e}")

    return connected


# Google Ads REST API version — update when Google releases a new stable version.
# v17 was sunset (returns 404). v20+ returns proper JSON errors.
GOOGLE_ADS_API_VERSION = "v20"


def _get_google_ads_customers(token: str) -> list[dict]:
    """Discover all accessible Google Ads customer accounts.

    Strategy:
    1. Call listAccessibleCustomers → returns all accounts where the OAuth user
       has DIRECT access (both standalone and manager/MCC accounts).
    2. For each account ID discovered (plus any manually configured MCC IDs from
       site_config), run a GAQL customer_client query to expand the full sub-account
       hierarchy.
    3. Deduplicate and return all leaf (non-manager) ad accounts found.
    """
    dev_token = (frappe.conf.get("google_ads_developer_token") or "").strip()
    if not dev_token:
        logger.warning("No google_ads_developer_token in site_config — skipping Google Ads customer discovery")
        return []

    base_url = f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}"
    headers = {
        "Authorization": f"Bearer {token}",
        "developer-token": dev_token,
        "Content-Type": "application/json",
    }

    # ── Step 1: Get directly accessible resource names ─────────────────────
    accessible_ids: list[str] = []
    try:
        resp = requests.get(
            f"{base_url}/customers:listAccessibleCustomers",
            headers=headers,
            timeout=30,
        )
        logger.info(f"Google Ads listAccessibleCustomers → status {resp.status_code}")
        if resp.status_code == 200:
            names = resp.json().get("resourceNames", [])
            logger.info(f"Google Ads resourceNames: {names}")
            accessible_ids = [r.replace("customers/", "").strip() for r in names if r]
        else:
            logger.error(f"Google Ads listAccessibleCustomers {resp.status_code}: {resp.text[:500]}")
    except Exception as e:
        logger.error(f"Google Ads listAccessibleCustomers failed: {e}")

    # ── Step 2: Add manually configured MCC IDs from site_config ──────────
    # Allows admin to seed discovery even if listAccessibleCustomers returns empty.
    # In site_config.json: "google_ads_mcc_customer_id": "8425847703"
    mcc_from_config = (frappe.conf.get("google_ads_mcc_customer_id") or "").strip()
    if mcc_from_config:
        cid_clean = mcc_from_config.replace("-", "").strip()
        if cid_clean and cid_clean not in accessible_ids:
            logger.info(f"Adding MCC customer ID from site_config: {cid_clean}")
            accessible_ids.append(cid_clean)

    if not accessible_ids:
        logger.warning("Google Ads: no accessible customer IDs found. Ensure the Google account has direct access to at least one Ads account.")
        return []

    # ── Step 3: For each accessible account, expand hierarchy via GAQL ─────
    all_customers: dict[str, dict] = {}
    for seed_cid in accessible_ids:
        # First record the seed account itself
        if seed_cid not in all_customers:
            all_customers[seed_cid] = {
                "account_id": seed_cid,
                "account_name": f"Google Ads ({seed_cid})",
            }

        # Query customer_client to get sub-accounts under this customer
        try:
            gaql = (
                "SELECT customer_client.client_customer, customer_client.descriptive_name, "
                "customer_client.manager, customer_client.status, customer_client.id "
                "FROM customer_client "
                "WHERE customer_client.status = 'ENABLED'"
            )
            search_url = f"{base_url}/customers/{seed_cid}/googleAds:search"
            search_headers = {**headers, "login-customer-id": seed_cid}
            sresp = requests.post(
                search_url,
                headers=search_headers,
                json={"query": gaql},
                timeout=30,
            )
            logger.info(f"Google Ads customer_client GAQL for {seed_cid} → status {sresp.status_code}")
            if sresp.status_code == 200:
                rows = sresp.json().get("results", [])
                logger.info(f"Google Ads customer_client rows for {seed_cid}: {len(rows)}")
                for row in rows:
                    cc = row.get("customerClient", {})
                    cid_str = str(cc.get("id", "")).strip()
                    is_manager = cc.get("manager", False)
                    desc_name = cc.get("descriptiveName", "") or f"Google Ads ({cid_str})"
                    if cid_str and cid_str not in all_customers:
                        all_customers[cid_str] = {
                            "account_id": cid_str,
                            "account_name": desc_name,
                            "is_manager": is_manager,
                        }
            else:
                logger.warning(f"Google Ads GAQL for {seed_cid}: {sresp.status_code}: {sresp.text[:300]}")
        except Exception as e:
            logger.warning(f"Google Ads customer_client expansion for {seed_cid} failed: {e}")

    result = list(all_customers.values())
    logger.info(f"Google Ads total discovered accounts: {len(result)} → {[c['account_id'] for c in result]}")
    return result



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
