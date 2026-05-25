"""
Meta OAuth — combined Social + Ads flow.

Used when a customer picks the "Both (Social + Ads)" pill in the Connect
page. Wraps the existing `meta_social` and `meta_ads` handlers:

  • `build_auth_url`     — union of both scope lists in one Facebook
                           OAuth dialog so the user grants both
                           permissions in a single consent screen.
  • `exchange_token`     — shared `exchange_code_for_long_lived_token`;
                           both flows return the same user-token shape.
  • `discover_accounts`  — runs Social discovery + Ads discovery against
                           the same token and merges the connected list.

No new MSuite App row is needed — Meta uses one app for both products
(see `META_APP_PLATFORM` in `meta_base.py`).
"""
from __future__ import annotations

import frappe

from msuite.constants import MSUITE_LOGGER_NAME

from . import meta_ads, meta_social
from .meta_base import (
    build_facebook_login_url,
    exchange_code_for_long_lived_token,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)


# Union of social + ads scopes. Deduped by `dict.fromkeys` so any
# overlap (none today, but defensive) doesn't get sent twice.
META_ALL_SCOPES: list[str] = list(dict.fromkeys(
    meta_social.META_SOCIAL_SCOPES + meta_ads.META_ADS_SCOPES,
))


# ---------------------------------------------------------------------------
# Registry interface
# ---------------------------------------------------------------------------


def build_auth_url(client_name: str, state: str) -> str:
    """Facebook OAuth dialog URL with both Pages/IG and Ads scopes.

    User sees a single consent screen listing everything we need; no
    second popup after Social finishes.
    """
    return build_facebook_login_url(META_ALL_SCOPES, state)


def exchange_token(code: str, state_data: dict) -> dict:
    """Code → long-lived user token (shared with meta_social/meta_ads)."""
    return exchange_code_for_long_lived_token(code)


def discover_accounts(client_name: str, token_data: dict) -> list[dict]:
    """Run both Social and Ads discovery against the same user token.

    Each handler owns its own upsert (Connected Account + per-platform
    rows like Page, Instagram Business, Ad Account). They don't share
    rows, so concatenating their return lists is safe — no de-duping
    needed.

    A failure in one half is logged but doesn't block the other —
    partial connection is better than nothing, and the customer can
    re-run the missing half from the Connect page.
    """
    social: list[dict] = []
    ads: list[dict] = []

    try:
        social = meta_social.discover_accounts(client_name, token_data) or []
    except Exception:
        logger.exception("meta_all: social discovery failed for %s", client_name)

    try:
        ads = meta_ads.discover_accounts(client_name, token_data) or []
    except Exception:
        logger.exception("meta_all: ads discovery failed for %s", client_name)

    return social + ads
