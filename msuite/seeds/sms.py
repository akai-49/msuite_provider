"""
SMS product — canonical seed definition.

This module is the *only* place where the SMS product catalog
(features, plans, per-plan feature values) is defined. Both the initial
installer and the reseed patch consume this data, which keeps production
and migration paths in lockstep.
"""

# ---------------------------------------------------------------------------
# Product feature catalog
# ---------------------------------------------------------------------------

_FEATURE_TYPE_BOOLEAN = "Boolean"
_FEATURE_TYPE_NUMERIC = "Numeric"

SMS_PRODUCT_FEATURES = [
    {"feature_key": "sms_accounts",    "feature_label": "SMS Accounts",        "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 1},
    {"feature_key": "sender_ids",      "feature_label": "Sender IDs",          "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 2},
    {"feature_key": "templates",       "feature_label": "SMS Templates",       "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 3},
    {"feature_key": "messaging",       "feature_label": "SMS Messaging",       "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 4},
    {"feature_key": "bulk_messaging",  "feature_label": "Bulk SMS Broadcasts", "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 5},
    {"feature_key": "analytics",       "feature_label": "SMS Analytics",       "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 6},
]

SMS_PRODUCT = {
    "product_name": "SMS",
    "product_code": "SMS",
    "description": "SMS marketing, transactional messaging, and automation platform",
    "features": SMS_PRODUCT_FEATURES,
}

# ---------------------------------------------------------------------------
# Plan catalog
# ---------------------------------------------------------------------------

SMS_PLANS = [
    {"plan_name": "SMS Trial",      "plan_code": "SMS-TRIAL", "tier": "Trial",      "item_code": "SMS-TRIAL-MONTHLY", "rate": 0,    "grants": []},
    {"plan_name": "SMS Basic",      "plan_code": "SMS-BASIC", "tier": "Basic",      "item_code": "SMS-BASIC-MONTHLY", "rate": 799,  "grants": []},
    {"plan_name": "SMS Pro",        "plan_code": "SMS-PRO",   "tier": "Pro",        "item_code": "SMS-PRO-MONTHLY",   "rate": 1599, "grants": []},
    {"plan_name": "SMS Business",   "plan_code": "SMS-BIZ",   "tier": "Business",   "item_code": "SMS-BIZ-MONTHLY",   "rate": 2999, "grants": []},
    {"plan_name": "SMS Enterprise", "plan_code": "SMS-ENT",   "tier": "Enterprise", "item_code": "SMS-ENT-MONTHLY",   "rate": 5999, "grants": []},
]

SMS_PLAN_FEATURES = {
    "SMS-TRIAL": {
        "sms_accounts":   (True,  1,    "total"),
        "sender_ids":     (True,  1,    "total"),
        "templates":      (True,  3,    "total"),
        "messaging":      (True,  100,  "month"),
        "bulk_messaging": (False, None, None),
        "analytics":      (True,  None, None),
    },
    "SMS-BASIC": {
        "sms_accounts":   (True,  2,    "total"),
        "sender_ids":     (True,  2,    "total"),
        "templates":      (True,  10,   "total"),
        "messaging":      (True,  2500, "month"),
        "bulk_messaging": (True,  None, None),
        "analytics":      (True,  None, None),
    },
    "SMS-PRO": {
        "sms_accounts":   (True,  5,    "total"),
        "sender_ids":     (True,  5,    "total"),
        "templates":      (True,  50,   "total"),
        "messaging":      (True,  10000,"month"),
        "bulk_messaging": (True,  None, None),
        "analytics":      (True,  None, None),
    },
    "SMS-BIZ": {
        "sms_accounts":   (True,  10,   "total"),
        "sender_ids":     (True,  10,   "total"),
        "templates":      (True,  100,  "total"),
        "messaging":      (True,  50000,"month"),
        "bulk_messaging": (True,  None, None),
        "analytics":      (True,  None, None),
    },
    "SMS-ENT": {
        "sms_accounts":   (True,  None, "total"),
        "sender_ids":     (True,  None, "total"),
        "templates":      (True,  None, "total"),
        "messaging":      (True,  None, "month"),
        "bulk_messaging": (True,  None, None),
        "analytics":      (True,  None, None),
    },
}

SMS_PLANS_FOR_INSTALL = [
    {
        **plan,
        "features": SMS_PLAN_FEATURES[plan["plan_code"]],
        "grants": plan.get("grants", []),
    }
    for plan in SMS_PLANS
]
