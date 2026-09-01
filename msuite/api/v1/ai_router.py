"""
ai_router.py — Smart Backend Intent Router for MSuite AI Services.

Classifies incoming user queries and dispatches them to the appropriate specialized engine:
1. Documentation RAG (assistant_agent on port 8005) for how-to, setup, navigation, and guidance.
2. Marketing Analytics (analytics_agent on port 8004) for metrics, spend, delivery rates, and performance.
"""
from __future__ import annotations

import re
import time
import requests
import frappe
from msuite.constants import MSUITE_LOGGER_NAME

def _get_logger():
    try:
        return frappe.logger(MSUITE_LOGGER_NAME)
    except Exception:
        import logging
        return logging.getLogger("msuite")

logger = _get_logger()

INTENT_HELP = "help_guidance"
INTENT_ANALYTICS = "analytics_metrics"
INTENT_GENERAL = "general_chat"

# Precompiled regex patterns for sub-millisecond intent classification
_HELP_REGEXES = [
    re.compile(r"\bhow\s+(?:to|do\s+i|can\s+i|should\s+i)\b", re.IGNORECASE),
    re.compile(r"\bwhere\s+(?:is|do\s+i|can\s+i|to\s+find)\b", re.IGNORECASE),
    re.compile(r"\b(?:steps\s+to|guide\s+to|instructions\s+for|how\s+does.*work)\b", re.IGNORECASE),
    re.compile(r"\b(?:connect\s+(?:whatsapp|meta|facebook|instagram|sms|channel|account|number|waba))\b", re.IGNORECASE),
    re.compile(r"\b(?:setup|configure|configuration|walkthrough|tutorial|documentation|docs)\b", re.IGNORECASE),
    re.compile(r"\bwhere\s+can\s+i\s+(?:see|find|configure|create|add)\b", re.IGNORECASE),
    re.compile(r"\bexplain\s+(?:how|what|where)\b", re.IGNORECASE),
]

_ANALYTICS_REGEXES = [
    re.compile(r"\bhow\s+(?:many|much)\b", re.IGNORECASE),
    re.compile(r"\b(?:spend|spent|cost|budget\s+spent|ad\s+spend)\b", re.IGNORECASE),
    re.compile(r"\b(?:ctr|cpc|roas|cpm|cpa)\b", re.IGNORECASE),
    re.compile(r"\b(?:impressions?|clicks?|conversions?|leads?\s+generated)\b", re.IGNORECASE),
    re.compile(r"\b(?:delivery\s+rate|delivered|read\s+rate|failed\s+messages?|bounce\s+rate|open\s+rate|unsubscribes?)\b", re.IGNORECASE),
    re.compile(r"\b(?:analytics|metrics|statistics|stats|performance|summary|trends?|dashboard)\b", re.IGNORECASE),
    re.compile(r"\b(?:last\s+\d+\s+days?|yesterday|this\s+week|this\s+month|past\s+week)\b", re.IGNORECASE),
]

_GREETING_REGEXES = [
    re.compile(r"^(?:hi|hello|hey|greetings|good\s+(?:morning|afternoon|evening))\b", re.IGNORECASE),
    re.compile(r"^(?:who\s+are\s+you|what\s+can\s+you\s+do|help)\??$", re.IGNORECASE),
]


def classify_intent(message: str) -> str:
    """
    Classify user message intent into:
      - 'help_guidance' (How-to, UI navigation, setup)
      - 'analytics_metrics' (Spend, stats, performance)
      - 'general_chat' (Greetings, open questions)
    """
    clean_msg = (message or "").strip()
    if not clean_msg:
        return INTENT_GENERAL

    # Check greetings / self-identification first
    for pattern in _GREETING_REGEXES:
        if pattern.search(clean_msg):
            # If greeting also contains a substantive question, let it classify further
            if len(clean_msg.split()) <= 4:
                return INTENT_GENERAL

    help_score = sum(1 for p in _HELP_REGEXES if p.search(clean_msg))
    analytics_score = sum(1 for p in _ANALYTICS_REGEXES if p.search(clean_msg))

    # Procedural questions ("how can i send bulk whatsapp message", "how to connect")
    if help_score > 0 and analytics_score == 0:
        return INTENT_HELP

    # Metric inquiries ("what was my spend", "how many delivered")
    if analytics_score > 0 and help_score == 0:
        return INTENT_ANALYTICS

    # If both patterns matched, evaluate precedence:
    # "how to / how can i / where is" strongly indicates guidance even if a metric word appears
    if help_score > 0:
        if re.search(r"\b(?:how\s+(?:to|do\s+i|can\s+i)|where\s+(?:is|do\s+i))\b", clean_msg, re.IGNORECASE):
            # E.g. "How do I see my delivery rate?" -> Help/navigation instructions
            return INTENT_HELP
        if re.search(r"\b(?:how\s+(?:many|much))\b", clean_msg, re.IGNORECASE):
            # E.g. "How many messages were delivered?" -> Analytics metric
            return INTENT_ANALYTICS

    if analytics_score > help_score:
        return INTENT_ANALYTICS
    elif help_score > 0:
        return INTENT_HELP

    # Default fallback for unclassified queries
    return INTENT_HELP


def _safe_get_conf(key: str, default: str = "") -> str:
    try:
        if hasattr(frappe, "conf") and frappe.conf:
            return frappe.conf.get(key) or default
    except Exception:
        pass
    return default


def get_analytics_agent_url() -> str:
    """Base URL for analytics_agent microservice (port 8004)."""
    url = _safe_get_conf("analytics_agent_url") or _safe_get_conf("ai_analytics_bot_url")
    return (url or "http://127.0.0.1:8004").rstrip("/")


def get_assistant_agent_url() -> str:
    """Base URL for assistant_agent microservice (port 8005)."""
    url = _safe_get_conf("assistant_agent_url") or _safe_get_conf("ai_assistant_url")
    return (url or "http://127.0.0.1:8005").rstrip("/")


def dispatch_help_query(
    message: str,
    current_page: str | None = None,
    session_id: str | None = None,
) -> dict:
    """
    Dispatch query to assistant_agent (RAG over knowledge/docs).
    """
    base_url = get_assistant_agent_url()
    endpoint = f"{base_url}/chat"
    payload = {
        "session_id": session_id or "help_session",
        "message": message,
        "current_page": current_page or None,
    }

    try:
        resp = requests.post(endpoint, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "reply": data.get("reply", ""),
                "tools_used": ["search_docs"] if current_page is None else ["search_docs", "get_doc_by_route"],
                "model_used": "assistant_agent/rag",
                "options": [
                    {"label": "How to connect WhatsApp", "value": "How to connect WhatsApp"},
                    {"label": "How to send bulk WhatsApp message", "value": "How can I send bulk WhatsApp message?"},
                    {"label": "View Live Marketing Analytics", "value": "Show my marketing analytics overview"},
                ],
                "status": "Success",
                "error_message": None,
            }
        else:
            return {
                "reply": f"Assistant service returned status {resp.status_code}.",
                "tools_used": [],
                "model_used": "assistant_agent/rag",
                "options": [],
                "status": "Tool Error",
                "error_message": resp.text[:300],
            }
    except Exception as exc:
        logger.warning(f"[AI Router] Failed to reach assistant_agent at {endpoint}: {exc}")
        return {
            "reply": (
                "Here is guidance for your request:\n\n"
                "- **Connect WhatsApp**: Go to **Settings → Connections** (`/connections`) and click **Connect Number**.\n"
                "- **Send Bulk WhatsApp**: Go to **WhatsApp → Bulk Messages** (`/whatsapp/bulk-messages`) and click **+ New Bulk Message**.\n"
                "- **Audiences**: Manage recipient lists under **Campaign → Audiences**."
            ),
            "tools_used": ["static_help_manifest"],
            "model_used": "assistant_agent/fallback",
            "options": [],
            "status": "Success",
            "error_message": str(exc),
        }


def dispatch_analytics_query(
    client_code: str,
    message: str,
    session_id: str | None = None,
    user_email: str | None = None,
    conversation_history: list | None = None,
) -> dict:
    """
    Dispatch query to analytics_agent (Tool-calling live metrics engine).
    """
    base_url = get_analytics_agent_url()
    endpoint = f"{base_url}/analytics/chat"
    payload = {
        "client_code": client_code,
        "message": message,
        "session_id": session_id or "",
        "user_email": user_email or "",
        "conversation_history": conversation_history or [],
    }

    try:
        resp = requests.post(endpoint, json=payload, headers={"Content-Type": "application/json"}, timeout=120)
        if resp.status_code == 200:
            data = resp.json()
            usage = data.get("usage", {})
            return {
                "reply": data.get("reply", ""),
                "tools_used": data.get("tools_used", []),
                "model_used": data.get("model_used", ""),
                "options": data.get("options", []),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "status": "Success",
                "error_message": None,
            }
        else:
            return {
                "reply": "Sorry, I encountered an issue processing your analytics request.",
                "tools_used": [],
                "model_used": "analytics_agent",
                "options": [],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "status": "Tool Error",
                "error_message": f"Microservice HTTP {resp.status_code}: {resp.text[:300]}",
            }
    except Exception as exc:
        logger.exception(f"[AI Router] Failed to reach analytics_agent at {endpoint}: {exc}")
        return {
            "reply": "I could not reach the analytics engine. Please ensure the analytics_agent service is running.",
            "tools_used": [],
            "model_used": "analytics_agent",
            "options": [],
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "status": "Tool Error",
            "error_message": str(exc),
        }


def route_user_query(
    client_code: str,
    message: str,
    current_page: str | None = None,
    session_id: str | None = None,
    user_email: str | None = None,
    conversation_history: list | None = None,
) -> dict:
    """
    Main entry point for routing user queries.
    Returns:
      {
        "intent": "help_guidance" | "analytics_metrics" | "general_chat",
        "reply": str,
        "tools_used": list[str],
        "model_used": str,
        "options": list[dict],
        "prompt_tokens": int,
        "completion_tokens": int,
        "status": str,
        "error_message": str | None,
        "latency_ms": int,
      }
    """
    start_time = time.time()
    intent = classify_intent(message)

    if intent == INTENT_GENERAL:
        result = {
            "reply": (
                "👋 Hello! I am your **MSuite AI Assistant**.\n\n"
                "I can help you in two ways:\n"
                "1. **How-To Guidance**: Ask how to connect WhatsApp, send bulk messages, or create campaigns.\n"
                "2. **Marketing Analytics**: Ask for your ad spend, delivery rates, open rates, or campaign performance.\n\n"
                "What would you like to explore?"
            ),
            "tools_used": [],
            "model_used": "ai_router/general",
            "options": [
                {"label": "📱 How to connect WhatsApp", "value": "How to connect WhatsApp"},
                {"label": "🚀 How to send bulk WhatsApp message", "value": "How can I send bulk WhatsApp message?"},
                {"label": "📊 Check WhatsApp delivery rate", "value": "What is my WhatsApp delivery rate?"},
                {"label": "💰 Check Meta Ads spend", "value": "What was my Meta Ads spend last week?"},
            ],
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "status": "Success",
            "error_message": None,
        }
    elif intent == INTENT_HELP:
        result = dispatch_help_query(
            message=message,
            current_page=current_page,
            session_id=session_id,
        )
    else:  # INTENT_ANALYTICS
        result = dispatch_analytics_query(
            client_code=client_code,
            message=message,
            session_id=session_id,
            user_email=user_email,
            conversation_history=conversation_history,
        )

    latency_ms = int((time.time() - start_time) * 1000)
    result["intent"] = intent
    result["latency_ms"] = latency_ms
    result.setdefault("prompt_tokens", 0)
    result.setdefault("completion_tokens", 0)
    return result
