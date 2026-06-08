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
* Inbox-channel keys (`inbox_facebook`, `inbox_instagram`) live on Social
  because they gate FB/IG inbox access, and FB/IG channels are owned by
  this product. The inbox dispatcher (client/inbox/constants.py) maps
  ChannelType.FACEBOOK_DM → "inbox_facebook" and reads the flag from
  plan_data at message-dispatch time.
* `inbox_seats` is a cross-product Numeric key — every product that
  enables an inbox channel declares its own per-tier seat cap. The
  provider's entitlement merge picks `max(WA.inbox_seats, Social.
  inbox_seats)` so a customer with WA Pro + Social Business gets 10
  seats (the higher), not 3 + 10 = 13. Matches the canonical seat model.
* Display order is grouped: platforms (1-9), inbox-channel gates (10-19),
  capabilities (20-29), limits (30+). Adding a new feature? Pick the
  group it belongs to and continue the numbering — keeps the Product
  form readable as the catalog grows.
"""

# ---------------------------------------------------------------------------
# Product feature catalog
# ---------------------------------------------------------------------------

_FEATURE_TYPE_BOOLEAN = "Boolean"
_FEATURE_TYPE_NUMERIC = "Numeric"

SOCIAL_PRODUCT_FEATURES = [
    # Per-platform publish toggles
    {"feature_key": "social_facebook",        "feature_label": "Facebook Pages",         "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 1},
    {"feature_key": "social_instagram",       "feature_label": "Instagram Business",     "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 2},
    {"feature_key": "social_linkedin",        "feature_label": "LinkedIn",               "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 3},
    {"feature_key": "social_twitter",         "feature_label": "X (Twitter)",            "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 4},
    {"feature_key": "social_youtube",         "feature_label": "YouTube",                "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 5},
    {"feature_key": "social_tiktok",          "feature_label": "TikTok",                 "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 6},
    # Inbox-channel gates — keys match `inbox/constants.py::CHANNEL_FEATURE_KEY`
    # on the client. The dispatcher reads these to decide whether to ingest
    # an FB DM / IG DM into the unified Conversation pipeline. Off = silently
    # drop the inbound event with a "feature locked" log line.
    {"feature_key": "inbox_facebook",         "feature_label": "Facebook Inbox",         "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 10},
    {"feature_key": "inbox_instagram",        "feature_label": "Instagram Inbox",        "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 11},
    # Cross-platform capabilities
    {"feature_key": "social_scheduled_posts", "feature_label": "Scheduled Posts",        "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 20},
    {"feature_key": "social_video_posts",     "feature_label": "Video / Reels / Shorts", "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 21},
    {"feature_key": "social_insights",        "feature_label": "Post Analytics",         "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 22},
    {"feature_key": "social_ai_assist",       "feature_label": "AI Content Assistant",   "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 23},
    {"feature_key": "social_bulk_posts",      "feature_label": "CSV Bulk Upload",        "feature_type": _FEATURE_TYPE_BOOLEAN, "display_order": 24},
    # Quantitative limits (value lives on the plan row)
    {"feature_key": "posts_per_month",        "feature_label": "Posts per Month",        "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 30},
    {"feature_key": "accounts_per_platform",  "feature_label": "Accounts per Platform",  "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 31},
    {"feature_key": "media_storage_mb",       "feature_label": "Media Storage (MB)",     "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 32},
    {"feature_key": "inbox_seats",            "feature_label": "Inbox Agent Seats",      "feature_type": _FEATURE_TYPE_NUMERIC, "display_order": 33},
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
# Inbox channels gate at Pro+ — Trial/Basic users can publish to FB/IG but
# the inbound message feed is locked. Same gating shape as WhatsApp where
# the WA product gives `inbox_whatsapp` only at Pro+.
#
# `inbox_seats` uses max-merge across products on the entitlement side, so
# the limits declared here are per-product caps; the customer's effective
# cap is `max(WA seats, Social seats)`.
SOCIAL_PLAN_FEATURES = {
    "SOCIAL-TRIAL": {
        "social_facebook":        (True,  None, None),
        "social_instagram":       (True,  None, None),
        "social_linkedin":        (False, None, None),
        "social_twitter":         (False, None, None),
        "social_youtube":         (False, None, None),
        "social_tiktok":          (False, None, None),
        "inbox_facebook":         (False, None, None),
        "inbox_instagram":        (False, None, None),
        "social_scheduled_posts": (False, None, None),
        "social_video_posts":     (False, None, None),
        "social_insights":        (False, None, None),
        "social_ai_assist":       (False, None, None),
        "social_bulk_posts":      (False, None, None),
        "posts_per_month":        (True,  10,   "per month"),
        "accounts_per_platform":  (True,  1,    "per platform"),
        "media_storage_mb":       (True,  500,  "MB"),
        "inbox_seats":            (False, None, None),
    },
    "SOCIAL-BASIC": {
        "social_facebook":        (True,  None, None),
        "social_instagram":       (True,  None, None),
        "social_linkedin":        (False, None, None),
        "social_twitter":         (False, None, None),
        "social_youtube":         (False, None, None),
        "social_tiktok":          (False, None, None),
        "inbox_facebook":         (False, None, None),
        "inbox_instagram":        (False, None, None),
        "social_scheduled_posts": (True,  None, None),
        "social_video_posts":     (False, None, None),
        "social_insights":        (False, None, None),
        "social_ai_assist":       (False, None, None),
        "social_bulk_posts":      (False, None, None),
        "posts_per_month":        (True,  50,    "per month"),
        "accounts_per_platform":  (True,  2,     "per platform"),
        "media_storage_mb":       (True,  2000,  "MB"),
        "inbox_seats":            (False, None,  None),
    },
    "SOCIAL-PRO": {
        "social_facebook":        (True,  None, None),
        "social_instagram":       (True,  None, None),
        "social_linkedin":        (True,  None, None),
        "social_twitter":         (True,  None, None),
        "social_youtube":         (False, None, None),
        "social_tiktok":          (False, None, None),
        "inbox_facebook":         (True,  None, None),
        "inbox_instagram":        (True,  None, None),
        "social_scheduled_posts": (True,  None, None),
        "social_video_posts":     (True,  None, None),
        "social_insights":        (True,  None, None),
        "social_ai_assist":       (True,  None, None),
        "social_bulk_posts":      (False, None, None),
        "posts_per_month":        (True,  300,    "per month"),
        "accounts_per_platform":  (True,  5,      "per platform"),
        "media_storage_mb":       (True,  10000,  "MB"),
        "inbox_seats":            (True,  3,      "seats"),
    },
    "SOCIAL-BIZ": {
        "social_facebook":        (True,  None, None),
        "social_instagram":       (True,  None, None),
        "social_linkedin":        (True,  None, None),
        "social_twitter":         (True,  None, None),
        "social_youtube":         (True,  None, None),
        "social_tiktok":          (True,  None, None),
        "inbox_facebook":         (True,  None, None),
        "inbox_instagram":        (True,  None, None),
        "social_scheduled_posts": (True,  None, None),
        "social_video_posts":     (True,  None, None),
        "social_insights":        (True,  None, None),
        "social_ai_assist":       (True,  None, None),
        "social_bulk_posts":      (True,  None, None),
        "posts_per_month":        (True,  1500,   "per month"),
        "accounts_per_platform":  (True,  10,     "per platform"),
        "media_storage_mb":       (True,  50000,  "MB"),
        "inbox_seats":            (True,  10,     "seats"),
    },
    "SOCIAL-ENT": {
        "social_facebook":        (True,  None, None),
        "social_instagram":       (True,  None, None),
        "social_linkedin":        (True,  None, None),
        "social_twitter":         (True,  None, None),
        "social_youtube":         (True,  None, None),
        "social_tiktok":          (True,  None, None),
        "inbox_facebook":         (True,  None, None),
        "inbox_instagram":        (True,  None, None),
        "social_scheduled_posts": (True,  None, None),
        "social_video_posts":     (True,  None, None),
        "social_insights":        (True,  None, None),
        "social_ai_assist":       (True,  None, None),
        "social_bulk_posts":      (True,  None, None),
        "posts_per_month":        (True,  None, "per month"),
        "accounts_per_platform":  (True,  None, "per platform"),
        "media_storage_mb":       (True,  None, "MB"),
        "inbox_seats":            (True,  None, "seats"),  # unlimited
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
