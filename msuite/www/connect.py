"""Context for the MSuite Connect webpage.

Two render modes share the same HTML:

  1. **Admin mode** (no query string) — operator picks a client from a
     dropdown, all platform cards are visible. This is the original
     flow used inside the provider's Frappe Desk session.

  2. **Client-initiated mode** (`?state=<token>&launch=whatsapp`) — the
     client redirected the user here after they clicked "Connect
     WhatsApp" on the client's Connections Page. State carries the
     `client_name` + `return_url` (so we know where to send the user
     after FB.login finishes). The JS reads `window.MSUITE_CLIENT_INITIATED`
     to skip the dropdown and auto-launch FB.login for WA.

We don't consume the state here — `exchange_whatsapp` deletes it at the
end. This avoids the case where a user reloads the page mid-flow and
the auto-launch silently dies because the state was already burnt.

`no_login_required = 1` because the client-initiated visitor is NOT a
provider user — they're an end-user on the client's site clicking
"Connect WhatsApp". Auth is provided by the state token, validated by
`get_connect_config` and `exchange_whatsapp` on every API call. A
guest who opens /connect directly (no state) will get an empty page —
the data calls still require System Manager / state, so nothing leaks.
"""
import json
import os

import frappe

from msuite.constants import OAUTH_STATE_CACHE_PREFIX


def _asset_version() -> str:
    """Mtime-based cache buster for /assets/msuite/js/connect.js.

    Frappe doesn't auto-version www-page asset URLs the way it does for
    Vue bundles, so the browser caches `connect.js` forever and never
    sees new builds. Appending `?v=<mtime>` to the <script src> forces
    a fresh fetch on every deploy without breaking long-term caching
    of unchanged builds.
    """
    path = os.path.join(
        frappe.get_app_path("msuite"), "public", "js", "connect.js",
    )
    try:
        return str(int(os.path.getmtime(path)))
    except OSError:
        return "0"

no_cache = 1
no_login_required = 1


def get_context(context):
    context.no_cache = 1
    context.show_sidebar = False
    context.asset_version = _asset_version()

    state = (frappe.form_dict.get("state") or "").strip()
    launch = (frappe.form_dict.get("launch") or "").strip().lower()

    if not state:
        # Admin mode — operator opens /connect from their Frappe Desk
        # session. The JS calls frappe.client.get_list which requires
        # System Manager; that's the right behaviour for that path.
        return

    # State param present → this is a client-initiated visit. From here
    # on we MUST set either `client_initiated` (success) or `state_error`
    # (expired / bad / wrong platform). Falling through to admin mode
    # would call `frappe.client.get_list` as a guest and 403.
    cached = frappe.cache.get_value(f"{OAUTH_STATE_CACHE_PREFIX}:{state}")
    if not cached:
        context.state_error = (
            "Your signup session has expired. Please go back to your "
            "Connections page and click Connect WhatsApp again."
        )
        return

    try:
        data = json.loads(cached)
    except (TypeError, ValueError):
        context.state_error = (
            "Your signup session is corrupted. Please retry from your "
            "Connections page."
        )
        return

    if data.get("platform") != "whatsapp":
        context.state_error = (
            "This signup link is for a different platform. Please retry "
            "from your Connections page."
        )
        return

    context.client_initiated = {
        "state":       state,
        "client_name": data.get("client_name") or "",
        "return_url":  data.get("return_url") or "",
        "launch":      launch or "whatsapp",
    }
