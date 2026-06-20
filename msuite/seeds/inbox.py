"""
Inbox product — canonical seed definition.

This module is the *only* place where the Inbox product catalog
(features, plans, per-plan feature values) is defined. Both the initial
installer and the reseed patch consume this data, which keeps production
and migration paths in lockstep.
"""

# ---------------------------------------------------------------------------
# Product feature catalog
# ---------------------------------------------------------------------------

_FEATURE_TYPE_BOOLEAN = "Boolean"
_FEATURE_TYPE_NUMERIC = "Numeric"

INBOX_PRODUCT_FEATURES = [
    {"feature_key": "inbox",            "feature_label": "Inbox",                   "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 10},
    {"feature_key": "inbox_whatsapp",   "feature_label": "WhatsApp Channel",         "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 20},
    {"feature_key": "inbox_instagram",  "feature_label": "Instagram DM Channel",     "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 30},
    {"feature_key": "inbox_facebook",   "feature_label": "Facebook DM Channel",      "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 40},
    {"feature_key": "inbox_email",      "feature_label": "Email Channel",            "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 50},
    {"feature_key": "inbox_website",    "feature_label": "Website Widget Channel",   "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 60},
    {"feature_key": "inbox_ai_assist",  "feature_label": "AI Reply Assist",          "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 70},
]

INBOX_PRODUCT = {
    "product_name": "Inbox",
    "product_code": "INBOX",
    "description": "Unified omnichannel inbox — reply, assign, and resolve customer conversations from WhatsApp, Instagram, and Facebook in one place.",
    "features": INBOX_PRODUCT_FEATURES,
}

# ---------------------------------------------------------------------------
# Plan catalog
# ---------------------------------------------------------------------------

INBOX_PLANS = [
    {"plan_name": "Inbox Trial",      "plan_code": "INBOX-TRIAL",   "tier": "Trial",      "item_code": "INBOX-TRIAL-MONTHLY",   "rate": 0},
    {"plan_name": "Inbox Starter",    "plan_code": "INBOX-STARTER", "tier": "Basic",      "item_code": "INBOX-STARTER-MONTHLY", "rate": 999},
    {"plan_name": "Inbox Pro",        "plan_code": "INBOX-PRO",     "tier": "Pro",        "item_code": "INBOX-PRO-MONTHLY",     "rate": 2999},
    {"plan_name": "Inbox Business",   "plan_code": "INBOX-BIZ",     "tier": "Business",   "item_code": "INBOX-BIZ-MONTHLY",     "rate": 6999},
    {"plan_name": "Inbox Enterprise", "plan_code": "INBOX-ENT",     "tier": "Enterprise", "item_code": "INBOX-ENT-MONTHLY",   "rate": 12999},
]

INBOX_PLAN_FEATURES = {
    "INBOX-TRIAL": {
        "inbox":            (True,  None, None),
        "inbox_whatsapp":   (True,  None, None),
        "inbox_instagram":  (True,  None, None),
        "inbox_facebook":   (True,  None, None),
        "inbox_email":      (False, None, None),
        "inbox_website":    (False, None, None),
        "inbox_ai_assist":  (True,  None, None),
    },
    "INBOX-STARTER": {
        "inbox":            (True,  None, None),
        "inbox_whatsapp":   (True,  None, None),
        "inbox_instagram":  (False, None, None),
        "inbox_facebook":   (False, None, None),
        "inbox_email":      (False, None, None),
        "inbox_website":    (False, None, None),
        "inbox_ai_assist":  (False, None, None),
    },
    "INBOX-PRO": {
        "inbox":            (True,  None, None),
        "inbox_whatsapp":   (True,  None, None),
        "inbox_instagram":  (True,  None, None),
        "inbox_facebook":   (True,  None, None),
        "inbox_email":      (False, None, None),
        "inbox_website":    (False, None, None),
        "inbox_ai_assist":  (False, None, None),
    },
    "INBOX-BIZ": {
        "inbox":            (True,  None, None),
        "inbox_whatsapp":   (True,  None, None),
        "inbox_instagram":  (True,  None, None),
        "inbox_facebook":   (True,  None, None),
        "inbox_email":      (False, None, None),
        "inbox_website":    (False, None, None),
        "inbox_ai_assist":  (True,  None, None),
    },
    "INBOX-ENT": {
        "inbox":            (True,  None, None),
        "inbox_whatsapp":   (True,  None, None),
        "inbox_instagram":  (True,  None, None),
        "inbox_facebook":   (True,  None, None),
        "inbox_email":      (True,  None, None),
        "inbox_website":    (True,  None, None),
        "inbox_ai_assist":  (True,  None, None),
    },
}

INBOX_PLANS_FOR_INSTALL = [
    {
        **plan,
        "features": INBOX_PLAN_FEATURES[plan["plan_code"]],
        "grants": [],
    }
    for plan in INBOX_PLANS
]

