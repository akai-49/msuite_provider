"""
OAuth and Embedded Signup API endpoints.

Handles platform authentication flows:
  - start_auth: generates OAuth URL for popup
  - auth_callback: receives OAuth redirect, stores accounts, closes popup
  - exchange_whatsapp: WhatsApp Embedded Signup code exchange
"""
import json

import frappe

from msuite.constants import ErrorCode, MSUITE_LOGGER_NAME
from msuite.utils.validators import (
    success_response,
    error_response,
    require_system_manager_or_msuite_manager,
    require_msuite_client_auth,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)


@frappe.whitelist()
def start_auth(platform: str, client_name: str) -> dict:
    """
    Generate OAuth URL for a platform. Opens in a popup.

    Args:
        platform: Platform key ("meta_social", "google", etc.)
        client_name: MSuite Client document name

    Returns:
        {"auth_url": "...", "state": "..."}
    """
    try:
        require_system_manager_or_msuite_manager()
        from msuite.services.oauth import build_auth_url
        data = build_auth_url(platform, client_name)
        return success_response(data)
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.INVALID_INPUT, str(e))


@frappe.whitelist(allow_guest=True)
def start_auth_for_client(client_name: str, platform: str) -> dict:
    """
    Client-initiated OAuth — the mirror of `start_auth` for when the
    marketer kicks off a connect flow from their own client site (not
    from the provider admin UI).

    Auth: `X-MSuite-Provider-Key` + `X-MSuite-Provider-Secret` headers,
    matched against `MSuite Client.api_key` / `api_secret` — same
    header pair already used by `_post_to_client` in `webhook.py` for
    the reverse direction. The client stores these on its
    `MSuite Settings` at activation time.

    Args:
        client_name: MSuite Client document name (the caller claims to be this).
        platform: Platform key ("meta_social", "meta_ads", "google", ...).

    Returns:
        {"status": "success", "data": {"auth_url": "...", "state": "..."}}

    Rejects anything not in `_AUTH_URL_BUILDERS` on the service layer
    with a clean error — the client's Connections Page greys out those
    cards upfront via `list_configured_platforms_for_client`.
    """
    try:
        # Resolve either doc-name or client_code → actual doc name so
        # build_auth_url (which does frappe.get_doc) always gets the
        # canonical identifier.
        client_doc = require_msuite_client_auth(client_name)
        from msuite.services.oauth import build_auth_url, is_platform_supported
        if not is_platform_supported(platform):
            return error_response(ErrorCode.INVALID_INPUT,
                                  f"Platform not supported: {platform}")
        data = build_auth_url(platform, client_doc.name)
        return success_response(data)
    except frappe.AuthenticationError as e:
        return error_response(ErrorCode.PERMISSION_DENIED, str(e))
    except frappe.PermissionError as e:
        return error_response(ErrorCode.PERMISSION_DENIED, str(e))
    except Exception as e:
        logger.error(f"start_auth_for_client failed: {e}", exc_info=True)
        return error_response(ErrorCode.INVALID_INPUT, str(e))


@frappe.whitelist(allow_guest=True)
def list_configured_platforms_for_client(client_name: str) -> dict:
    """
    Return platforms whose OAuth handlers are registered + ready for
    this client. Used by the client's Connections Page to disable
    unsupported platform cards before the user clicks Connect.

    "Configured" here means: a handler is registered in the service
    layer's `_AUTH_URL_BUILDERS` registry — i.e. the code path exists.
    Credential presence (MSuite App records) is checked by the builder
    at auth-URL time; a client with a supported platform but missing
    app credentials still gets a clean error on click.

    Auth: same `X-MSuite-Provider-Key/Secret` headers as `start_auth_for_client`.
    """
    try:
        require_msuite_client_auth(client_name)
        from msuite.services.oauth import _AUTH_URL_BUILDERS
        return success_response({"platforms": list(_AUTH_URL_BUILDERS.keys())})
    except frappe.AuthenticationError as e:
        return error_response(ErrorCode.PERMISSION_DENIED, str(e))
    except Exception as e:
        logger.error(f"list_configured_platforms_for_client failed: {e}",
                     exc_info=True)
        return error_response(ErrorCode.INVALID_INPUT, str(e))


@frappe.whitelist(allow_guest=True)
def auth_callback(**kwargs) -> None:
    """
    OAuth callback endpoint. Called by platform redirect in a popup window.

    Receives authorization code + CSRF state, exchanges for token,
    discovers accounts, stores, pushes to client.

    Renders HTML that posts result to opener window and auto-closes.
    """
    code = frappe.form_dict.get("code", "")
    state = frappe.form_dict.get("state", "")
    error = frappe.form_dict.get("error", "")

    if error:
        _respond_popup(
            "Authorization Failed",
            f"The platform returned an error: {error}",
            "red",
            {"type": "msuite_auth_error", "error": error},
        )
        return

    if not code or not state:
        _respond_popup(
            "Authorization Failed",
            "Missing authorization code or state parameter.",
            "red",
            {"type": "msuite_auth_error", "error": "missing_params"},
        )
        return

    try:
        from msuite.services.oauth import process_oauth_callback
        result = process_oauth_callback(code, state)

        count = len(result.get("connected", []))
        platform = result.get("platform", "")
        _respond_popup(
            "Connected Successfully",
            f"Connected {count} account(s) via {platform}.",
            "green",
            {
                "type": "msuite_connected",
                "platform": platform,
                "count": count,
                "connected": result.get("connected", []),
            },
        )
    except Exception as e:
        logger.error(f"OAuth callback error: {e}", exc_info=True)
        _respond_popup(
            "Authorization Failed",
            str(e),
            "red",
            {"type": "msuite_auth_error", "error": str(e)},
        )


@frappe.whitelist()
def exchange_whatsapp(
    client_name: str,
    code: str,
    waba_id: str = "",
    phone_number_id: str = "",
    event: str = "",
    business_id: str = "",
) -> dict:
    """
    Exchange WhatsApp Embedded Signup authorization code.

    Called from MSuite Client JS after FB.login() popup completes.
    Optionally receives session_info fields from sessionInfoListener (v2).

    Args:
        client_name: MSuite Client document name
        code: Authorization code from FB.login() callback
        waba_id: Optional WABA ID from sessionInfoListener
        phone_number_id: Optional phone number ID from sessionInfoListener
        event: Optional event type (FINISH, FINISH_ONLY_WABA, COEXISTENCE, CANCEL, ERROR)
        business_id: Optional business ID (from coexistence event)

    Returns:
        Success dict with WABA and phone counts
    """
    try:
        require_system_manager_or_msuite_manager()

        # Capture client environment from the HTTP request
        client_env = _capture_client_env()

        # Build session_info from all available fields
        session_info = {}
        if waba_id:
            session_info["waba_id"] = waba_id
        if phone_number_id:
            session_info["phone_number_id"] = phone_number_id
        if event:
            session_info["event"] = event
        if business_id:
            session_info["business_id"] = business_id

        from msuite.services.oauth import exchange_whatsapp_code
        result = exchange_whatsapp_code(client_name, code, session_info, client_env)
        return success_response(result)
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.INVALID_INPUT, str(e))


@frappe.whitelist()
def log_signup_event(
    client_name: str,
    event: str,
    current_step: str = "",
    error_message: str = "",
    waba_id: str = "",
) -> dict:
    """
    Log a CANCEL or ERROR event from WhatsApp Embedded Signup.

    Called when the user cancels or an error occurs (no code to exchange).
    Creates a consent record for audit purposes.

    Args:
        client_name: MSuite Client document name
        event: "CANCEL" or "ERROR"
        current_step: Which step the user was on (CANCEL only)
        error_message: Error description (ERROR only)
        waba_id: WABA ID if available
    """
    try:
        require_system_manager_or_msuite_manager()
        client_env = _capture_client_env()

        session_info = {"event": event, "waba_id": waba_id}

        if event == "CANCEL":
            session_info["current_step"] = current_step
            from msuite.services.oauth.meta_whatsapp import create_cancel_consent
            create_cancel_consent(client_name, session_info, client_env)
        elif event == "ERROR":
            session_info["error_message"] = error_message
            from msuite.services.oauth.meta_whatsapp import create_error_consent
            create_error_consent(client_name, session_info, client_env)

        return success_response({"logged": True})
    except Exception as e:
        return error_response(ErrorCode.INVALID_INPUT, str(e))


def _capture_client_env() -> dict:
    """Capture IP, user-agent, browser, OS, language from the HTTP request."""
    env = {}
    if not frappe.request:
        return env

    env["ip_address"] = getattr(frappe.local, "request_ip", "") or ""
    env["accept_language"] = frappe.get_request_header("Accept-Language") or ""

    # User-Agent: prefer raw header (always available), then parse via Werkzeug
    raw_ua = frappe.get_request_header("User-Agent") or ""
    env["user_agent"] = raw_ua

    ua = frappe.request.user_agent
    if ua:
        env["browser"] = f"{ua.browser} {ua.version}" if ua.browser else ""
        env["platform_os"] = ua.platform or ""
    else:
        env["browser"] = ""
        env["platform_os"] = ""

    return env


# ----------------------------------------------------------------------
# Connect page configuration
# ----------------------------------------------------------------------
#
# Catalog of platforms rendered on /connect. This is the single source of
# truth used by `get_connect_config`. Ordering here is the display order.
#
# Each entry:
#   key            - flow identifier (matches OAuth registry key; also how
#                    the frontend picks between Embedded Signup and popup)
#   label          - card heading
#   description    - one-line subtitle
#   icon           - emoji (keeps the page dependency-free)
#   app_platform   - MSuite App.platform value to source app_id from
#   oauth_handler  - OAuth registry key, or None for special flows
#                    (WhatsApp uses Embedded Signup, not standard OAuth)
#   product        - MSuite Product code (WA | SOCIAL | ADS) the card
#                    belongs to; lets the frontend group/gate by plan.
#
# Adding a new platform = one row here + one OAuth handler file.

_CONNECT_PLATFORMS = [
    {
        "key": "whatsapp",
        "label": "WhatsApp",
        "description": "Connect your WhatsApp Business Account via Meta Embedded Signup.",
        "icon": "💬",
        "app_platform": "Meta WhatsApp",
        "oauth_handler": None,
        "product": "WA",
    },
    {
        "key": "meta_social",
        "label": "Meta (Facebook & Instagram)",
        "description": "Publish organic posts to Facebook Pages and linked Instagram Business accounts.",
        "icon": "📱",
        "app_platform": "Meta Social",
        "oauth_handler": "meta_social",
        "product": "SOCIAL",
    },
    {
        "key": "linkedin",
        "label": "LinkedIn",
        "description": "Publish to LinkedIn profiles and company pages.",
        "icon": "💼",
        "app_platform": "LinkedIn",
        "oauth_handler": "linkedin",
        "product": "SOCIAL",
    },
    {
        "key": "twitter",
        "label": "X (Twitter)",
        "description": "Publish tweets and threads.",
        "icon": "🐦",
        "app_platform": "Twitter",
        "oauth_handler": "twitter",
        "product": "SOCIAL",
    },
    {
        "key": "tiktok",
        "label": "TikTok",
        "description": "Post videos via the TikTok Content Posting API.",
        "icon": "🎵",
        "app_platform": "TikTok",
        "oauth_handler": "tiktok",
        "product": "SOCIAL",
    },
    {
        "key": "youtube",
        "label": "YouTube",
        "description": "Upload videos to YouTube via Google OAuth.",
        "icon": "▶️",
        "app_platform": "Google",
        "oauth_handler": "google",
        "product": "SOCIAL",
    },
    {
        "key": "meta_ads",
        "label": "Meta Ads",
        "description": "Manage Meta Ad Accounts for paid campaigns on Facebook and Instagram.",
        "icon": "📊",
        "app_platform": "Meta Social",
        "oauth_handler": "meta_ads",
        "product": "ADS",
    },
]


@frappe.whitelist()
def get_connect_config(client_name: str) -> dict:
    """
    Return the rendering data for the `/connect` page.

    Emits a uniform list of supported platforms. For each, the frontend
    learns whether the platform is `ready` (MSuite App configured + OAuth
    handler registered). Secrets are never included — only app_ids and
    flow-level metadata the browser needs.
    """
    try:
        require_system_manager_or_msuite_manager()
        client = frappe.get_doc("MSuite Client", client_name)

        return success_response({
            "client": {
                "name": client.name,
                "url": client.client_url,
                "status": client.status,
                "customer": client.customer,
            },
            "platforms": [_resolve_platform(p) for p in _CONNECT_PLATFORMS],
            "connected_accounts": _list_connected_accounts(client.name),
        })
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.INVALID_INPUT, str(e))


def _resolve_platform(platform_def: dict) -> dict:
    """Hydrate a platform catalog entry with live app config + readiness."""
    from msuite.services.oauth import is_platform_supported

    app = frappe.db.get_value(
        "MSuite App",
        {"platform": platform_def["app_platform"], "is_active": 1},
        ["app_id", "config_id", "redirect_uri", "allow_coexistence"],
        as_dict=True,
    )

    handler_ready = (
        platform_def["oauth_handler"] is None
        or is_platform_supported(platform_def["oauth_handler"])
    )
    has_app = bool(app and app.app_id)

    entry = {
        "key": platform_def["key"],
        "label": platform_def["label"],
        "description": platform_def["description"],
        "icon": platform_def["icon"],
        "product": platform_def["product"],
        "flow": "embedded_signup" if platform_def["key"] == "whatsapp" else "oauth_popup",
        "ready": has_app and handler_ready,
        "reason": _readiness_reason(platform_def, has_app, handler_ready),
        "app_id": app.app_id if has_app else None,
    }

    # WhatsApp-only extras that the FB SDK needs on the client.
    if platform_def["key"] == "whatsapp" and has_app:
        entry["config_id"] = app.config_id
        entry["allow_coexistence"] = bool(app.allow_coexistence or 0)

    return entry


def _readiness_reason(platform_def: dict, has_app: bool, handler_ready: bool) -> str:
    """Short explanation shown under a greyed-out card."""
    if not has_app:
        return f"No active MSuite App configured for {platform_def['app_platform']}."
    if not handler_ready:
        return "OAuth handler not yet available."
    return ""


def _list_connected_accounts(client_name: str) -> list[dict]:
    """Accounts already linked to this client, for the bottom section of /connect."""
    return frappe.get_all(
        "MSuite Connected Account",
        filters={"client": client_name},
        fields=[
            "name", "platform", "account_name", "account_id",
            "status", "business_name", "connected_at",
        ],
        order_by="platform asc, connected_at desc",
    )


# ======================================================================
# Popup response helper
# ======================================================================


def _respond_popup(title: str, message: str, indicator: str, post_data: dict):
    """
    Render HTML page that posts result to opener window via postMessage
    and auto-closes. Used for OAuth popup flows.
    """
    safe_message = frappe.utils.escape_html(message)
    post_json = json.dumps(post_data)

    frappe.respond_as_web_page(
        title,
        f"""<p>{safe_message}</p>
        <script>
            if (window.opener) {{
                window.opener.postMessage({post_json}, '*');
            }}
            setTimeout(function() {{ window.close(); }}, 2000);
        </script>""",
        indicator_color=indicator,
    )
