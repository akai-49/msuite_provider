"""
Runs after: bench install-app msuite

Responsibilities:
1. Create custom fields on ERPNext Item doctype
2. Create custom fields on ERPNext Customer doctype
3. Create MSuite Customer Group Membership child doctype link on Customer
4. Create MSuite parent Customer Group
5. Create Item Groups: WhatsApp, Email, Social Post, Ads, Bundles
6. Create MSuite Manager role
7. Seed all Products, Plans, Bundles
8. All operations check existence before creating (fully idempotent)

Per-product seed definitions live under msuite/seeds/ so both fresh
installs and migration patches consume the same configuration.
"""

import frappe

from msuite.seeds.social_post import SOCIAL_PRODUCT, SOCIAL_PLANS_FOR_INSTALL


def after_install():
    _create_msuite_manager_role()
    _ensure_erpnext_defaults()
    _create_custom_fields_on_item()
    _create_custom_fields_on_customer()
    _create_msuite_parent_customer_group()

    try:
        _create_item_groups()
    except Exception as e:
        print(f"Warning: Could not create Item Groups: {e}")

    try:
        _seed_products()
    except Exception as e:
        print(f"Warning: Could not seed products: {e}")

    try:
        _seed_plans()
    except Exception as e:
        print(f"Warning: Could not seed plans: {e}")

    try:
        _seed_bundles()
    except Exception as e:
        print(f"Warning: Could not seed bundles: {e}")

    frappe.db.commit()


def _ensure_erpnext_defaults():
    """Ensure ERPNext defaults exist for seeding."""
    # Ensure UOM exists
    if not frappe.db.exists("UOM", "Nos"):
        uom = frappe.new_doc("UOM")
        uom.uom_name = "Nos"
        uom.insert(ignore_permissions=True, ignore_if_duplicate=True)

    # Ensure territory exists
    if not frappe.db.exists("Territory", "All Territories"):
        doc = frappe.new_doc("Territory")
        doc.territory_name = "All Territories"
        doc.is_group = 1
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True, ignore_if_duplicate=True)


def _create_msuite_manager_role():
    """Creates 'MSuite Manager' role if not exists."""
    if not frappe.db.exists("Role", "MSuite Manager"):
        doc = frappe.new_doc("Role")
        doc.role_name = "MSuite Manager"
        doc.desk_access = 1
        doc.insert(ignore_permissions=True)


def _create_item_groups():
    """Creates Item Groups: WhatsApp, Email, Social Post, Ads, Bundles."""
    # Ensure root Item Group exists
    if not frappe.db.exists("Item Group", "All Item Groups"):
        doc = frappe.new_doc("Item Group")
        doc.item_group_name = "All Item Groups"
        doc.is_group = 1
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True, ignore_if_duplicate=True)

    groups = ["WhatsApp", "Email", "Social Post", "Ads", "Bundles"]
    for group_name in groups:
        if not frappe.db.exists("Item Group", group_name):
            doc = frappe.new_doc("Item Group")
            doc.item_group_name = group_name
            doc.parent_item_group = "All Item Groups"
            doc.insert(ignore_permissions=True)


def _create_custom_fields_on_item():
    """Adds is_msuite_bundle and msuite_bundle custom fields to Item."""
    if not frappe.db.exists("Custom Field", {"dt": "Item", "fieldname": "msuite_section"}):
        frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "Item",
            "fieldname": "msuite_section",
            "fieldtype": "Section Break",
            "label": "MSuite",
            "insert_after": "description",
        }).insert(ignore_permissions=True)

    if not frappe.db.exists("Custom Field", {"dt": "Item", "fieldname": "is_msuite_bundle"}):
        frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "Item",
            "fieldname": "is_msuite_bundle",
            "fieldtype": "Check",
            "label": "Is MSuite Bundle",
            "default": "0",
            "insert_after": "msuite_section",
        }).insert(ignore_permissions=True)

    if not frappe.db.exists("Custom Field", {"dt": "Item", "fieldname": "msuite_bundle"}):
        frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "Item",
            "fieldname": "msuite_bundle",
            "fieldtype": "Link",
            "label": "MSuite Bundle",
            "options": "MSuite Bundle",
            "depends_on": "eval:doc.is_msuite_bundle == 1",
            "insert_after": "is_msuite_bundle",
        }).insert(ignore_permissions=True)


def _create_custom_fields_on_customer():
    """Adds msuite_group_memberships child table to Customer."""
    if not frappe.db.exists("Custom Field", {"dt": "Customer", "fieldname": "msuite_memberships_section"}):
        frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "Customer",
            "fieldname": "msuite_memberships_section",
            "fieldtype": "Section Break",
            "label": "MSuite Memberships",
            "insert_after": "customer_group",
            "hidden": 1,
        }).insert(ignore_permissions=True)

    if not frappe.db.exists("Custom Field", {"dt": "Customer", "fieldname": "msuite_group_memberships"}):
        frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "Customer",
            "fieldname": "msuite_group_memberships",
            "fieldtype": "Table",
            "label": "MSuite Group Memberships",
            "options": "MSuite Customer Group Membership",
            "hidden": 1,
            "insert_after": "msuite_memberships_section",
        }).insert(ignore_permissions=True)


def _create_msuite_parent_customer_group():
    """Creates 'MSuite' Customer Group with parent 'All Customer Groups'."""
    # Ensure root Customer Group exists
    if not frappe.db.exists("Customer Group", "All Customer Groups"):
        doc = frappe.new_doc("Customer Group")
        doc.customer_group_name = "All Customer Groups"
        doc.is_group = 1
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True, ignore_if_duplicate=True)

    if not frappe.db.exists("Customer Group", "MSuite"):
        doc = frappe.new_doc("Customer Group")
        doc.customer_group_name = "MSuite"
        doc.parent_customer_group = "All Customer Groups"
        doc.insert(ignore_permissions=True)


def _seed_products():
    """Seeds 4 MSuite Products with their complete feature definitions."""
    products = {
        "WA": {
            "product_name": "WhatsApp",
            "product_code": "WA",
            "description": "WhatsApp Business messaging platform",
            "features": [
                # Volume + capability gates. Keys here are the canonical
                # feature_keys the client's plan_enforcer + inbox/constants
                # already check against — don't rename without updating both.
                {"feature_key": "messaging",        "feature_label": "Messaging",        "feature_type": "Numeric", "display_order": 1},
                {"feature_key": "templates",        "feature_label": "Templates",        "feature_type": "Numeric", "display_order": 2},
                {"feature_key": "calling",          "feature_label": "Voice Calling",    "feature_type": "Boolean", "display_order": 3},
                {"feature_key": "flows",            "feature_label": "Automation Flows", "feature_type": "Numeric", "display_order": 4},
                {"feature_key": "bulk_messaging",   "feature_label": "Bulk Messaging",   "feature_type": "Numeric", "display_order": 5},
                {"feature_key": "inbox_whatsapp",   "feature_label": "WhatsApp Inbox",   "feature_type": "Boolean", "display_order": 6},
                {"feature_key": "chatbot",          "feature_label": "Chatbot Builder",  "feature_type": "Boolean", "display_order": 7},
                {"feature_key": "analytics",        "feature_label": "Analytics Dashboard", "feature_type": "Boolean", "display_order": 8},
                # Cross-product seat key — `inbox_seats` exists on every
                # product that enables any inbox channel. Entitlements
                # merge with max-wins, so a customer with WA Pro +
                # Social Business gets the higher of the two seat caps.
                {"feature_key": "inbox_seats",      "feature_label": "Inbox Agent Seats", "feature_type": "Numeric", "display_order": 9},
            ],
        },
        "EMAIL": {
            "product_name": "Email",
            "product_code": "EMAIL",
            "description": "Email marketing and automation platform",
            "features": [
                {"feature_key": "campaigns", "feature_label": "Email Campaigns", "feature_type": "Boolean", "display_order": 1},
                {"feature_key": "contacts", "feature_label": "Contact Limit", "feature_type": "Numeric", "display_order": 2},
                {"feature_key": "automations", "feature_label": "Marketing Automations", "feature_type": "Boolean", "display_order": 3},
                {"feature_key": "templates", "feature_label": "Email Templates", "feature_type": "Numeric", "display_order": 4},
                {"feature_key": "smtp_accounts", "feature_label": "SMTP Accounts", "feature_type": "Numeric", "display_order": 5},
                {"feature_key": "analytics", "feature_label": "Analytics Dashboard", "feature_type": "Boolean", "display_order": 6},
                {"feature_key": "segmentation", "feature_label": "List Segmentation", "feature_type": "Boolean", "display_order": 7},
                {"feature_key": "ab_testing", "feature_label": "A/B Testing", "feature_type": "Boolean", "display_order": 8},
            ],
        },
        "SOCIAL": SOCIAL_PRODUCT,
        "ADS": {
            "product_name": "Ads",
            "product_code": "ADS",
            "description": "Digital advertising management platform",
            "features": [
                {"feature_key": "ad_accounts", "feature_label": "Ad Accounts", "feature_type": "Numeric", "display_order": 1},
                {"feature_key": "campaigns", "feature_label": "Active Campaigns", "feature_type": "Numeric", "display_order": 2},
                {"feature_key": "ad_sets", "feature_label": "Ad Sets per Campaign", "feature_type": "Numeric", "display_order": 3},
                {"feature_key": "creatives", "feature_label": "Creative Assets", "feature_type": "Numeric", "display_order": 4},
                {"feature_key": "budget_management", "feature_label": "Budget Management", "feature_type": "Boolean", "display_order": 5},
                {"feature_key": "analytics", "feature_label": "Analytics Dashboard", "feature_type": "Boolean", "display_order": 6},
                {"feature_key": "audience_targeting", "feature_label": "Audience Targeting", "feature_type": "Boolean", "display_order": 7},
                {"feature_key": "automated_rules", "feature_label": "Automated Rules", "feature_type": "Boolean", "display_order": 8},
            ],
        },
    }

    for code, data in products.items():
        if frappe.db.exists("MSuite Product", {"product_code": code}):
            continue
        doc = frappe.new_doc("MSuite Product")
        doc.product_name = data["product_name"]
        doc.product_code = data["product_code"]
        doc.description = data["description"]
        doc.is_active = 1
        for feat in data["features"]:
            doc.append("features", feat)
        doc.insert(ignore_permissions=True)


def _create_item_if_not_exists(item_code, item_name, item_group, rate):
    """Helper to create ERPNext Item."""
    if frappe.db.exists("Item", item_code):
        return
    doc = frappe.new_doc("Item")
    doc.item_code = item_code
    doc.item_name = item_name
    doc.item_group = item_group
    doc.stock_uom = "Nos"
    doc.is_stock_item = 0
    doc.include_item_in_manufacturing = 0
    doc.standard_rate = rate
    doc.insert(ignore_permissions=True)


def _create_subscription_plan_if_not_exists(plan_name, item_code, rate):
    """Helper to create ERPNext Subscription Plan."""
    if frappe.db.exists("Subscription Plan", plan_name):
        return
    doc = frappe.new_doc("Subscription Plan")
    doc.plan_name = plan_name
    doc.item = item_code
    doc.price_determination = "Fixed Rate"
    doc.cost = rate
    doc.billing_interval = "Month"
    doc.billing_interval_count = 1
    doc.insert(ignore_permissions=True)


def _get_product_feature_name(product_code, feature_key):
    """Helper to find MSuite Product Feature name."""
    product = frappe.get_doc("MSuite Product", {"product_code": product_code})
    for feat in product.features:
        if feat.feature_key == feature_key:
            return feat.name
    return None


def _seed_plans():
    """Seeds MSuite Plans for all products and tiers."""
    # ---- WhatsApp Plans ----
    wa_plans = [
        {
            "plan_name": "WhatsApp Trial", "plan_code": "WA-TRIAL", "tier": "Trial",
            "item_code": "WA-TRIAL-MONTHLY", "rate": 0,
            "features": {
                "messaging":      (True,  100,  "per month"),
                "templates":      (True,  3,    "total"),
                "calling":        (False, None, None),
                "flows":          (False, None, None),
                "bulk_messaging": (False, None, None),
                "inbox_whatsapp": (False, None, None),
                "chatbot":        (False, None, None),
                "analytics":      (True,  None, None),
                "inbox_seats":    (False, None, None),  # off entirely; inbox locked anyway
            },
            "grants": [],
        },
        {
            "plan_name": "WhatsApp Basic", "plan_code": "WA-BASIC", "tier": "Basic",
            "item_code": "WA-BASIC-MONTHLY", "rate": 999,
            "features": {
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
            "grants": [],
        },
        {
            "plan_name": "WhatsApp Pro", "plan_code": "WA-PRO", "tier": "Pro",
            "item_code": "WA-PRO-MONTHLY", "rate": 1999,
            "features": {
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
            "grants": [],
        },
        {
            "plan_name": "WhatsApp Business", "plan_code": "WA-BIZ", "tier": "Business",
            "item_code": "WA-BIZ-MONTHLY", "rate": 2999,
            "features": {
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
            "grants": [{"granted_plan_code": "SOCIAL-BASIC", "grant_type": "Complimentary", "expires_after_days": 0}],
        },
        {
            "plan_name": "WhatsApp Enterprise", "plan_code": "WA-ENT", "tier": "Enterprise",
            "item_code": "WA-ENT-MONTHLY", "rate": 5999,
            "features": {
                "messaging":      (True, None, "per month"),
                "templates":      (True, None, "total"),
                "calling":        (True, None, None),
                "flows":          (True, None, "total"),
                "bulk_messaging": (True, None, "per campaign"),
                "inbox_whatsapp": (True, None, None),
                "chatbot":        (True, None, None),
                "analytics":      (True, None, None),
                "inbox_seats":    (True, None, "seats"),  # unlimited
            },
            "grants": [{"granted_plan_code": "SOCIAL-PRO", "grant_type": "Complimentary", "expires_after_days": 0}],
        },
    ]

    # ---- Email Plans ----
    email_plans = [
        {
            "plan_name": "Email Trial", "plan_code": "EMAIL-TRIAL", "tier": "Trial",
            "item_code": "EMAIL-TRIAL-MONTHLY", "rate": 0,
            "features": {
                "campaigns": (True, None, None), "contacts": (True, 500, "total"),
                "automations": (False, None, None), "templates": (True, 3, "total"),
                "smtp_accounts": (True, 1, "total"), "analytics": (True, None, None),
                "segmentation": (False, None, None), "ab_testing": (False, None, None),
            },
            "grants": [],
        },
        {
            "plan_name": "Email Basic", "plan_code": "EMAIL-BASIC", "tier": "Basic",
            "item_code": "EMAIL-BASIC-MONTHLY", "rate": 799,
            "features": {
                "campaigns": (True, None, None), "contacts": (True, 2500, "total"),
                "automations": (False, None, None), "templates": (True, 10, "total"),
                "smtp_accounts": (True, 2, "total"), "analytics": (True, None, None),
                "segmentation": (False, None, None), "ab_testing": (False, None, None),
            },
            "grants": [],
        },
        {
            "plan_name": "Email Pro", "plan_code": "EMAIL-PRO", "tier": "Pro",
            "item_code": "EMAIL-PRO-MONTHLY", "rate": 1599,
            "features": {
                "campaigns": (True, None, None), "contacts": (True, 10000, "total"),
                "automations": (True, None, None), "templates": (True, 50, "total"),
                "smtp_accounts": (True, 5, "total"), "analytics": (True, None, None),
                "segmentation": (True, None, None), "ab_testing": (False, None, None),
            },
            "grants": [],
        },
        {
            "plan_name": "Email Business", "plan_code": "EMAIL-BIZ", "tier": "Business",
            "item_code": "EMAIL-BIZ-MONTHLY", "rate": 2499,
            "features": {
                "campaigns": (True, None, None), "contacts": (True, 50000, "total"),
                "automations": (True, None, None), "templates": (True, 100, "total"),
                "smtp_accounts": (True, 10, "total"), "analytics": (True, None, None),
                "segmentation": (True, None, None), "ab_testing": (True, None, None),
            },
            "grants": [],
        },
        {
            "plan_name": "Email Enterprise", "plan_code": "EMAIL-ENT", "tier": "Enterprise",
            "item_code": "EMAIL-ENT-MONTHLY", "rate": 4999,
            "features": {
                "campaigns": (True, None, None), "contacts": (True, None, "total"),
                "automations": (True, None, None), "templates": (True, None, "total"),
                "smtp_accounts": (True, None, "total"), "analytics": (True, None, None),
                "segmentation": (True, None, None), "ab_testing": (True, None, None),
            },
            "grants": [],
        },
    ]

    # ---- Social Post Plans (sourced from msuite/seeds/social_post.py) ----
    social_plans = SOCIAL_PLANS_FOR_INSTALL

    # ---- Ads Plans ----
    ads_plans = [
        {
            "plan_name": "Ads Trial", "plan_code": "ADS-TRIAL", "tier": "Trial",
            "item_code": "ADS-TRIAL-MONTHLY", "rate": 0,
            "features": {
                "ad_accounts": (True, 1, "total"), "campaigns": (True, 2, "total"),
                "ad_sets": (True, 2, "per campaign"), "creatives": (True, 5, "total"),
                "budget_management": (False, None, None), "analytics": (True, None, None),
                "audience_targeting": (False, None, None), "automated_rules": (False, None, None),
            },
            "grants": [],
        },
        {
            "plan_name": "Ads Basic", "plan_code": "ADS-BASIC", "tier": "Basic",
            "item_code": "ADS-BASIC-MONTHLY", "rate": 1499,
            "features": {
                "ad_accounts": (True, 2, "total"), "campaigns": (True, 5, "total"),
                "ad_sets": (True, 3, "per campaign"), "creatives": (True, 20, "total"),
                "budget_management": (False, None, None), "analytics": (True, None, None),
                "audience_targeting": (False, None, None), "automated_rules": (False, None, None),
            },
            "grants": [],
        },
        {
            "plan_name": "Ads Pro", "plan_code": "ADS-PRO", "tier": "Pro",
            "item_code": "ADS-PRO-MONTHLY", "rate": 2999,
            "features": {
                "ad_accounts": (True, 5, "total"), "campaigns": (True, 15, "total"),
                "ad_sets": (True, 5, "per campaign"), "creatives": (True, 50, "total"),
                "budget_management": (True, None, None), "analytics": (True, None, None),
                "audience_targeting": (True, None, None), "automated_rules": (False, None, None),
            },
            "grants": [],
        },
        {
            "plan_name": "Ads Business", "plan_code": "ADS-BIZ", "tier": "Business",
            "item_code": "ADS-BIZ-MONTHLY", "rate": 4999,
            "features": {
                "ad_accounts": (True, 10, "total"), "campaigns": (True, 50, "total"),
                "ad_sets": (True, 10, "per campaign"), "creatives": (True, 200, "total"),
                "budget_management": (True, None, None), "analytics": (True, None, None),
                "audience_targeting": (True, None, None), "automated_rules": (True, None, None),
            },
            "grants": [],
        },
        {
            "plan_name": "Ads Enterprise", "plan_code": "ADS-ENT", "tier": "Enterprise",
            "item_code": "ADS-ENT-MONTHLY", "rate": 9999,
            "features": {
                "ad_accounts": (True, None, "total"), "campaigns": (True, None, "total"),
                "ad_sets": (True, None, "per campaign"), "creatives": (True, None, "total"),
                "budget_management": (True, None, None), "analytics": (True, None, None),
                "audience_targeting": (True, None, None), "automated_rules": (True, None, None),
            },
            "grants": [],
        },
    ]

    product_map = {"WA": "WhatsApp", "EMAIL": "Email", "SOCIAL": "Social Post", "ADS": "Ads"}
    all_plans = wa_plans + email_plans + social_plans + ads_plans

    for plan_data in all_plans:
        plan_code = plan_data["plan_code"]
        if frappe.db.exists("MSuite Plan", {"plan_code": plan_code}):
            continue

        product_prefix = plan_code.split("-")[0]
        item_group = product_map.get(product_prefix, "All Item Groups")
        item_code = plan_data["item_code"]

        _create_item_if_not_exists(item_code, plan_data["plan_name"], item_group, plan_data["rate"])
        _create_subscription_plan_if_not_exists(item_code, item_code, plan_data["rate"])

        product_code = product_prefix
        product_name = frappe.db.get_value("MSuite Product", {"product_code": product_code}, "name")
        if not product_name:
            continue

        doc = frappe.new_doc("MSuite Plan")
        doc.plan_name = plan_data["plan_name"]
        doc.plan_code = plan_code
        doc.product = product_name
        doc.tier = plan_data["tier"]
        doc.item = item_code
        doc.is_active = 1
        doc.activated_on = frappe.utils.now()

        product_doc = frappe.get_doc("MSuite Product", product_name)
        feature_map = {f.feature_key: f.name for f in product_doc.features}

        for feat_key, (enabled, limit_val, limit_label) in plan_data["features"].items():
            pf_name = feature_map.get(feat_key)
            if pf_name:
                row = {
                    "product_feature": pf_name,
                    "is_enabled": 1 if enabled else 0,
                }
                if limit_val is not None:
                    row["limit_value"] = limit_val
                if limit_label:
                    row["limit_label"] = limit_label
                doc.append("features", row)

        doc.insert(ignore_permissions=True)

    # Apply grant rules after all plans are created
    _apply_grant_rules(all_plans)


def _apply_grant_rules(all_plans):
    """Apply complimentary grant rules after all plans exist."""
    for plan_data in all_plans:
        if not plan_data.get("grants"):
            continue
        plan_code = plan_data["plan_code"]
        plan_name = frappe.db.get_value("MSuite Plan", {"plan_code": plan_code}, "name")
        if not plan_name:
            continue

        plan_doc = frappe.get_doc("MSuite Plan", plan_name)
        if plan_doc.grants:
            continue

        for grant_rule in plan_data["grants"]:
            granted_plan_name = frappe.db.get_value(
                "MSuite Plan", {"plan_code": grant_rule["granted_plan_code"]}, "name"
            )
            if granted_plan_name:
                plan_doc.append("grants", {
                    "granted_plan": granted_plan_name,
                    "grant_type": grant_rule["grant_type"],
                    "expires_after_days": grant_rule.get("expires_after_days", 0),
                })

        if plan_doc.grants:
            plan_doc.save(ignore_permissions=True)


def _seed_bundles():
    """Seeds MSuite Bundles and their bundle Items."""
    if frappe.db.exists("MSuite Bundle", {"bundle_code": "MARKETING-COMBO"}):
        return

    wa_biz = frappe.db.get_value("MSuite Plan", {"plan_code": "WA-BIZ"}, "name")
    social_pro = frappe.db.get_value("MSuite Plan", {"plan_code": "SOCIAL-PRO"}, "name")

    if not wa_biz or not social_pro:
        return

    bundle = frappe.new_doc("MSuite Bundle")
    bundle.bundle_name = "Marketing Combo"
    bundle.bundle_code = "MARKETING-COMBO"
    bundle.is_active = 1
    bundle.activated_on = frappe.utils.now()
    bundle.description = "WhatsApp Business + Social Post Pro at a discounted price"
    bundle.append("components", {"plan": wa_biz, "quantity": 1})
    bundle.append("components", {"plan": social_pro, "quantity": 1})
    bundle.insert(ignore_permissions=True)

    item_code = "MARKETING-COMBO-MONTHLY"
    _create_item_if_not_exists(item_code, "Marketing Combo Monthly", "Bundles", 5999)

    item_doc = frappe.get_doc("Item", item_code)
    item_doc.is_msuite_bundle = 1
    item_doc.msuite_bundle = bundle.name
    item_doc.save(ignore_permissions=True)

    _create_subscription_plan_if_not_exists(item_code, item_code, 5999)
