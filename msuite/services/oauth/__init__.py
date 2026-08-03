"""
OAuth service — public API.

Dispatches to platform-specific handlers via registries. Each module
implements `build_auth_url`, `exchange_token`, and `discover_accounts`
for one product's flow:
  - meta_whatsapp.py : WhatsApp Embedded Signup (special — not standard OAuth)
  - meta_social.py   : Facebook Pages + Instagram Business (organic)
  - meta_ads.py      : Meta Ad Accounts (paid)
  - (future) google.py, tiktok.py, linkedin.py, twitter.py

Shared Meta plumbing (token exchange, business discovery) lives in
meta_base.py so meta_social and meta_ads only own what differs between
them (scopes + discovery).

Usage:
    from msuite.services.oauth import build_auth_url, process_oauth_callback
    from msuite.services.oauth import exchange_whatsapp_code
"""
import json
import secrets

import frappe
from frappe.utils import now, add_days

from msuite.constants import (
    ConnectedAccountStatus,
    OAUTH_STATE_TTL_SECONDS,
    OAUTH_STATE_CACHE_PREFIX,
    LINKEDIN_PENDING_TTL_SECONDS,
    LINKEDIN_PENDING_CACHE_PREFIX,
    TOKEN_REFRESH_BUFFER_DAYS,
    MSUITE_LOGGER_NAME,
)
from msuite.exceptions import OAuthError

# Platform handler imports
from . import google
from . import linkedin
from . import meta_ads
from . import meta_all
from . import meta_catalogue
from . import meta_social
from . import meta_whatsapp
from . import twitter

logger = frappe.logger(MSUITE_LOGGER_NAME)


# ── Platform handler registries ──────────────────────────────────────────
# Each platform registers three functions:
#   auth_url_builder(client_name, state) → url
#   token_exchanger(code, state_data) → token_data
#   account_discoverer(client_name, token_data) → connected_list

_AUTH_URL_BUILDERS = {
    "meta_social": meta_social.build_auth_url,
    "meta_ads":    meta_ads.build_auth_url,
    "meta_all":    meta_all.build_auth_url,
    "meta_catalogue": meta_catalogue.build_auth_url,
    # One handler, one MSuite App. The three keys differ only in which
    # entity type the callback connects (see linkedin._mode_from_state).
    "linkedin":          linkedin.build_auth_url,
    "linkedin_page":     linkedin.build_auth_url,
    "linkedin_profile":  linkedin.build_auth_url,
    "twitter":     twitter.build_auth_url,
    "google":      google.build_auth_url,
    "google_youtube": google.build_auth_url,
    "google_gmail": google.build_auth_url,
    "google_ads":   google.build_auth_url,
}

_TOKEN_EXCHANGERS = {
    "meta_social": meta_social.exchange_token,
    "meta_ads":    meta_ads.exchange_token,
    "meta_all":    meta_all.exchange_token,
    "meta_catalogue": meta_catalogue.exchange_token,
    "linkedin":          linkedin.exchange_token,
    "linkedin_page":     linkedin.exchange_token,
    "linkedin_profile":  linkedin.exchange_token,
    "twitter":     twitter.exchange_token,
    "google":      google.exchange_token,
    "google_youtube": google.exchange_token,
    "google_gmail": google.exchange_token,
    "google_ads":   google.exchange_token,
}

_ACCOUNT_DISCOVERERS = {
    "meta_social": meta_social.discover_accounts,
    "meta_ads":    meta_ads.discover_accounts,
    "meta_all":    meta_all.discover_accounts,
    "meta_catalogue": meta_catalogue.discover_accounts,
    "linkedin":          linkedin.discover_accounts,
    "linkedin_page":     linkedin.discover_accounts,
    "linkedin_profile":  linkedin.discover_accounts,
    "twitter":     twitter.discover_accounts,
    "google":      google.discover_accounts,
    "google_youtube": google.discover_accounts,
    "google_gmail": google.discover_accounts,
    "google_ads":   google.discover_accounts,
}

_TOKEN_REFRESHERS = {
    # Keys MUST match `MSuite Connected Account.platform` field options.
    "Facebook":  meta_base.refresh_long_lived_token,
    "Instagram": meta_base.refresh_long_lived_token,
    "Meta Ads":  meta_base.refresh_long_lived_token,
    "Meta Catalogue": meta_base.refresh_long_lived_token,
    "LinkedIn":  linkedin.refresh_token,
    "Twitter":   twitter.refresh_token_fn,
    "YouTube":   google.refresh_token_fn,
    "Google Ads": google.refresh_token_fn,
    "Gmail":      google.refresh_token_fn,
}


# ── Public API ───────────────────────────────────────────────────────────


def is_platform_supported(platform: str) -> bool:
    """True when an OAuth handler is registered for this platform key.

    Used by the Connect page to grey out cards whose handler hasn't landed
    yet (LinkedIn, Twitter, TikTok, Google) so operators see the roadmap
    without being able to click into a broken flow.
    """
    return platform in _AUTH_URL_BUILDERS


def build_auth_url(platform: str, client_name: str) -> dict:
    """
    Generate an OAuth URL for a platform. Stores CSRF state in Redis.

    Args:
        platform: Platform key (e.g., "meta_social", "google")
        client_name: MSuite Client document name

    Returns:
        {"auth_url": "https://...", "state": "..."}

    Raises:
        OAuthError: if platform not supported or client not active
    """
    client_doc = frappe.get_doc("MSuite Client", client_name)
    if client_doc.status != "Active":
        frappe.throw("Client must be active to connect accounts", OAuthError)

    builder = _AUTH_URL_BUILDERS.get(platform)
    if not builder:
        frappe.throw(f"Unsupported OAuth platform: {platform}", OAuthError)

    # Generate and cache CSRF state
    state = secrets.token_urlsafe(32)
    frappe.cache.set_value(
        f"{OAUTH_STATE_CACHE_PREFIX}:{state}",
        json.dumps({"client_name": client_name, "platform": platform, "state": state}),
        expires_in_sec=OAUTH_STATE_TTL_SECONDS,
    )

    auth_url = builder(client_name, state)
    return {"auth_url": auth_url, "state": state}


def process_oauth_callback(code: str, state: str) -> dict:
    """
    Process OAuth callback after user authorizes.

    Validates CSRF state, exchanges code, discovers accounts,
    stores them, pushes to client.

    Args:
        code: Authorization code from platform
        state: CSRF state token from callback URL

    Returns:
        {"platform": "...", "connected": [...]}

    Raises:
        OAuthError: if state expired or platform not supported
    """
    # Validate CSRF state
    cached = frappe.cache.get_value(f"{OAUTH_STATE_CACHE_PREFIX}:{state}")
    if not cached:
        frappe.throw("OAuth session expired. Please try again.", OAuthError)

    state_data = json.loads(cached)
    state_data["state"] = state
    frappe.cache.delete_value(f"{OAUTH_STATE_CACHE_PREFIX}:{state}")

    platform = state_data["platform"]
    client_name = state_data["client_name"]

    # Exchange code for token
    exchanger = _TOKEN_EXCHANGERS.get(platform)
    if not exchanger:
        frappe.throw(f"No token exchanger for platform: {platform}", OAuthError)
    token_data = exchanger(code, state_data)

    # Discover accounts
    discoverer = _ACCOUNT_DISCOVERERS.get(platform)
    if not discoverer:
        frappe.throw(f"No account discoverer for platform: {platform}", OAuthError)
    connected = discoverer(client_name, token_data)

    # A discoverer may defer account creation and hand back candidates for the
    # user to choose from (LinkedIn pages). Stash the token + candidates so
    # the finalize call can complete the connect without a second OAuth round.
    if isinstance(connected, dict) and connected.get("pending"):
        frappe.cache.set_value(
            f"{LINKEDIN_PENDING_CACHE_PREFIX}:{state}",
            json.dumps({
                "client_name": client_name,
                "platform": platform,
                "token_data": token_data,
                "candidates": connected.get("candidates", []),
            }),
            expires_in_sec=LINKEDIN_PENDING_TTL_SECONDS,
        )
        return {
            "platform": platform,
            "pending": True,
            "state": state,
            "candidates": connected.get("candidates", []),
        }

    frappe.db.commit()
    logger.info(
        f"OAuth complete for {client_name}/{platform}: "
        f"{len(connected)} accounts connected"
    )
    return {"platform": platform, "connected": connected}


def finalize_linkedin_pages(state: str, selected: list[str], client_name: str) -> dict:
    """Connect the LinkedIn pages the user ticked after the OAuth callback.

    Consumes the pending entry cached by `process_oauth_callback`, so the
    token from the original authorization is reused — the user does not go
    through LinkedIn a second time. The cache entry is burned on use.

    `client_name` must match the client that started the flow: the cache is
    keyed by state alone, and that entry holds a live access token, so one
    authenticated client must not be able to finalize another's session.
    """
    cache_key = f"{LINKEDIN_PENDING_CACHE_PREFIX}:{state}"
    cached = frappe.cache.get_value(cache_key)
    if not cached:
        frappe.throw("LinkedIn selection expired. Please reconnect.", OAuthError)

    pending = json.loads(cached)
    if pending.get("client_name") != client_name:
        # Burn it: a mismatch means either a bug or a probe, and either way
        # this state should not survive to be retried.
        frappe.cache.delete_value(cache_key)
        logger.warning(
            f"LinkedIn finalize rejected: {client_name} tried to claim a "
            f"session belonging to {pending.get('client_name')}"
        )
        frappe.throw("LinkedIn session does not belong to this client.", OAuthError)

    frappe.cache.delete_value(cache_key)

    connected = linkedin.finalize_pages(
        client_name=pending["client_name"],
        token_data=pending["token_data"],
        candidates=pending.get("candidates", []),
        selected=selected or [],
    )

    frappe.db.commit()
    logger.info(
        f"LinkedIn page selection for {pending['client_name']}: "
        f"{len(connected)} of {len(pending.get('candidates', []))} connected"
    )
    return {"platform": "linkedin", "connected": connected}


def exchange_whatsapp_code(
    client_name: str,
    code: str,
    session_info: dict | None = None,
    client_env: dict | None = None,
    redirect_uri: str | None = None,
) -> dict:
    """
    WhatsApp Embedded Signup code exchange.

    Delegates to meta_whatsapp.exchange_code(). Not a standard OAuth
    flow — uses Embedded Signup specific endpoints.

    Args:
        client_name: MSuite Client document name
        code: Authorization code from FB.login()
        session_info: Optional waba_id + phone_number_id from sessionInfoListener
        client_env: Optional dict with ip_address, user_agent, browser, etc.
        redirect_uri: Optional redirect URI used in manual OAuth redirect flow.

    Returns:
        {"waba_count": N, "phone_count": N}
    """
    client_doc = frappe.get_doc("MSuite Client", client_name)
    if client_doc.status != "Active":
        frappe.throw("Client must be active", OAuthError)

    return meta_whatsapp.exchange_code(
        client_name=client_name,
        code=code,
        session_info=session_info,
        client_env=client_env,
        redirect_uri=redirect_uri,
    )


def refresh_expiring_tokens() -> dict:
    """
    Refresh tokens approaching expiry. Called by daily scheduler.

    Finds Connected Accounts with token_expiry within
    TOKEN_REFRESH_BUFFER_DAYS and calls the platform-specific refresher.

    Returns:
        {"refreshed": N, "failed": N, "expired": N}
    """
    cutoff = add_days(now(), TOKEN_REFRESH_BUFFER_DAYS)
    accounts = frappe.get_all(
        "MSuite Connected Account",
        filters=[
            ["status", "=", ConnectedAccountStatus.ACTIVE],
            ["token_expiry", "is", "set"],
            ["token_expiry", "<=", cutoff],
        ],
        fields=["name", "client", "platform", "account_id"],
    )

    summary = {"refreshed": 0, "failed": 0, "expired": 0}

    for account in accounts:
        try:
            refresher = _TOKEN_REFRESHERS.get(account.platform)
            if refresher:
                refresher(account.name)
                summary["refreshed"] += 1
            else:
                # No refresher — mark as expired
                frappe.db.set_value(
                    "MSuite Connected Account", account.name,
                    "status", ConnectedAccountStatus.EXPIRED,
                )
                summary["expired"] += 1
        except Exception as e:
            logger.error(f"Token refresh failed for {account.name}: {e}")
            summary["failed"] += 1

    frappe.db.commit()
    return summary
