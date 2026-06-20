"""
Email product — canonical seed definition.

This module is the *only* place where the Email product catalog
(features, plans, per-plan feature values) is defined. Both the initial
installer and the reseed patch consume this data, which keeps production
and migration paths in lockstep.
"""

# ---------------------------------------------------------------------------
# Product feature catalog
# ---------------------------------------------------------------------------

_FEATURE_TYPE_BOOLEAN = "Boolean"
_FEATURE_TYPE_NUMERIC = "Numeric"

EMAIL_PRODUCT_FEATURES = [
    {"feature_key": "campaigns",        "feature_label": "Email Campaigns",      "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 1},
    {"feature_key": "contacts",         "feature_label": "Contact Limit",        "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 2},
    {"feature_key": "automations",      "feature_label": "Marketing Automations", "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 3},
    {"feature_key": "templates",        "feature_label": "Email Templates",      "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 4},
    {"feature_key": "smtp_accounts",    "feature_label": "SMTP Accounts",        "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 5},
    {"feature_key": "analytics",        "feature_label": "Analytics Dashboard",  "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 6},
    {"feature_key": "segmentation",     "feature_label": "List Segmentation",    "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 7},
    {"feature_key": "ab_testing",       "feature_label": "A/B Testing",          "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 8},
]

EMAIL_PRODUCT = {
    "product_name": "Email",
    "product_code": "EMAIL",
    "description": "Email marketing and automation platform",
    "features": EMAIL_PRODUCT_FEATURES,
}

# ---------------------------------------------------------------------------
# Plan catalog
# ---------------------------------------------------------------------------

EMAIL_PLANS = [
    {"plan_name": "Email Trial",      "plan_code": "EMAIL-TRIAL", "tier": "Trial",      "item_code": "EMAIL-TRIAL-MONTHLY", "rate": 0},
    {"plan_name": "Email Basic",      "plan_code": "EMAIL-BASIC", "tier": "Basic",      "item_code": "EMAIL-BASIC-MONTHLY", "rate": 799},
    {"plan_name": "Email Pro",        "plan_code": "EMAIL-PRO",   "tier": "Pro",        "item_code": "EMAIL-PRO-MONTHLY",   "rate": 1599},
    {"plan_name": "Email Business",   "plan_code": "EMAIL-BIZ",   "tier": "Business",   "item_code": "EMAIL-BIZ-MONTHLY",   "rate": 2499},
    {"plan_name": "Email Enterprise", "plan_code": "EMAIL-ENT",   "tier": "Enterprise", "item_code": "EMAIL-ENT-MONTHLY",   "rate": 4999},
]

EMAIL_PLAN_FEATURES = {
    "EMAIL-TRIAL": {
        "campaigns":     (True,  None, None),
        "contacts":      (True,  500,  "total"),
        "automations":   (False, None, None),
        "templates":     (True,  3,    "total"),
        "smtp_accounts": (True,  1,    "total"),
        "analytics":     (True,  None, None),
        "segmentation":  (False, None, None),
        "ab_testing":    (False, None, None),
    },
    "EMAIL-BASIC": {
        "campaigns":     (True,  None, None),
        "contacts":      (True,  2500, "total"),
        "automations":   (False, None, None),
        "templates":     (True,  10,   "total"),
        "smtp_accounts": (True,  2,    "total"),
        "analytics":     (True,  None, None),
        "segmentation":  (False, None, None),
        "ab_testing":    (False, None, None),
    },
    "EMAIL-PRO": {
        "campaigns":     (True,  None, None),
        "contacts":      (True,  10000, "total"),
        "automations":   (True,  None, None),
        "templates":     (True,  50,   "total"),
        "smtp_accounts": (True,  5,    "total"),
        "analytics":     (True,  None, None),
        "segmentation":  (True,  None, None),
        "ab_testing":    (False, None, None),
    },
    "EMAIL-BIZ": {
        "campaigns":     (True,  None, None),
        "contacts":      (True,  50000, "total"),
        "automations":   (True,  None, None),
        "templates":     (True,  100,  "total"),
        "smtp_accounts": (True,  10,   "total"),
        "analytics":     (True,  None, None),
        "segmentation":  (True,  None, None),
        "ab_testing":    (True,  None, None),
    },
    "EMAIL-ENT": {
        "campaigns":     (True,  None, None),
        "contacts":      (True,  None, "total"),
        "automations":   (True,  None, None),
        "templates":     (True,  None, "total"),
        "smtp_accounts": (True,  None, "total"),
        "analytics":     (True,  None, None),
        "segmentation":  (True,  None, None),
        "ab_testing":    (True,  None, None),
    },
}

EMAIL_PLANS_FOR_INSTALL = [
    {
        **plan,
        "features": EMAIL_PLAN_FEATURES[plan["plan_code"]],
        "grants": [],
    }
    for plan in EMAIL_PLANS
]

