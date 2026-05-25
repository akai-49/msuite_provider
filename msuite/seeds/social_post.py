"""
Social Post product — canonical seed definition.

This module is the *only* place where the Social Post product catalog
(features, plans, per-plan feature values) is defined. Both the initial
installer and the reseed patch consume this data, which keeps production
and migration paths in lockstep.

Design notes
------------
* Per-platform features are Boolean toggles. A plan that includes a
  platform sets its feature `is_enabled = 1`; plans that exclude it simply
  omit the row. This makes "does this plan allow LinkedIn?" a single
  property lookup on the Client.
* Numeric limits (`posts_per_month`, `accounts_per_platform`,
  `media_storage_mb`) are Numeric features whose `limit_value` carries the
  quota. `limit_value = None` means unlimited, matching how the Client's
  plan_enforcer already treats WhatsApp limits.
* Display order is grouped: platforms (1-9), capabilities (10-19),
  limits (20+) — keeps the Product form readable as features grow.
"""

# ---------------------------------------------------------------------------
# Product feature catalog
# ---------------------------------------------------------------------------

_FEATURE_TYPE_BOOLEAN = "Boolean"
_FEATURE_TYPE_NUMERIC = "Numeric"

SOCIAL_PRODUCT_FEATURES = [
    # Per-platform publish toggles
    {"feature_key": "social_facebook",  "feature_label": "Facebook Pages",       "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 1},
    {"feature_key": "social_instagram", "feature_label": "Instagram Business",   "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 2},
    {"feature_key": "social_linkedin",  "feature_label": "LinkedIn",             "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 3},
    {"feature_key": "social_twitter",   "feature_label": "X (Twitter)",          "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 4},
    {"feature_key": "social_youtube",   "feature_label": "YouTube",              "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 5},
    {"feature_key": "social_tiktok",    "feature_label": "TikTok",               "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 6},
    # Cross-platform capabilities
    {"feature_key": "social_scheduled_posts",    "feature_label": "Scheduled Posts",        "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 10},
    {"feature_key": "social_carousel_posts",     "feature_label": "Carousel / Multi-image", "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 11},
    {"feature_key": "social_video_posts",        "feature_label": "Video / Reels / Shorts", "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 12},
    {"feature_key": "social_insights",           "feature_label": "Post Analytics",         "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 13},
    {"feature_key": "social_comment_moderation", "feature_label": "Comment Moderation",     "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 14},
    {"feature_key": "social_approval_workflow",  "feature_label": "Approval Workflow",      "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 15},
    {"feature_key": "social_ai_assist",          "feature_label": "AI Content Assistant",   "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 16},
    {"feature_key": "social_bulk_posts",         "feature_label": "CSV Bulk Upload",        "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 17},
    # Quantitative limits (value lives on the plan row)
    {"feature_key": "posts_per_month",       "feature_label": "Posts per Month",        "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 20},
    {"feature_key": "accounts_per_platform", "feature_label": "Accounts per Platform",  "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 21},
    {"feature_key": "media_storage_mb",      "feature_label": "Media Storage (MB)",     "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 22},
]


SOCIAL_PRODUCT = {
    "product_name": "Social Post",
    "product_code": "SOCIAL",
    "description": "Multi-platform social media management and scheduling",
    "features": SOCIAL_PRODUCT_FEATURES,
}


# ---------------------------------------------------------------------------
# Plan catalog
# ---------------------------------------------------------------------------

SOCIAL_PLANS = [
    {"plan_name": "Social Post Trial",      "plan_code": "SOCIAL-TRIAL", "tier": "Trial",      "item_code": "SOCIAL-TRIAL-MONTHLY", "rate": 0},
    {"plan_name": "Social Post Basic",      "plan_code": "SOCIAL-BASIC", "tier": "Basic",      "item_code": "SOCIAL-BASIC-MONTHLY", "rate": 599},
    {"plan_name": "Social Post Pro",        "plan_code": "SOCIAL-PRO",   "tier": "Pro",        "item_code": "SOCIAL-PRO-MONTHLY",   "rate": 1199},
    {"plan_name": "Social Post Business",   "plan_code": "SOCIAL-BIZ",   "tier": "Business",   "item_code": "SOCIAL-BIZ-MONTHLY",   "rate": 1999},
    {"plan_name": "Social Post Enterprise", "plan_code": "SOCIAL-ENT",   "tier": "Enterprise", "item_code": "SOCIAL-ENT-MONTHLY",   "rate": 3999},
]


# Per-plan feature map. Tuple = (is_enabled, limit_value, limit_label).
#   is_enabled   — Boolean flag written to MSuite Plan Feature.is_enabled
#   limit_value  — numeric quota; None means "no cap" for Numeric features
#   limit_label  — human suffix ("per month"), optional
#
# Omitted features resolve to disabled on the Client; we still emit explicit
# False rows so the Plan form shows all features and admins can tweak them.
SOCIAL_PLAN_FEATURES = {
    "SOCIAL-TRIAL": {
        "social_facebook":           (True,  None, None),
        "social_instagram":          (True,  None, None),
        "social_linkedin":           (False, None, None),
        "social_twitter":            (False, None, None),
        "social_youtube":            (False, None, None),
        "social_tiktok":             (False, None, None),
        "social_scheduled_posts":    (False, None, None),
        "social_carousel_posts":     (True,  None, None),
        "social_video_posts":        (False, None, None),
        "social_insights":           (False, None, None),
        "social_comment_moderation": (False, None, None),
        "social_approval_workflow":  (False, None, None),
        "social_ai_assist":          (False, None, None),
        "social_bulk_posts":         (False, None, None),
        "posts_per_month":           (True,  10,   "per month"),
        "accounts_per_platform":     (True,  1,    "per platform"),
        "media_storage_mb":          (True,  500,  "MB"),
    },
    "SOCIAL-BASIC": {
        "social_facebook":           (True,  None, None),
        "social_instagram":          (True,  None, None),
        "social_linkedin":           (False, None, None),
        "social_twitter":            (False, None, None),
        "social_youtube":            (False, None, None),
        "social_tiktok":             (False, None, None),
        "social_scheduled_posts":    (True,  None, None),
        "social_carousel_posts":     (True,  None, None),
        "social_video_posts":        (False, None, None),
        "social_insights":           (False, None, None),
        "social_comment_moderation": (False, None, None),
        "social_approval_workflow":  (False, None, None),
        "social_ai_assist":          (False, None, None),
        "social_bulk_posts":         (False, None, None),
        "posts_per_month":           (True,  50,    "per month"),
        "accounts_per_platform":     (True,  2,     "per platform"),
        "media_storage_mb":          (True,  2000,  "MB"),
    },
    "SOCIAL-PRO": {
        "social_facebook":           (True,  None, None),
        "social_instagram":          (True,  None, None),
        "social_linkedin":           (True,  None, None),
        "social_twitter":            (True,  None, None),
        "social_youtube":            (False, None, None),
        "social_tiktok":             (False, None, None),
        "social_scheduled_posts":    (True,  None, None),
        "social_carousel_posts":     (True,  None, None),
        "social_video_posts":        (True,  None, None),
        "social_insights":           (True,  None, None),
        "social_comment_moderation": (True,  None, None),
        "social_approval_workflow":  (False, None, None),
        "social_ai_assist":          (True,  None, None),
        "social_bulk_posts":         (False, None, None),
        "posts_per_month":           (True,  300,    "per month"),
        "accounts_per_platform":     (True,  5,      "per platform"),
        "media_storage_mb":          (True,  10000,  "MB"),
    },
    "SOCIAL-BIZ": {
        "social_facebook":           (True,  None, None),
        "social_instagram":          (True,  None, None),
        "social_linkedin":           (True,  None, None),
        "social_twitter":            (True,  None, None),
        "social_youtube":            (True,  None, None),
        "social_tiktok":             (True,  None, None),
        "social_scheduled_posts":    (True,  None, None),
        "social_carousel_posts":     (True,  None, None),
        "social_video_posts":        (True,  None, None),
        "social_insights":           (True,  None, None),
        "social_comment_moderation": (True,  None, None),
        "social_approval_workflow":  (True,  None, None),
        "social_ai_assist":          (True,  None, None),
        "social_bulk_posts":         (True,  None, None),
        "posts_per_month":           (True,  1500,   "per month"),
        "accounts_per_platform":     (True,  10,     "per platform"),
        "media_storage_mb":          (True,  50000,  "MB"),
    },
    "SOCIAL-ENT": {
        "social_facebook":           (True,  None, None),
        "social_instagram":          (True,  None, None),
        "social_linkedin":           (True,  None, None),
        "social_twitter":            (True,  None, None),
        "social_youtube":            (True,  None, None),
        "social_tiktok":             (True,  None, None),
        "social_scheduled_posts":    (True,  None, None),
        "social_carousel_posts":     (True,  None, None),
        "social_video_posts":        (True,  None, None),
        "social_insights":           (True,  None, None),
        "social_comment_moderation": (True,  None, None),
        "social_approval_workflow":  (True,  None, None),
        "social_ai_assist":          (True,  None, None),
        "social_bulk_posts":         (True,  None, None),
        "posts_per_month":           (True,  None, "per month"),
        "accounts_per_platform":     (True,  None, "per platform"),
        "media_storage_mb":          (True,  None, "MB"),
    },
}


# Install-time format: merges plan metadata with its features so install.py
# can treat WA/SOCIAL/EMAIL/ADS plans uniformly (one list, one loop).
SOCIAL_PLANS_FOR_INSTALL = [
    {
        **plan,
        "features": SOCIAL_PLAN_FEATURES[plan["plan_code"]],
        "grants": [],  # SOCIAL plans don't grant others; WA plans grant these.
    }
    for plan in SOCIAL_PLANS
]
