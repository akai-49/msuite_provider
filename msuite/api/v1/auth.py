"""
OAuth and Embedded Signup API endpoints.

Handles platform authentication flows:
  - start_auth: generates OAuth URL for popup
  - auth_callback: receives OAuth redirect, stores accounts, closes popup
  - exchange_whatsapp: WhatsApp Embedded Signup code exchange
  - start_whatsapp_embedded_signup: client-initiated WA signup (redirect → /connect)
"""
import json
import secrets

import frappe

from msuite.constants import (
    ErrorCode,
    MSUITE_LOGGER_NAME,
    OAUTH_STATE_CACHE_PREFIX,
    OAUTH_STATE_TTL_SECONDS,
)
from msuite.utils.validators import (
    success_response,
    error_response,
    require_system_manager_or_msuite_manager,
    require_msuite_client_auth,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)


# Platform key used to namespace the WhatsApp Embedded Signup state in the
# OAuth state cache. Distinct from the standard OAuth platform keys
# (`meta_social`, `meta_ads`, …) because the WA flow doesn't use the
# `_AUTH_URL_BUILDERS` registry — it has its own exchange endpoint.
_WHATSAPP_STATE_PLATFORM = "whatsapp"


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
def start_whatsapp_embedded_signup(client_name: str, return_url: str = "") -> dict:
    """
    Client-initiated WhatsApp Embedded Signup entry.

    Generates a direct Meta OAuth dialog URL for WhatsApp Embedded Signup.
    Opens in a popup on the client side, bypassing the provider's /connect page.

    Auth: same `X-MSuite-Provider-Key/Secret` headers as
    `start_auth_for_client`.

    Args:
        client_name: MSuite Client doc name or client_code.
        return_url: Absolute URL on the client.

    Returns:
        {"auth_url": "https://www.facebook.com/v25.0/dialog/oauth?...",
         "state":    "..."}
    """
    try:
        client_doc = require_msuite_client_auth(client_name)

        app = frappe.db.get_value(
            "MSuite App",
            {"platform": "Meta WhatsApp", "is_active": 1},
            ["app_id", "config_id", "redirect_uri"],
            as_dict=True,
        )
        if not app or not app.get("app_id") or not app.get("config_id"):
            return error_response(
                ErrorCode.INVALID_INPUT,
                "Meta WhatsApp app is not configured or active on the provider."
            )

        # Mint state, cache with client_name + return_url. Reuses the
        # standard OAuth state plumbing so cleanup / TTL / replay-safety
        # behave identically.
        state = secrets.token_urlsafe(32)
        frappe.cache.set_value(
            f"{OAUTH_STATE_CACHE_PREFIX}:{state}",
            json.dumps({
                "client_name":     client_doc.name,
                "platform":        _WHATSAPP_STATE_PLATFORM,
                "return_url":      return_url,
                "initiated_from":  "client",
            }),
            expires_in_sec=OAUTH_STATE_TTL_SECONDS,
        )

        redirect_uri = app.get("redirect_uri")
        if not redirect_uri:
            social_redirect = frappe.db.get_value("MSuite App", {"platform": "Meta Social", "is_active": 1}, "redirect_uri")
            if social_redirect:
                redirect_uri = social_redirect
            else:
                provider_base = (
                    (frappe.local.conf.host_name or frappe.local.conf.hostname or "").rstrip("/")
                    or frappe.utils.get_url().rstrip("/")
                )
                redirect_uri = f"{provider_base}/api/method/msuite.api.v1.auth.auth_callback"

        from urllib.parse import urlencode
        from msuite.constants import GRAPH_API_VERSION

        params = {
            "client_id": app["app_id"],
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "config_id": app["config_id"],
        }
        auth_url = f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth?{urlencode(params)}"
        return success_response({"auth_url": auth_url, "state": state})

    except frappe.AuthenticationError as e:
        return error_response(ErrorCode.PERMISSION_DENIED, str(e))
    except frappe.PermissionError as e:
        return error_response(ErrorCode.PERMISSION_DENIED, str(e))
    except Exception as e:
        logger.error(
            f"start_whatsapp_embedded_signup failed: {e}", exc_info=True,
        )
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
        # Check if the cached state is for WhatsApp Embedded Signup
        cached = frappe.cache.get_value(f"{OAUTH_STATE_CACHE_PREFIX}:{state}")
        if cached:
            try:
                state_data = json.loads(cached)
            except (TypeError, ValueError):
                state_data = {}

            if state_data.get("platform") == _WHATSAPP_STATE_PLATFORM:
                client_name = state_data.get("client_name")
                client_env = _capture_client_env()
                client_env["initiated_from"] = state_data.get("initiated_from", "client")

                # Burn state to prevent replay
                frappe.cache.delete_value(f"{OAUTH_STATE_CACHE_PREFIX}:{state}")

                if not client_name:
                    _respond_popup(
                        "Authorization Failed",
                        "Missing client identifier in signup session.",
                        "red",
                        {"type": "msuite_auth_error", "error": "missing_client"},
                    )
                    return

                app = frappe.db.get_value(
                    "MSuite App",
                    {"platform": "Meta WhatsApp", "is_active": 1},
                    ["redirect_uri"],
                    as_dict=True,
                ) or {}
                redirect_uri = app.get("redirect_uri")
                if not redirect_uri:
                    social_redirect = frappe.db.get_value("MSuite App", {"platform": "Meta Social", "is_active": 1}, "redirect_uri")
                    if social_redirect:
                        redirect_uri = social_redirect
                    else:
                        provider_base = (
                            (frappe.local.conf.host_name or frappe.local.conf.hostname or "").rstrip("/")
                            or frappe.utils.get_url().rstrip("/")
                        )
                        redirect_uri = f"{provider_base}/api/method/msuite.api.v1.auth.auth_callback"

                from msuite.services.oauth import exchange_whatsapp_code
                result = exchange_whatsapp_code(
                    client_name=client_name,
                    code=code,
                    session_info=None,
                    client_env=client_env,
                    redirect_uri=redirect_uri,
                )

                waba_count = result.get("waba_count", 0)
                _respond_popup(
                    "Connected Successfully",
                    f"Connected {waba_count} WhatsApp Business Account(s).",
                    "green",
                    {
                        "type": "msuite_connected",
                        "platform": "whatsapp",
                        "count": waba_count,
                        "accounts_saved": waba_count,
                    },
                )
                return

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


@frappe.whitelist(allow_guest=True)
def exchange_whatsapp(
    client_name: str = "",
    code: str = "",
    waba_id: str = "",
    phone_number_id: str = "",
    event: str = "",
    business_id: str = "",
    state: str = "",
) -> dict:
    """
    Exchange WhatsApp Embedded Signup authorization code.

    Two callers, two auth paths — but the SAME exchange logic underneath:

      1. **Provider admin** (`/connect` opened directly by an operator):
         No `state`; falls back to `require_system_manager_or_msuite_manager()`.
         `client_name` comes from the form's dropdown.

      2. **Client-initiated** (`/connect?state=…&launch=whatsapp`):
         `state` was minted by `start_whatsapp_embedded_signup`. Validating
         the state proves the caller came from a registered client (state
         only mints via `X-MSuite-Provider-Key/Secret` auth). `client_name`
         is read from the cached state, NOT the form — the operator role
         check is skipped because the state itself is the credential.

    Args:
        client_name: MSuite Client document name (admin flow only; ignored
            when `state` is present and valid).
        code: Authorization code from FB.login() callback.
        waba_id / phone_number_id / event / business_id: Optional
            session_info fields from FB's sessionInfoListener.
        state: One-time state token from `start_whatsapp_embedded_signup`.
            When present and valid, switches to client-initiated mode.

    Returns:
        Success dict with WABA + phone counts. When state-mode, also
        includes `redirect_to` so the page JS knows where to send the
        user after success.
    """
    try:
        # ── Auth + client_name resolution ────────────────────────────
        state_data: dict = {}
        if state:
            cached = frappe.cache.get_value(
                f"{OAUTH_STATE_CACHE_PREFIX}:{state}",
            )
            if not cached:
                return error_response(
                    ErrorCode.PERMISSION_DENIED,
                    "Signup session expired. Please retry from your "
                    "Connections page.",
                )
            try:
                state_data = json.loads(cached)
            except (TypeError, ValueError):
                return error_response(
                    ErrorCode.PERMISSION_DENIED,
                    "Signup session is corrupted. Please retry.",
                )
            if state_data.get("platform") != _WHATSAPP_STATE_PLATFORM:
                return error_response(
                    ErrorCode.PERMISSION_DENIED,
                    "Signup session is for a different platform.",
                )
            # Override client_name from state — the form's value is
            # ignored in client-initiated mode (the state is signed +
            # scoped to one client).
            client_name = state_data.get("client_name") or ""
            # Burn the state — single-use against replay. If the
            # downstream exchange fails the client can retry by going
            # back to the Connections page; we don't want a leaked
            # state token to authorise a second exchange.
            frappe.cache.delete_value(
                f"{OAUTH_STATE_CACHE_PREFIX}:{state}",
            )
        else:
            require_system_manager_or_msuite_manager()

        if not client_name:
            return error_response(
                ErrorCode.INVALID_INPUT,
                "client_name is required.",
            )
        if not code:
            return error_response(
                ErrorCode.INVALID_INPUT,
                "code is required.",
            )

        # ── Common exchange logic — unchanged ────────────────────────
        client_env = _capture_client_env()
        if state_data:
            # Audit trail: marks the consent record so support can tell
            # admin-initiated from client-initiated flows apart.
            client_env["initiated_from"] = state_data.get("initiated_from", "client")

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

        # Tell the page where to send the user next. Admin flow has no
        # return_url so the page stays put and shows the success alert.
        if state_data and state_data.get("return_url"):
            sep = "&" if "?" in state_data["return_url"] else "?"
            result = {
                **result,
                "redirect_to": (
                    f"{state_data['return_url']}{sep}"
                    f"status=connected&platform=whatsapp"
                    f"&waba_count={result.get('waba_count', 0)}"
                    f"&phone_count={result.get('phone_count', 0)}"
                ),
            }
        return success_response(result)
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        logger.error(f"exchange_whatsapp failed: {e}", exc_info=True)
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


@frappe.whitelist(allow_guest=True)
def get_connect_config(client_name: str, state: str = "") -> dict:
    """
    Return the rendering data for the `/connect` page.

    Emits a uniform list of supported platforms. For each, the frontend
    learns whether the platform is `ready` (MSuite App configured + OAuth
    handler registered). Secrets are never included — only app_ids and
    flow-level metadata the browser needs.

    Two auth paths (mirror of `exchange_whatsapp`):
      - With `state`: a cached client-initiated state proves the caller.
        We validate state platform == "whatsapp" and that the cached
        client_name matches the requested one. State is NOT consumed
        here — only by `exchange_whatsapp` at the end of the flow.
      - Without `state`: standard System Manager / MSuite Manager check.
    """
    try:
        if state:
            cached = frappe.cache.get_value(
                f"{OAUTH_STATE_CACHE_PREFIX}:{state}",
            )
            if not cached:
                return error_response(
                    ErrorCode.PERMISSION_DENIED,
                    "Signup session expired.",
                )
            try:
                state_data = json.loads(cached)
            except (TypeError, ValueError):
                return error_response(
                    ErrorCode.PERMISSION_DENIED,
                    "Signup session is corrupted.",
                )
            if state_data.get("platform") != _WHATSAPP_STATE_PLATFORM:
                return error_response(
                    ErrorCode.PERMISSION_DENIED,
                    "Signup session is for a different platform.",
                )
            if state_data.get("client_name") != client_name:
                return error_response(
                    ErrorCode.PERMISSION_DENIED,
                    "Signup session client mismatch.",
                )
        else:
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
            "name", "platform", "display_name as account_name", "account_id",
            "status", "auth_account.account_name as business_name", "connected_at",
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
            setTimeout(function() {{ window.close(); }}, 10000);
        </script>""",
        indicator_color=indicator,
    )
