"""
Ads product — canonical seed definition.

This module is the *only* place where the Ads product catalog
(features, plans, per-plan feature values) is defined. Both the initial
installer and the reseed patch consume this data, which keeps production
and migration paths in lockstep.
"""

# ---------------------------------------------------------------------------
# Product feature catalog
# ---------------------------------------------------------------------------

_FEATURE_TYPE_BOOLEAN = "Boolean"
_FEATURE_TYPE_NUMERIC = "Numeric"

ADS_PRODUCT_FEATURES = [
    {"feature_key": "ads_meta",                 "feature_label": "Meta Ads",                        "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 10},
    {"feature_key": "ads_google",               "feature_label": "Google Ads",                      "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 20},
    {"feature_key": "ads_lead_gen",             "feature_label": "Meta Lead Gen Forms",             "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 30},
    {"feature_key": "ads_advanced_targeting",   "feature_label": "Custom / Lookalike Audiences",    "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 40},
    {"feature_key": "ads_insights",             "feature_label": "Async insights sync",             "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 50},
    {"feature_key": "ads_bulk_duplicate",       "feature_label": "Bulk duplicate campaigns",        "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 60},
    {"feature_key": "ads_creative_library",     "feature_label": "Creative Library page",           "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 70},
    {"feature_key": "active_ad_campaigns",      "feature_label": "Max concurrent Ad Campaigns",     "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 80},
    {"feature_key": "ad_accounts_per_platform", "feature_label": "Ad Accounts per platform",        "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 90},
    {"feature_key": "ads_monthly_spend",        "feature_label": "Monthly spend ceiling",           "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 100},
]

ADS_PRODUCT = {
    "product_name": "Ads",
    "product_code": "ADS",
    "description": "Digital advertising management platform",
    "features": ADS_PRODUCT_FEATURES,
}

# ---------------------------------------------------------------------------
# Plan catalog
# ---------------------------------------------------------------------------

ADS_PLANS = [
    {"plan_name": "Ads Trial",      "plan_code": "ADS-TRIAL", "tier": "Trial",      "item_code": "ADS-TRIAL-MONTHLY", "rate": 0},
    {"plan_name": "Ads Basic",      "plan_code": "ADS-BASIC", "tier": "Basic",      "item_code": "ADS-BASIC-MONTHLY", "rate": 1499},
    {"plan_name": "Ads Pro",        "plan_code": "ADS-PRO",   "tier": "Pro",        "item_code": "ADS-PRO-MONTHLY",   "rate": 2999},
    {"plan_name": "Ads Business",   "plan_code": "ADS-BIZ",   "tier": "Business",   "item_code": "ADS-BIZ-MONTHLY",   "rate": 4999},
    {"plan_name": "Ads Enterprise", "plan_code": "ADS-ENT",   "tier": "Enterprise", "item_code": "ADS-ENT-MONTHLY",   "rate": 9999},
]

ADS_PLAN_FEATURES = {
    "ADS-TRIAL": {
        "ads_meta":                 (True,  None, None),
        "ads_google":               (False, None, None),
        "ads_lead_gen":             (True,  None, None),
        "ads_advanced_targeting":   (False, None, None),
        "ads_insights":             (True,  None, None),
        "ads_bulk_duplicate":       (False, None, None),
        "ads_creative_library":     (False, None, None),
        "active_ad_campaigns":      (True,  2,    "campaigns"),
        "ad_accounts_per_platform": (True,  1,    "per platform"),
        "ads_monthly_spend":        (True,  100,  "per month"),
    },
    "ADS-BASIC": {
        "ads_meta":                 (True,  None, None),
        "ads_google":               (True,  None, None),
        "ads_lead_gen":             (True,  None, None),
        "ads_advanced_targeting":   (False, None, None),
        "ads_insights":             (True,  None, None),
        "ads_bulk_duplicate":       (False, None, None),
        "ads_creative_library":     (True,  None, None),
        "active_ad_campaigns":      (True,  5,    "campaigns"),
        "ad_accounts_per_platform": (True,  2,    "per platform"),
        "ads_monthly_spend":        (True,  500,  "per month"),
    },
    "ADS-PRO": {
        "ads_meta":                 (True,  None, None),
        "ads_google":               (True,  None, None),
        "ads_lead_gen":             (True,  None, None),
        "ads_advanced_targeting":   (True,  None, None),
        "ads_insights":             (True,  None, None),
        "ads_bulk_duplicate":       (False, None, None),
        "ads_creative_library":     (True,  None, None),
        "active_ad_campaigns":      (True,  15,   "campaigns"),
        "ad_accounts_per_platform": (True,  5,    "per platform"),
        "ads_monthly_spend":        (True,  2000, "per month"),
    },
    "ADS-BIZ": {
        "ads_meta":                 (True,  None, None),
        "ads_google":               (True,  None, None),
        "ads_lead_gen":             (True,  None, None),
        "ads_advanced_targeting":   (True,  None, None),
        "ads_insights":             (True,  None, None),
        "ads_bulk_duplicate":       (True,  None, None),
        "ads_creative_library":     (True,  None, None),
        "active_ad_campaigns":      (True,  50,   "campaigns"),
        "ad_accounts_per_platform": (True,  10,   "per platform"),
        "ads_monthly_spend":        (True,  10000, "per month"),
    },
    "ADS-ENT": {
        "ads_meta":                 (True,  None, None),
        "ads_google":               (True,  None, None),
        "ads_lead_gen":             (True,  None, None),
        "ads_advanced_targeting":   (True,  None, None),
        "ads_insights":             (True,  None, None),
        "ads_bulk_duplicate":       (True,  None, None),
        "ads_creative_library":     (True,  None, None),
        "active_ad_campaigns":      (True,  None, "campaigns"),
        "ad_accounts_per_platform": (True,  None, "per platform"),
        "ads_monthly_spend":        (True,  None, "per month"),
    },
}

ADS_PLANS_FOR_INSTALL = [
    {
        **plan,
        "features": ADS_PLAN_FEATURES[plan["plan_code"]],
        "grants": [],
    }
    for plan in ADS_PLANS
]

