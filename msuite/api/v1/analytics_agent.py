"""
analytics_agent.py — Provider-side Analytics Agent API & Routing Hub.

Acts as the single gateway between:
  1. Authenticated client sites (msuite_workspace)
  2. The standalone AI Analytics Docker microservice (analytics_agent)
  3. Centralized query audit logging (MSuite Analytics Agent Query Log)
"""
from __future__ import annotations

import hmac
import json
import time
import requests

import frappe
from frappe.utils import now_datetime

from msuite.constants import MSUITE_LOGGER_NAME
from msuite.services import client_service
from msuite.services.entitlement_service import get_customer_entitlements
from msuite.utils.validators import (
    error_response,
    require_msuite_client_auth,
    success_response,
)

from msuite.api.v1.ai_router import route_user_query

logger = frappe.logger(MSUITE_LOGGER_NAME)


def _require_mcp_backend_key():
    """
    Authenticate a trusted backend caller (the msuite-mcp-server) on
    endpoints that resolve or forward using a tenant's real MSuite Client
    credentials. The caller must send `mcp_backend_shared_key` (from this
    site's site_config) as the `X-MCP-Backend-Key` header. Fails closed:
    if the key isn't configured here, every gated endpoint is rejected —
    these endpoints hand out or use plaintext tenant api_secrets and must
    never be reachable unauthenticated.
    """
    expected = frappe.conf.get("mcp_backend_shared_key")
    if not expected:
        logger.error(
            "[MCP Backend] Gated endpoint called but mcp_backend_shared_key is not "
            "configured — rejecting. Set it in site_config and on the MCP server."
        )
        frappe.throw(
            "MCP backend authentication is not configured on this provider.",
            frappe.AuthenticationError,
        )
    sent = (frappe.request.headers.get("X-MCP-Backend-Key") or "").strip()
    if not (sent and hmac.compare_digest(sent, str(expected))):
        frappe.throw("Invalid or missing X-MCP-Backend-Key.", frappe.AuthenticationError)


def _get_microservice_url() -> str:
    """Read microservice base URL from site_config or environment."""
    url = frappe.conf.get("analytics_agent_url") or frappe.conf.get("ai_analytics_bot_url") or "http://127.0.0.1:8004"
    return url.rstrip("/")


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def get_analytics_agent_config(client_identifier: str) -> dict:
    """Return whether the Analytics Agent is enabled for this client's plan."""
    try:
        client_doc = require_msuite_client_auth(client_identifier)
    except frappe.AuthenticationError as exc:
        return error_response("AUTH_FAILED", str(exc))

    entitlements = get_customer_entitlements(client_doc.customer)
    features = entitlements.get("features", {})
    agent_feature = features.get("analytics_agent") or features.get("ai_analytics_bot", {})
    is_enabled = bool(agent_feature.get("enabled", False))

    return success_response({
        "enabled": is_enabled,
        "plan_name": ", ".join(entitlements.get("active_plans", [])) or "No active plan",
        "client_code": client_doc.client_code or client_doc.name,
    })


@frappe.whitelist(allow_guest=True, methods=["POST"])
def chat(
    client_identifier: str,
    message: str,
    session_id: str | None = None,
    user_email: str | None = None,
    conversation_history: list | str | None = None,
    current_page: str | None = None,
) -> dict:
    """
    Main user chat entrypoint.
    1. Authenticates client credentials.
    2. Validates plan entitlement for 'analytics_agent'.
    3. Routes query intelligently via ai_router to either assistant_agent or analytics_agent.
    4. Logs the query, intent & response to MSuite Analytics Agent Query Log.
    """
    start_time = time.time()
    try:
        client_doc = require_msuite_client_auth(client_identifier)
    except frappe.AuthenticationError as exc:
        return error_response("AUTH_FAILED", str(exc))

    # Entitlement verification
    entitlements = get_customer_entitlements(client_doc.customer)
    features = entitlements.get("features", {})
    agent_feature = features.get("analytics_agent") or features.get("ai_analytics_bot", {})
    if not agent_feature.get("enabled", False):
        return error_response(
            "FEATURE_NOT_ENTITLED",
            "The Analytics Agent is not included in your current plan. Please upgrade to Pro or above.",
        )

    if isinstance(conversation_history, str):
        try:
            conversation_history = json.loads(conversation_history)
        except Exception:
            conversation_history = []

    client_code = client_doc.client_code or client_doc.name

    # Route through Smart Intent Router
    route_result = route_user_query(
        client_code=client_code,
        message=(message or "").strip(),
        current_page=current_page,
        session_id=session_id or "",
        user_email=user_email or "",
        conversation_history=conversation_history or [],
    )

    bot_response_text = route_result.get("reply", "")
    tools_invoked = route_result.get("tools_used", [])
    model_used = route_result.get("model_used", "")
    options_list = route_result.get("options", [])
    prompt_tokens = route_result.get("prompt_tokens", 0)
    completion_tokens = route_result.get("completion_tokens", 0)
    status = route_result.get("status", "Success")
    error_msg = route_result.get("error_message")
    intent = route_result.get("intent", "general_chat")
    latency_ms = route_result.get("latency_ms", int((time.time() - start_time) * 1000))

    # Centralized audit logging on Provider
    log_doc_name = None
    try:
        log_payload = {
            "doctype": "MSuite Analytics Agent Query Log",
            "client": client_doc.name,
            "client_code": client_code,
            "user_email": user_email or "",
            "session_id": session_id or "",
            "user_question": message,
            "bot_response": bot_response_text,
            "tools_invoked": json.dumps(tools_invoked),
            "model_used": model_used,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": latency_ms,
            "status": status,
            "error_message": error_msg or "",
        }
        if frappe.db.has_column("MSuite Analytics Agent Query Log", "intent"):
            log_payload["intent"] = intent

        log_doc = frappe.get_doc(log_payload)
        log_doc.insert(ignore_permissions=True)
        frappe.db.commit()
        log_doc_name = log_doc.name
    except Exception as exc:
        logger.error(f"[Analytics Agent] Failed to write query log: {exc}")

    return success_response({
        "reply": bot_response_text,
        "tools_used": tools_invoked,
        "model_used": model_used,
        "latency_ms": latency_ms,
        "log_id": log_doc_name or "",
        "options": options_list,
        "intent": intent,
    })


@frappe.whitelist(allow_guest=True, methods=["POST"])
def proxy_tool_call(
    client_code: str,
    method: str,
    params: str | dict | None = None,
) -> dict:
    """
    Proxy an analytics tool execution from a trusted backend to the target client.
    Uses stored credentials in MSuite Client to ensure secure, tenant-isolated access.
    Gated by `_require_mcp_backend_key()` — this endpoint hands a caller's request
    through with the target tenant's real provider-auth credentials attached, so it
    must never be reachable without proving the caller is a trusted backend first.
    """
    _require_mcp_backend_key()
    doc_name = client_service._resolve_client_doc_name(client_code) if hasattr(client_service, "_resolve_client_doc_name") else None
    if not doc_name:
        doc_name = frappe.db.get_value("MSuite Client", {"client_code": client_code}, "name") or client_code

    if not frappe.db.exists("MSuite Client", doc_name):
        return error_response("CLIENT_NOT_FOUND", f"Client '{client_code}' not found on provider.")

    client_doc = frappe.get_doc("MSuite Client", doc_name)
    if client_doc.status != "Active":
        return error_response("CLIENT_NOT_ACTIVE", f"Client '{client_code}' is {client_doc.status}.")

    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    elif not params:
        params = {}

    headers = client_service.make_auth_headers(client_doc)

    try:
        result = client_service._post_to_client(
            client_doc.client_url,
            method,
            params,
            headers=headers,
        )
        return success_response(result)
    except Exception as exc:
        logger.error(f"[Analytics Agent] Tool proxy failed for {client_code} on {method}: {exc}")
        return error_response("TOOL_PROXY_FAILED", str(exc))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def get_mcp_client_credentials(client_code: str) -> dict:
    """
    Return a tenant's raw Frappe REST credentials to the trusted msuite-mcp-server
    so it can call that tenant's `agent_facade` directly, the same way it already
    calls its one single-tenant `.env`-configured site today — just resolved
    per-`client_code` instead of hardcoded. Gated by `_require_mcp_backend_key()`:
    this hands out a decrypted `api_secret`, so it must never be reachable
    unauthenticated.
    """
    _require_mcp_backend_key()

    doc_name = frappe.db.get_value("MSuite Client", {"client_code": client_code}, "name") or (
        client_code if frappe.db.exists("MSuite Client", client_code) else None
    )
    if not doc_name:
        return error_response("CLIENT_NOT_FOUND", f"Client '{client_code}' not found on provider.")

    client_doc = frappe.get_doc("MSuite Client", doc_name)
    if client_doc.status != "Active":
        return error_response("CLIENT_NOT_ACTIVE", f"Client '{client_code}' is {client_doc.status}.")

    from frappe.utils.password import get_decrypted_password

    api_secret = get_decrypted_password("MSuite Client", client_doc.name, "api_secret", raise_exception=False)
    if not client_doc.api_key or not api_secret:
        return error_response("CREDENTIALS_MISSING", f"Client '{client_code}' has no stored API credentials.")

    return success_response({
        "client_url": client_doc.client_url,
        "api_key": client_doc.api_key,
        "api_secret": api_secret,
    })


@frappe.whitelist(allow_guest=True, methods=["POST"])
def submit_feedback(client_identifier: str, log_id: str, rating: str) -> dict:
    """Record user thumbs up / down feedback on a previous query."""
    try:
        require_msuite_client_auth(client_identifier)
    except frappe.AuthenticationError as exc:
        return error_response("AUTH_FAILED", str(exc))

    if not frappe.db.exists("MSuite Analytics Agent Query Log", log_id):
        return error_response("NOT_FOUND", f"Query log {log_id} not found.")

    rating_clean = "Thumbs Up" if "up" in (rating or "").lower() else "Thumbs Down"
    frappe.db.set_value("MSuite Analytics Agent Query Log", log_id, "user_rating", rating_clean)
    frappe.db.commit()

    return success_response({"message": "Feedback recorded."})


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def get_chat_history(
    client_identifier: str,
    session_id: str | None = None,
    user_email: str | None = None,
    limit: int = 50,
) -> dict:
    """
    Retrieve stored chat conversation history from MariaDB (MSuite Analytics Agent Query Log).
    """
    try:
        client_doc = require_msuite_client_auth(client_identifier)
    except frappe.AuthenticationError as exc:
        return error_response("AUTH_FAILED", str(exc))

    filters = {"client": client_doc.name}
    if session_id:
        filters["session_id"] = session_id
    elif user_email:
        filters["user_email"] = user_email

    logs = frappe.get_all(
        "MSuite Analytics Agent Query Log",
        filters=filters,
        fields=[
            "name",
            "session_id",
            "user_email",
            "user_question",
            "bot_response",
            "tools_invoked",
            "model_used",
            "user_rating",
            "creation",
        ],
        order_by="creation asc",
        limit=int(limit or 50),
    )

    formatted_messages = []
    for log in logs:
        # User turn
        if log.user_question:
            formatted_messages.append({
                "role": "user",
                "text": log.user_question,
                "timestamp": str(log.creation),
            })
        # Assistant turn
        if log.bot_response:
            tools = []
            if log.tools_invoked:
                try:
                    tools = json.loads(log.tools_invoked)
                except Exception:
                    tools = []
            formatted_messages.append({
                "role": "assistant",
                "text": log.bot_response,
                "tools_used": tools,
                "model_used": log.model_used or "",
                "log_id": log.name,
                "user_rating": log.user_rating or "None",
                "timestamp": str(log.creation),
            })

    return success_response({
        "messages": formatted_messages,
        "session_id": session_id or "",
        "total_turns": len(logs),
    })
