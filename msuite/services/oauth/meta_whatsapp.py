"""
WhatsApp Embedded Signup handler.

Flow:
  1. Frontend calls FB.login() with config_id → user authorizes
  2. sessionInfoListener captures waba_id + phone_number_id (v2)
  3. authResponse returns authorization code
  4. Backend exchanges code → system user token (never expires)
  5. debug_token → extract permissions, user_id, WABA IDs
  6. For each WABA: subscribe webhooks, get details + phone numbers
  7. Create: Auth Account + Connected Account + WhatsApp Account
  8. Create: MSuite Signup Consent (audit record with permissions + environment)
  9. Push complete data to client (consent data stays on provider)
"""
import json
from datetime import datetime

import frappe
import requests
from frappe.utils import now

from msuite.constants import Platform, GRAPH_API_BASE, MSUITE_LOGGER_NAME
from msuite.exceptions import TokenExchangeError, AccountDiscoveryError

from .base import (
    get_msuite_app,
    upsert_auth_account,
    upsert_connected_account,
    upsert_whatsapp_account,
    push_account_to_client,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)


def exchange_code(
    client_name: str,
    code: str,
    session_info: dict | None = None,
    client_env: dict | None = None,
) -> dict:
    """
    Exchange WhatsApp Embedded Signup authorization code.

    Args:
        client_name: MSuite Client document name
        code: Authorization code from FB.login()
        session_info: Optional waba_id + phone_number_id from sessionInfoListener (v2)
        client_env: Optional dict with ip_address, user_agent, browser, platform_os, accept_language

    Returns:
        {"waba_count": N, "phone_count": N}
    """
    app = get_msuite_app("Meta WhatsApp")
    app_id = app.app_id
    app_secret = app.get_password("app_secret")

    # Step 1: Exchange code for system user access token
    resp = requests.get(
        f"{GRAPH_API_BASE}/oauth/access_token",
        params={"client_id": app_id, "client_secret": app_secret, "code": code},
        timeout=30,
    )
    token_data = resp.json()
    if "access_token" not in token_data:
        logger.error(f"WhatsApp token exchange failed: {token_data}")
        # Create consent record for failed exchange
        _create_consent_record(
            client_name=client_name,
            event_type="ERROR",
            consent_status="Failed",
            session_info=session_info,
            client_env=client_env,
            error_message=token_data.get("error", {}).get("message", "Token exchange failed"),
        )
        frappe.throw("Failed to exchange WhatsApp authorization code", TokenExchangeError)

    access_token = token_data["access_token"]

    # Step 2: debug_token — get permissions, user_id, WABA IDs
    debug_data = _debug_token(access_token, app_id, app_secret)

    # Step 3: Determine WABA IDs — prefer session_info (v2) over debug_token
    waba_ids = []
    if session_info and session_info.get("waba_id"):
        waba_ids = [session_info["waba_id"]]
    else:
        # Extract from granular_scopes
        for scope in debug_data.get("granular_scopes", []):
            if scope.get("scope") == "whatsapp_business_management":
                waba_ids = scope.get("target_ids", [])
                break

    if not waba_ids:
        frappe.throw("No WhatsApp Business Accounts found", AccountDiscoveryError)

    # Step 4: Process each WABA
    total_phones = 0
    connected_account_names = []
    for waba_id in waba_ids:
        phones, ca_name = _process_waba(client_name, waba_id, access_token, app.name)
        total_phones += phones
        if ca_name:
            connected_account_names.append(ca_name)

    # Step 5: Create consent record
    # Enrich session_info with data discovered during processing
    enriched_session = dict(session_info or {})
    if not enriched_session.get("waba_id") and waba_ids:
        enriched_session["waba_id"] = waba_ids[0]
    # Fill phone_number_id from the first phone in the first WABA if not in session
    if not enriched_session.get("phone_number_id") and connected_account_names:
        first_phone = frappe.db.get_value(
            "MSuite WhatsApp Phone",
            {"parent": frappe.db.get_value("MSuite WhatsApp Account", {"connected_account": connected_account_names[0]})},
            "phone_number_id",
        )
        if first_phone:
            enriched_session["phone_number_id"] = first_phone

    _create_consent_record(
        client_name=client_name,
        event_type=_map_event_type(session_info),
        consent_status="Granted",
        session_info=enriched_session,
        client_env=client_env,
        debug_data=debug_data,
        connected_account=connected_account_names[0] if connected_account_names else None,
        raw_auth_response={"token_type": token_data.get("token_type"), "has_token": True},
    )

    frappe.db.commit()
    logger.info(
        f"WhatsApp Embedded Signup for {client_name}: "
        f"{len(waba_ids)} WABAs, {total_phones} phones"
    )
    return {"waba_count": len(waba_ids), "phone_count": total_phones}


# ── Consent record creation ──────────────────────────────────────────────


def create_cancel_consent(client_name: str, session_info: dict, client_env: dict | None = None):
    """Create a consent record for a CANCEL event (called from auth API)."""
    _create_consent_record(
        client_name=client_name,
        event_type="CANCEL",
        consent_status="Cancelled",
        session_info=session_info,
        client_env=client_env,
        cancelled_step=session_info.get("current_step", ""),
    )
    frappe.db.commit()


def create_error_consent(client_name: str, session_info: dict, client_env: dict | None = None):
    """Create a consent record for an ERROR event (called from auth API)."""
    _create_consent_record(
        client_name=client_name,
        event_type="ERROR",
        consent_status="Failed",
        session_info=session_info,
        client_env=client_env,
        error_message=session_info.get("error_message", ""),
    )
    frappe.db.commit()


def _create_consent_record(
    client_name: str,
    event_type: str,
    consent_status: str,
    session_info: dict | None = None,
    client_env: dict | None = None,
    debug_data: dict | None = None,
    connected_account: str | None = None,
    raw_auth_response: dict | None = None,
    error_message: str = "",
    cancelled_step: str = "",
):
    """Create an MSuite Signup Consent record."""
    session_info = session_info or {}
    client_env = client_env or {}
    debug_data = debug_data or {}

    doc = frappe.new_doc("MSuite Signup Consent")
    doc.client = client_name
    doc.connected_account = connected_account
    doc.platform = Platform.WHATSAPP
    doc.consent_status = consent_status
    doc.consented_at = now()

    # Event data
    doc.event_type = event_type
    doc.waba_id = session_info.get("waba_id", "")
    doc.phone_number_id = session_info.get("phone_number_id", "")
    doc.business_id = session_info.get("business_id", "")
    doc.cancelled_step = cancelled_step
    doc.error_message = error_message

    # Permissions from debug_token
    doc.meta_user_id = str(debug_data.get("user_id", ""))
    doc.granted_scopes = json.dumps(debug_data.get("scopes", []))
    doc.granular_scopes = json.dumps(debug_data.get("granular_scopes", []))
    doc.token_valid = 1 if debug_data.get("is_valid") else 0

    # data_access_expires_at is a unix timestamp (0 = never expires)
    expires_ts = debug_data.get("data_access_expires_at")
    if expires_ts and int(expires_ts) > 0:
        try:
            doc.data_access_expires = datetime.fromtimestamp(int(expires_ts))
        except (ValueError, TypeError, OSError):
            pass

    # Client environment
    doc.ip_address = client_env.get("ip_address", "")
    doc.user_agent_string = client_env.get("user_agent", "")
    doc.browser = client_env.get("browser", "")
    doc.platform_os = client_env.get("platform_os", "")
    doc.accept_language = client_env.get("accept_language", "")

    # Audit trail (sanitized — no tokens)
    doc.raw_debug_token = json.dumps(debug_data) if debug_data else ""
    doc.raw_auth_response = json.dumps(raw_auth_response) if raw_auth_response else ""

    doc.insert(ignore_permissions=True)
    return doc.name


def _map_event_type(session_info: dict | None) -> str:
    """Map session_info event to our consent event_type."""
    if not session_info:
        return "FINISH"
    event = session_info.get("event", "")
    if event == "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING":
        return "COEXISTENCE"
    if event == "FINISH_ONLY_WABA":
        return "FINISH_ONLY_WABA"
    return "FINISH"


# ── debug_token ──────────────────────────────────────────────────────────


def _debug_token(access_token: str, app_id: str, app_secret: str) -> dict:
    """
    Call debug_token to get permissions, user_id, and WABA IDs.

    Returns the 'data' object from the response, or empty dict on failure.
    """
    try:
        resp = requests.get(
            f"{GRAPH_API_BASE}/debug_token",
            params={
                "input_token": access_token,
                "access_token": f"{app_id}|{app_secret}",
            },
            timeout=30,
        )
        return resp.json().get("data", {})
    except Exception as e:
        logger.error(f"debug_token call failed: {e}")
        return {}


# ── WABA processing ──────────────────────────────────────────────────────


def _process_waba(
    client_name: str,
    waba_id: str,
    access_token: str,
    app_name: str,
) -> tuple[int, str | None]:
    """
    Process one WABA: subscribe, fetch details, create records, push to client.

    Returns:
        (phone_count, connected_account_name)
    """
    # Subscribe provider app to WABA webhooks
    try:
        requests.post(
            f"{GRAPH_API_BASE}/{waba_id}/subscribed_apps",
            params={"access_token": access_token},
            timeout=30,
        )
    except Exception as e:
        logger.warning(f"Failed to subscribe to WABA {waba_id}: {e}")

    # Fetch WABA details (with fallback if owner_business_info not available)
    waba_name, waba_currency, waba_timezone, biz_id, biz_name = _fetch_waba_details(
        waba_id, access_token
    )

    # Fetch phone numbers
    phone_rows = _fetch_phone_numbers(waba_id, access_token)

    # Build a meaningful display name: WABA name > first phone's verified name > WABA ID
    display_name = (
        waba_name
        or (phone_rows[0]["verified_name"] if phone_rows else "")
        or f"WABA {waba_id}"
    )

    # Always create Auth Account — use business ID if available, WABA ID as fallback
    auth_id = biz_id or waba_id
    auth_display = biz_name or display_name
    auth_account_name = upsert_auth_account(client_name, "Meta", auth_id, {
        "account_name": auth_display,
    })

    # Create/update Connected Account (WABA-level, holds the token)
    ca_name = upsert_connected_account(client_name, Platform.WHATSAPP, waba_id, {
        "display_name": display_name,
        "auth_account": auth_account_name,
        "access_token": access_token,
        "token_type": "System User",
        "token_expiry": None,
        "msuite_app": app_name,
    })

    # Create/update WhatsApp Account (WABA details + phone child rows)
    upsert_whatsapp_account(
        connected_account_name=ca_name,
        waba_id=waba_id,
        waba_name=waba_name or display_name,
        waba_currency=waba_currency,
        waba_timezone=waba_timezone,
        phones=phone_rows,
    )

    # Push to client (consent data stays on provider — never pushed)
    # Include app_id so client can upload media for templates
    meta_app_id = frappe.db.get_value("MSuite App", app_name, "app_id") or ""
    push_account_to_client(client_name, Platform.WHATSAPP, {
        "waba_id": waba_id,
        "waba_name": waba_name or display_name,
        "waba_currency": waba_currency,
        "waba_timezone": waba_timezone,
        "business_id": biz_id,
        "business_name": biz_name or display_name,
        "app_id": meta_app_id,
        "system_user_token": access_token,
        "phones": [
            {
                "phone_number_id": p["phone_number_id"],
                "display_phone_number": p["display_phone_number"],
                "verified_name": p["verified_name"],
                "quality_rating": p["quality_rating"],
                "messaging_limit_tier": p["messaging_limit_tier"],
                "webhook_verify_token": p["webhook_verify_token"],
            }
            for p in phone_rows
        ],
    })

    return len(phone_rows), ca_name


def _fetch_waba_details(waba_id: str, access_token: str):
    """
    Fetch WABA details from Meta Graph API.

    Two-step fallback:
      1. Try with owner_business_info{name,id} expansion
      2. If that fails (400), retry without business info

    Returns:
        (waba_name, waba_currency, waba_timezone, biz_id, biz_name)
    """
    waba_name, waba_currency, waba_timezone = "", "", ""
    biz_id, biz_name = "", ""

    # Attempt 1: with business info
    try:
        resp = requests.get(
            f"{GRAPH_API_BASE}/{waba_id}",
            params={
                "access_token": access_token,
                "fields": "name,currency,timezone_id,owner_business_info{name,id}",
            },
            timeout=30,
        )
        data = resp.json()

        if resp.status_code == 200 and "error" not in data:
            waba_name = data.get("name", "")
            waba_currency = data.get("currency", "")
            waba_timezone = data.get("timezone_id", "")
            owner_biz = data.get("owner_business_info", {})
            biz_id = owner_biz.get("id", "")
            biz_name = owner_biz.get("name", "")
            return waba_name, waba_currency, waba_timezone, biz_id, biz_name

        # Attempt 2: without business info (some WABAs don't support it)
        logger.warning(
            f"WABA {waba_id} details with business info failed ({resp.status_code}), "
            f"retrying without: {data.get('error', {}).get('message', '')}"
        )
    except Exception as e:
        logger.error(f"WABA {waba_id} details request failed: {e}")

    try:
        resp = requests.get(
            f"{GRAPH_API_BASE}/{waba_id}",
            params={
                "access_token": access_token,
                "fields": "name,currency,timezone_id",
            },
            timeout=30,
        )
        data = resp.json()
        if resp.status_code == 200 and "error" not in data:
            waba_name = data.get("name", "")
            waba_currency = data.get("currency", "")
            waba_timezone = data.get("timezone_id", "")
    except Exception as e:
        logger.error(f"WABA {waba_id} details fallback also failed: {e}")

    return waba_name, waba_currency, waba_timezone, biz_id, biz_name


def _fetch_phone_numbers(waba_id: str, access_token: str) -> list[dict]:
    """Fetch phone numbers for a WABA and return formatted rows."""
    phone_list = []
    try:
        resp = requests.get(
            f"{GRAPH_API_BASE}/{waba_id}/phone_numbers",
            params={
                "access_token": access_token,
                "fields": "id,display_phone_number,verified_name,quality_rating,"
                          "code_verification_status,platform_type,messaging_limit_tier",
            },
            timeout=30,
        )
        phone_list = resp.json().get("data", [])
    except Exception as e:
        logger.error(f"Failed to get phone numbers for WABA {waba_id}: {e}")

    return [
        {
            "phone_number_id": phone["id"],
            "display_phone_number": phone.get("display_phone_number", ""),
            "verified_name": phone.get("verified_name", ""),
            "quality_rating": phone.get("quality_rating", ""),
            "messaging_limit_tier": phone.get("messaging_limit_tier", ""),
            "code_verification_status": phone.get("code_verification_status", ""),
            "platform_type": phone.get("platform_type", "CLOUD_API"),
            "webhook_verify_token": frappe.generate_hash(length=20),
            "status": "Active",
        }
        for phone in phone_list
    ]
