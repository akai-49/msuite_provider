"""
WhatsApp product — canonical seed definition.

This module is the *only* place where the WhatsApp product catalog
(features, plans, per-plan feature values) is defined. Both the initial
installer and the reseed patch consume this data, which keeps production
and migration paths in lockstep.
"""

# ---------------------------------------------------------------------------
# Product feature catalog
# ---------------------------------------------------------------------------

_FEATURE_TYPE_BOOLEAN = "Boolean"
_FEATURE_TYPE_NUMERIC = "Numeric"

WA_PRODUCT_FEATURES = [
    {"feature_key": "messaging",        "feature_label": "Messaging",        "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 1},
    {"feature_key": "templates",        "feature_label": "Templates",        "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 2},
    {"feature_key": "calling",          "feature_label": "Voice Calling",    "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 3},
    {"feature_key": "flows",            "feature_label": "Automation Flows", "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 4},
    {"feature_key": "bulk_messaging",   "feature_label": "Bulk Messaging",   "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 5},
    {"feature_key": "inbox_whatsapp",   "feature_label": "WhatsApp Inbox",   "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 6},
    {"feature_key": "chatbot",          "feature_label": "Chatbot Builder",  "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 7},
    {"feature_key": "analytics",        "feature_label": "Analytics Dashboard", "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 8},
    {"feature_key": "inbox_seats",      "feature_label": "Inbox Agent Seats", "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 9},
]

WA_PRODUCT = {
    "product_name": "WhatsApp",
    "product_code": "WA",
    "description": "WhatsApp Business messaging platform",
    "features": WA_PRODUCT_FEATURES,
}

# ---------------------------------------------------------------------------
# Plan catalog
# ---------------------------------------------------------------------------

WA_PLANS = [
    {"plan_name": "WhatsApp Trial",      "plan_code": "WA-TRIAL", "tier": "Trial",      "item_code": "WA-TRIAL-MONTHLY", "rate": 0,    "grants": []},
    {"plan_name": "WhatsApp Basic",      "plan_code": "WA-BASIC", "tier": "Basic",      "item_code": "WA-BASIC-MONTHLY", "rate": 999,  "grants": []},
    {"plan_name": "WhatsApp Pro",        "plan_code": "WA-PRO",   "tier": "Pro",        "item_code": "WA-PRO-MONTHLY",   "rate": 1999, "grants": []},
    {"plan_name": "WhatsApp Business",   "plan_code": "WA-BIZ",   "tier": "Business",   "item_code": "WA-BIZ-MONTHLY",   "rate": 2999, "grants": [{"granted_plan_code": "SOCIAL-BASIC", "grant_type": "Complimentary", "expires_after_days": 0}]},
    {"plan_name": "WhatsApp Enterprise", "plan_code": "WA-ENT",   "tier": "Enterprise", "item_code": "WA-ENT-MONTHLY",   "rate": 5999, "grants": [{"granted_plan_code": "SOCIAL-PRO", "grant_type": "Complimentary", "expires_after_days": 0}]},
]

WA_PLAN_FEATURES = {
    "WA-TRIAL": {
        "messaging":      (True,  100,  "per month"),
        "templates":      (True,  3,    "total"),
        "calling":        (False, None, None),
        "flows":          (False, None, None),
        "bulk_messaging": (False, None, None),
        "inbox_whatsapp": (False, None, None),
        "chatbot":        (False, None, None),
        "analytics":      (True,  None, None),
        "inbox_seats":    (False, None, None),
    },
    "WA-BASIC": {
        "messaging":      (True,  1000, "per month"),
        "templates":      (True,  5,    "total"),
        "calling":        (False, None, None),
        "flows":          (False, None, None),
        "bulk_messaging": (False, None, None),
        "inbox_whatsapp": (False, None, None),
        "chatbot":        (False, None, None),
        "analytics":      (True,  None, None),
        "inbox_seats":    (False, None, None),
    },
    "WA-PRO": {
        "messaging":      (True,  5000, "per month"),
        "templates":      (True,  25,   "total"),
        "calling":        (True,  None, None),
        "flows":          (True,  3,    "total"),
        "bulk_messaging": (True,  5000, "per campaign"),
        "inbox_whatsapp": (True,  None, None),
        "chatbot":        (False, None, None),
        "analytics":      (True,  None, None),
        "inbox_seats":    (True,  3,    "seats"),
    },
    "WA-BIZ": {
        "messaging":      (True,  None,  "per month"),
        "templates":      (True,  100,   "total"),
        "calling":        (True,  None,  None),
        "flows":          (True,  10,    "total"),
        "bulk_messaging": (True,  50000, "per campaign"),
        "inbox_whatsapp": (True,  None,  None),
        "chatbot":        (True,  None,  None),
        "analytics":      (True,  None,  None),
        "inbox_seats":    (True,  10,    "seats"),
    },
    "WA-ENT": {
        "messaging":      (True,  None, "per month"),
        "templates":      (True,  None, "total"),
        "calling":        (True,  None, None),
        "flows":          (True,  None, "total"),
        "bulk_messaging": (True,  None, "per campaign"),
        "inbox_whatsapp": (True,  None, None),
        "chatbot":        (True,  None, None),
        "analytics":      (True,  None, None),
        "inbox_seats":    (True,  None, "seats"),
    },
}

WA_PLANS_FOR_INSTALL = [
    {
        **plan,
        "features": WA_PLAN_FEATURES[plan["plan_code"]],
        "grants": plan.get("grants", []),
    }
    for plan in WA_PLANS
]
