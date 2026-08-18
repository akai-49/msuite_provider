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
    inspect_debug_token,
    upsert_businesses_as_auth_accounts,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)

META_SOCIAL_SCOPES = [
    # ── Business management scope for Business Suite assets ──────────
    "business_management",         # access Business Suite Pages + Accounts
    # ── Page-level scopes ─────────────────────────────────────────────
    "pages_manage_posts",          # publish organic posts
    "pages_read_engagement",       # read posts + Page-authored comments
    "pages_read_user_content",     # read user-authored content (comments, reviews)
    "pages_manage_engagement",     # reply / hide / delete comments
    "pages_messaging",             # send + receive FB Page DMs
    "pages_manage_metadata",       # subscribe webhooks per-Page (subscribed_apps)
    "pages_show_list",             # enumerate Pages on /me/accounts
    "pages_manage_ads",            # create Lead Gen Forms + boost posts via Page
    "leads_retrieval",             # read submitted leads from /leadgen + webhook
    # ── Instagram scopes (FB-Login flow — instagram_* family, NOT
    # instagram_business_*; the latter is for IG Business Login only). ─
    "instagram_basic",                  # read IG profile
    "instagram_content_publish",        # publish IG media
    "instagram_manage_comments",        # reply / hide / delete IG comments
    "instagram_manage_messages",        # send + receive IG DMs
    "instagram_manage_insights",        # post analytics
]


# Webhook fields we want delivered for each Page. Sent once per Page in
# `_subscribe_page_to_webhooks` immediately after upserting the Connected
# Account. Subscribing the Page also delivers the linked Instagram
# account's messaging events (they arrive on the `instagram` object).
#
# EVERY name here must be valid for the `page` object. Meta validates the
# whole list atomically: one unrecognised field rejects the entire
# `subscribed_apps` call with "(#100) Param subscribed_fields[n] must be
# one of {...}", leaving the Page subscribed to NOTHING and the tenant
# receiving no webhooks at all. That is exactly what `comments` did here —
# it is an Instagram-object field configured in the App Dashboard, not a
# Page field — so it silently took down Page DM delivery with it.
PAGE_WEBHOOK_FIELDS = (
    # Facebook Page DMs + delivery confirmations
    "messages",
    "messaging_postbacks",
    "message_echoes",
    "message_reactions",
    "message_deliveries",
    "message_reads",
    # Facebook Page comments / posts / likes (item="comment" is the
    # comment event; reactions + posts share the same `feed` field).
    "feed",
    # Meta Lead Gen Form submissions
    "leadgen",
)


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
    """Discover Facebook Pages + linked Instagram Business accounts across personal and Business Suite portfolios."""
    user_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", LONG_LIVED_TOKEN_TTL)
    app_name   = token_data.get("app_name", "")

    # Inspect debug token to get granular_target_ids selected by the user in Meta consent popup
    debug_info = inspect_debug_token(user_token)
    granular_target_ids = debug_info.get("target_ids") or set()

    businesses   = discover_businesses(user_token)
    biz_auth_map = upsert_businesses_as_auth_accounts(client_name, businesses)
    page_biz_map = _build_page_business_map(businesses, user_token, biz_auth_map)

    return _discover_pages_and_instagram(
        client_name, user_token, app_name, expires_in, page_biz_map, businesses, granular_target_ids=granular_target_ids
    )


# ---------------------------------------------------------------------------
# Page ↔ Business mapping & discovery
# ---------------------------------------------------------------------------


def _build_page_business_map(
    businesses: list[dict],
    user_token: str,
    biz_auth_map: dict[str, str],
) -> dict[str, dict]:
    """Map `page_id` and `page_name` → `{business_id, business_name, auth_account}`.

    Queries `owned_pages` first across all Business Manager portfolios, then
    `client_pages`. `owned_pages` entries represent primary page ownership and
    take precedence over agency/client relationships in `client_pages`.
    """
    page_biz_map: dict[str, dict] = {}
    for endpoint in ("owned_pages", "client_pages"):
        for biz in businesses:
            biz_id = biz.get("id")
            if not biz_id:
                continue
            try:
                resp = requests.get(
                    f"{GRAPH_API_BASE}/{biz_id}/{endpoint}",
                    params={
                        "access_token": user_token,
                        "fields":       "id,name,access_token,category,picture,business",
                        "limit":        200,
                    },
                    timeout=30,
                )
                for p in resp.json().get("data", []):
                    p_biz = p.get("business") or {}
                    owner_id = str(p_biz.get("id") or biz_id)
                    owner_name = p_biz.get("name") or biz.get("name", "")

                    info = {
                        "business_id":   owner_id,
                        "business_name": owner_name,
                        "auth_account":  biz_auth_map.get(owner_id) or biz_auth_map.get(biz_id),
                        "page_data":     p,
                    }
                    p_id = p.get("id")
                    p_name = p.get("name")

                    # owned_pages takes precedence; client_pages must not overwrite an owned_pages entry
                    if p_id and (endpoint == "owned_pages" or p_id not in page_biz_map):
                        page_biz_map[p_id] = info
                    if p_name and (endpoint == "owned_pages" or p_name not in page_biz_map):
                        page_biz_map[p_name] = info
            except Exception as e:
                logger.warning(f"{endpoint} lookup failed for biz {biz_id}: {e}")
    return page_biz_map


def _fetch_page_detail(page_id: str, user_token: str) -> dict | None:
    """Fetch individual Page details including access_token directly from Page node."""
    for field_list in (
        "id,name,access_token,category,picture",
        "id,name,access_token,category,picture,business",
        "id,name,access_token",
    ):
        try:
            resp = requests.get(
                f"{GRAPH_API_BASE}/{page_id}",
                params={
                    "access_token": user_token,
                    "fields":       field_list,
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("id") and (data.get("name") or data.get("access_token")):
                return data
        except Exception as e:
            logger.warning(f"Page detail fetch failed for page {page_id}: {e}")
    return None


def _discover_pages_and_instagram(
    client_name: str,
    user_token: str,
    app_name: str,
    expires_in: int,
    page_biz_map: dict,
    businesses: list[dict],
    granular_target_ids: set[str] | None = None,
) -> list[dict]:
    """Discover Facebook Pages via `/me/accounts`, `/{biz}/owned_pages`, `/{biz}/client_pages` and their linked IG."""
    connected: list[dict] = []
    pages_dict: dict[str, dict] = {}
    biz_auth_map = {biz.get("id"): biz.get("name", "") for biz in businesses if biz.get("id")}

    # 1. Fetch personal / direct pages from /me/accounts
    try:
        resp = requests.get(
            f"{GRAPH_API_BASE}/me/accounts",
            params={
                "access_token": user_token,
                "fields":       "id,name,access_token,category,picture",
                "limit":        200,
            },
            timeout=30,
        )
        if resp.status_code == 200:
            for page in resp.json().get("data", []):
                if page.get("id"):
                    pages_dict[page["id"]] = page
    except Exception as e:
        logger.error(f"Failed to discover personal Facebook Pages (/me/accounts): {e}")

    # 2. Also query any explicitly granted Page IDs from granular_target_ids (e.g. standalone/NPE pages)
    if granular_target_ids:
        for tid in granular_target_ids:
            if not tid or tid.startswith("act_") or tid in pages_dict or tid in biz_auth_map:
                continue
            p_data = _fetch_page_detail(tid, user_token)
            if p_data and p_data.get("id") and (p_data.get("name") or p_data.get("access_token")):
                pages_dict[p_data["id"]] = p_data
                logger.info(f"Discovered standalone page {p_data.get('name', tid)} ({p_data['id']}) via granular target ID")

    # 3. Collect pages from Business Manager / Business Suite (owned_pages & client_pages)
    for endpoint in ("owned_pages", "client_pages"):
        for biz in businesses:
            biz_id = biz.get("id")
            if not biz_id:
                continue
            try:
                resp = requests.get(
                    f"{GRAPH_API_BASE}/{biz_id}/{endpoint}",
                    params={
                        "access_token": user_token,
                        "fields":       "id,name,access_token,category,picture,business",
                        "limit":        200,
                    },
                    timeout=30,
                )
                for page in resp.json().get("data", []):
                    page_id = page.get("id")
                    if page_id and page_id not in pages_dict:
                        pages_dict[page_id] = page
            except Exception as e:
                logger.warning(f"Failed to fetch {endpoint} for business {biz_id}: {e}")

    # 4. Process all unique discovered Pages
    for page_id, page in pages_dict.items():
        page_name  = page.get("name", "")
        page_token = page.get("access_token", "")

        p_biz = page.get("business") or {}
        direct_biz_id = str(p_biz.get("id") or "")
        direct_biz_name = str(p_biz.get("name") or "")

        biz_info     = page_biz_map.get(page_id) or page_biz_map.get(page_name) or {}
        biz_id       = direct_biz_id or biz_info.get("business_id", "")
        biz_name     = direct_biz_name or biz_info.get("business_name", "")


        token_type = "Page Token"
        if not page_token:
            page_detail = _fetch_page_detail(page_id, user_token)
            if page_detail and page_detail.get("access_token"):
                page_token = page_detail["access_token"]
                page = page_detail
                if not direct_biz_id and page.get("business"):
                    p_biz = page.get("business") or {}
                    direct_biz_id = str(p_biz.get("id") or "")
                    direct_biz_name = str(p_biz.get("name") or "")
                    biz_id = direct_biz_id or biz_id
                    biz_name = direct_biz_name or biz_name

        if not page_token:
            page_token = user_token
            token_type = "User Token"
            logger.info(f"Page {page_name} ({page_id}): using user_token as fallback")

        avatar_url = f"https://graph.facebook.com/{page_id}/picture?type=large"
        auth_account = biz_info.get("auth_account") or (biz_auth_map.get(biz_id) if biz_id else None)

        ca_name = upsert_connected_account(
            client_name, Platform.FACEBOOK, page_id, {
                "display_name":  page_name,
                "auth_account":  auth_account,
                "access_token":  page_token,
                "token_type":    token_type,
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
            "avatar_url":        avatar_url,
        })

        _subscribe_page_to_webhooks(ca_name, page_id, page_token)

        ig_result = _discover_instagram_for_page(
            client_name, page_id, page_name, page_token, user_token,
            app_name, expires_in, biz_id, biz_name=biz_name, auth_account=auth_account,
        )
        if ig_result:
            connected.append(ig_result)

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
    biz_name: str = "",
    auth_account: str | None = None,
) -> dict | None:
    """Check a page for a linked Instagram Business Account; upsert + push."""
    try:
        resp = requests.get(
            f"{GRAPH_API_BASE}/{page_id}",
            params={
                "fields":       "instagram_business_account{id,username,name,profile_picture_url}",
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
        ig_avatar_url = ig_account.get("profile_picture_url") or ""

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
            "business_name":    biz_name,
            "avatar_url":       ig_avatar_url,
        })

        return {"platform": "Instagram", "name": f"@{ig_username}"}

    except Exception as e:
        logger.error(f"Instagram check failed for page {page_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Per-Page webhook subscription
# ---------------------------------------------------------------------------


def _subscribe_page_to_webhooks(connected_account_name: str,
                                page_id: str,
                                page_token: str) -> None:
    """Subscribe one Page to our webhook fields via
    `POST /{page-id}/subscribed_apps`.

    Meta requires this per-Page even when the App Dashboard already lists
    the fields globally — the dashboard says WHICH events we'd like to
    receive, the per-Page call says WHICH Pages should fire them at us.
    Skipping this is the silent-no-events bug: customers OAuth'd
    successfully and we still got nothing.

    Records the outcome on `MSuite Connected Account`:
      - `subscribed_fields` (JSON) — fields Meta accepted
      - `subscribed_at`      — timestamp of the last success
      - `last_subscription_error` — string from the last failure (empty on success)

    Idempotent — calling it twice for the same Page is safe; Meta replies
    `{success: true}` either way.
    """
    fields_csv = ",".join(PAGE_WEBHOOK_FIELDS)
    err_msg = ""
    success_fields = ""
    try:
        resp = requests.post(
            f"{GRAPH_API_BASE}/{page_id}/subscribed_apps",
            data={
                "subscribed_fields": fields_csv,
                "access_token":      page_token,
            },
            timeout=15,
        )
        body = resp.json() if resp.content else {}
        if resp.ok and (body.get("success") in (True, "true", 1)):
            success_fields = fields_csv
            logger.info(
                f"Page {page_id} subscribed to webhook fields: {fields_csv}"
            )
        else:
            err_msg = (
                (body.get("error") or {}).get("message")
                or f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
            logger.warning(
                f"Page {page_id} subscription failed: {err_msg}"
            )
    except requests.RequestException as e:
        err_msg = f"network: {e}"
        logger.warning(f"Page {page_id} subscription network error: {e}")

    # Persist outcome on the Connected Account — never raises; failed
    # subscriptions go into `last_subscription_error` so the scheduled
    # `retry_failed_page_subscriptions` job can retry without full OAuth.
    try:
        updates: dict = {"last_subscription_error": err_msg or ""}
        if success_fields:
            updates["subscribed_fields"] = success_fields
            updates["subscribed_at"] = now()
        frappe.db.set_value(
            "MSuite Connected Account", connected_account_name,
            updates, update_modified=False,
        )
    except Exception as e:
        logger.error(
            f"Could not persist subscription state on {connected_account_name}: {e}"
        )


def retry_failed_page_subscriptions() -> None:
    """Scheduled repair: re-run `subscribed_apps` for every Active
    Facebook Connected Account whose last subscription attempt failed.

    Picks up automatically after a token refresh — the retry uses the
    account's CURRENT decrypted token, so a subscription that failed on
    an expired token heals on the next run without re-OAuth.
    """
    from frappe.utils.password import get_decrypted_password

    rows = frappe.get_all(
        "MSuite Connected Account",
        filters={
            "platform": "Facebook",
            "status": "Active",
            "last_subscription_error": ["!=", ""],
        },
        fields=["name", "account_id"],
        limit_page_length=100,
    )
    for row in rows:
        token = get_decrypted_password(
            "MSuite Connected Account", row.name, "access_token", raise_exception=False
        )
        if not token:
            continue
        try:
            _subscribe_page_to_webhooks(row.name, row.account_id, token)
        except Exception as e:
            logger.warning(f"Subscription repair failed for {row.name}: {e}")
