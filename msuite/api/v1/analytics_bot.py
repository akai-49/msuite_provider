"""
analytics_bot.py — Provider-side AI Analytics Bot API & Routing Hub.

Acts as the single gateway between:
  1. Authenticated client sites (msuite_workspace)
  2. The standalone AI Analytics Docker microservice
  3. Centralized query audit logging (MSuite AI Analytics Query Log)
"""
from __future__ import annotations

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

logger = frappe.logger(MSUITE_LOGGER_NAME)


def _get_microservice_url() -> str:
    """Read microservice base URL from site_config or environment."""
    url = frappe.conf.get("ai_analytics_bot_url") or "http://127.0.0.1:8004"
    return url.rstrip("/")


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def get_analytics_bot_config(client_identifier: str) -> dict:
    """Return whether the AI Analytics Bot is enabled for this client's plan."""
    try:
        client_doc = require_msuite_client_auth(client_identifier)
    except frappe.AuthenticationError as exc:
        return error_response("AUTH_FAILED", str(exc))

    entitlements = get_customer_entitlements(client_doc.customer)
    features = entitlements.get("features", {})
    bot_feature = features.get("ai_analytics_bot", {})
    is_enabled = bool(bot_feature.get("enabled", False))

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
) -> dict:
    """
    Main user chat entrypoint.
    1. Authenticates client credentials.
    2. Validates plan entitlement for 'ai_analytics_bot'.
    3. Forwards to Docker microservice.
    4. Logs the query & response to MSuite AI Analytics Query Log.
    """
    start_time = time.time()
    try:
        client_doc = require_msuite_client_auth(client_identifier)
    except frappe.AuthenticationError as exc:
        return error_response("AUTH_FAILED", str(exc))

    # Entitlement verification
    entitlements = get_customer_entitlements(client_doc.customer)
    features = entitlements.get("features", {})
    bot_feature = features.get("ai_analytics_bot", {})
    if not bot_feature.get("enabled", False):
        return error_response(
            "FEATURE_NOT_ENTITLED",
            "The AI Analytics Bot is not included in your current plan. Please upgrade to Pro or above.",
        )

    if isinstance(conversation_history, str):
        try:
            conversation_history = json.loads(conversation_history)
        except Exception:
            conversation_history = []

    microservice_url = _get_microservice_url()
    client_code = client_doc.client_code or client_doc.name
    endpoint = f"{microservice_url}/analytics/chat"

    payload = {
        "client_code": client_code,
        "message": (message or "").strip(),
        "session_id": session_id or "",
        "user_email": user_email or "",
        "conversation_history": conversation_history or [],
    }

    status = "Success"
    error_msg = None
    bot_response_text = ""
    tools_invoked = []
    model_used = ""
    prompt_tokens = 0
    completion_tokens = 0

    try:
        resp = requests.post(
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=120,
        )
        if resp.status_code != 200:
            status = "Tool Error"
            error_msg = f"Microservice HTTP {resp.status_code}: {resp.text[:300]}"
            bot_response_text = "Sorry, I encountered an issue processing your analytics request."
        else:
            data = resp.json()
            bot_response_text = data.get("reply", "")
            tools_invoked = data.get("tools_used", [])
            model_used = data.get("model_used", "")
            options_list = data.get("options", [])
            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
    except Exception as exc:
        logger.exception(f"[AI Analytics Bot] Error contacting microservice at {endpoint}")
        status = "Tool Error"
        error_msg = str(exc)
        bot_response_text = "I could not reach the analytics engine. Please ensure the service is running."
        options_list = []

    latency_ms = int((time.time() - start_time) * 1000)

    # Centralized audit logging on Provider
    log_doc_name = None
    try:
        log_doc = frappe.get_doc({
            "doctype": "MSuite AI Analytics Query Log",
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
        })
        log_doc.insert(ignore_permissions=True)
        frappe.db.commit()
        log_doc_name = log_doc.name
    except Exception as exc:
        logger.error(f"[AI Analytics Bot] Failed to write query log: {exc}")

    return success_response({
        "reply": bot_response_text,
        "tools_used": tools_invoked,
        "model_used": model_used,
        "latency_ms": latency_ms,
        "log_id": log_doc_name or "",
        "options": options_list,
    })


@frappe.whitelist(allow_guest=True, methods=["POST"])
def proxy_tool_call(
    client_code: str,
    method: str,
    params: str | dict | None = None,
) -> dict:
    """
    Proxy an analytics tool execution from the AI microservice to the target client.
    Uses stored credentials in MSuite Client to ensure secure, tenant-isolated access.
    """
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
        logger.error(f"[AI Analytics Bot] Tool proxy failed for {client_code} on {method}: {exc}")
        return error_response("TOOL_PROXY_FAILED", str(exc))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def submit_feedback(client_identifier: str, log_id: str, rating: str) -> dict:
    """Record user thumbs up / down feedback on a previous query."""
    try:
        require_msuite_client_auth(client_identifier)
    except frappe.AuthenticationError as exc:
        return error_response("AUTH_FAILED", str(exc))

    if not frappe.db.exists("MSuite AI Analytics Query Log", log_id):
        return error_response("NOT_FOUND", f"Query log {log_id} not found.")

    rating_clean = "Thumbs Up" if "up" in (rating or "").lower() else "Thumbs Down"
    frappe.db.set_value("MSuite AI Analytics Query Log", log_id, "user_rating", rating_clean)
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
    Retrieve stored chat conversation history from MariaDB (MSuite AI Analytics Query Log).
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
        "MSuite AI Analytics Query Log",
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
