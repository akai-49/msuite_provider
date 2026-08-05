"""
AI Calling product — canonical seed definition.

This module is the *only* place where the AI Calling product catalog
(features, plans, per-plan feature values) is defined. Both the initial
installer and the reseed patch consume this data, which keeps production
and migration paths in lockstep.
"""

# ---------------------------------------------------------------------------
# Product feature catalog
# ---------------------------------------------------------------------------

_FEATURE_TYPE_BOOLEAN = "Boolean"
_FEATURE_TYPE_NUMERIC = "Numeric"

AI_CALLING_PRODUCT_FEATURES = [
    {"feature_key": "ai_calling",            "feature_label": "AI Calling",              "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 10},
    {"feature_key": "broadcasts",          "feature_label": "Broadcasts",            "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 20},
    {"feature_key": "broadcast_limit",     "feature_label": "Broadcast Recipients",  "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 30},
    {"feature_key": "monthly_call_minutes",  "feature_label": "Monthly Call Minutes",    "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 40},
    {"feature_key": "inbound_calling",       "feature_label": "Inbound Calling",         "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 50},
    {"feature_key": "outbound_calling",      "feature_label": "Outbound Calling",        "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 60},
    {"feature_key": "knowledge_base",        "feature_label": "Knowledge Base",          "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 70},
    {"feature_key": "kb_articles_limit",     "feature_label": "KB Articles Limit",       "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 80},
    {"feature_key": "ai_agents",             "feature_label": "AI Voice Agents",         "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 90},
    {"feature_key": "call_recording",        "feature_label": "Call Recording",          "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 100},
    {"feature_key": "call_analytics",        "feature_label": "Call Analytics",          "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 110},
    {"feature_key": "call_transfer",         "feature_label": "Live Call Transfer",      "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 120},
]

AI_CALLING_PRODUCT = {
    "product_name": "AI Calling",
    "product_code": "AICALLING",
    "description": "AI-powered voice calling platform — broadcasts, inbound/outbound calling, knowledge base, and AI agents.",
    "features": AI_CALLING_PRODUCT_FEATURES,
}

# ---------------------------------------------------------------------------
# Plan catalog
# ---------------------------------------------------------------------------

AI_CALLING_PLANS = [
    {"plan_name": "AI Calling Trial",      "plan_code": "AICALLING-TRIAL",   "tier": "Trial",      "item_code": "AICALLING-TRIAL-MONTHLY",   "rate": 0},
    {"plan_name": "AI Calling Starter",    "plan_code": "AICALLING-STARTER", "tier": "Basic",      "item_code": "AICALLING-STARTER-MONTHLY", "rate": 1499},
    {"plan_name": "AI Calling Pro",        "plan_code": "AICALLING-PRO",     "tier": "Pro",        "item_code": "AICALLING-PRO-MONTHLY",     "rate": 2999},
    {"plan_name": "AI Calling Business",   "plan_code": "AICALLING-BIZ",     "tier": "Business",   "item_code": "AICALLING-BIZ-MONTHLY",     "rate": 5999},
    {"plan_name": "AI Calling Enterprise", "plan_code": "AICALLING-ENT",     "tier": "Enterprise", "item_code": "AICALLING-ENT-MONTHLY",     "rate": 14999},
]

AI_CALLING_PLAN_FEATURES = {
    "AICALLING-TRIAL": {
        "ai_calling":           (True,  None, None),
        "broadcasts":         (True,  None, None),
        "broadcast_limit":    (True,  100,  "per broadcast"),
        "monthly_call_minutes": (True,  50,   "minutes per month"),
        "inbound_calling":      (False, None, None),
        "outbound_calling":     (True,  None, None),
        "knowledge_base":       (True,  None, None),
        "kb_articles_limit":    (True,  5,    "articles"),
        "ai_agents":            (True,  1,    "agents"),
        "call_recording":       (False, None, None),
        "call_analytics":       (True,  None, None),
        "call_transfer":        (False, None, None),
    },
    "AICALLING-STARTER": {
        "ai_calling":           (True,  None, None),
        "broadcasts":         (True,  None, None),
        "broadcast_limit":    (True,  1000, "per broadcast"),
        "monthly_call_minutes": (True,  500,  "minutes per month"),
        "inbound_calling":      (False, None, None),
        "outbound_calling":     (True,  None, None),
        "knowledge_base":       (True,  None, None),
        "kb_articles_limit":    (True,  20,   "articles"),
        "ai_agents":            (True,  2,    "agents"),
        "call_recording":       (True,  None, None),
        "call_analytics":       (True,  None, None),
        "call_transfer":        (False, None, None),
    },
    "AICALLING-PRO": {
        "ai_calling":           (True,  None,  None),
        "broadcasts":         (True,  None,  None),
        "broadcast_limit":    (True,  5000,  "per broadcast"),
        "monthly_call_minutes": (True,  2000,  "minutes per month"),
        "inbound_calling":      (True,  None,  None),
        "outbound_calling":     (True,  None,  None),
        "knowledge_base":       (True,  None,  None),
        "kb_articles_limit":    (True,  50,    "articles"),
        "ai_agents":            (True,  5,     "agents"),
        "call_recording":       (True,  None,  None),
        "call_analytics":       (True,  None,  None),
        "call_transfer":        (False, None,  None),
    },
    "AICALLING-BIZ": {
        "ai_calling":           (True,  None,  None),
        "broadcasts":         (True,  None,  None),
        "broadcast_limit":    (True,  25000, "per broadcast"),
        "monthly_call_minutes": (True,  10000, "minutes per month"),
        "inbound_calling":      (True,  None,  None),
        "outbound_calling":     (True,  None,  None),
        "knowledge_base":       (True,  None,  None),
        "kb_articles_limit":    (True,  None,  "articles"),
        "ai_agents":            (True,  15,    "agents"),
        "call_recording":       (True,  None,  None),
        "call_analytics":       (True,  None,  None),
        "call_transfer":        (True,  None,  None),
    },
    "AICALLING-ENT": {
        "ai_calling":           (True,  None, None),
        "broadcasts":         (True,  None, None),
        "broadcast_limit":    (True,  None, "per broadcast"),
        "monthly_call_minutes": (True,  None, "minutes per month"),
        "inbound_calling":      (True,  None, None),
        "outbound_calling":     (True,  None, None),
        "knowledge_base":       (True,  None, None),
        "kb_articles_limit":    (True,  None, "articles"),
        "ai_agents":            (True,  None, "agents"),
        "call_recording":       (True,  None, None),
        "call_analytics":       (True,  None, None),
        "call_transfer":        (True,  None, None),
    },
}

AI_CALLING_PLANS_FOR_INSTALL = [
    {
        **plan,
        "features": AI_CALLING_PLAN_FEATURES[plan["plan_code"]],
        "grants": [],
    }
    for plan in AI_CALLING_PLANS
]
