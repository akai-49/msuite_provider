"""
One-shot setup for the Ads E2E test on the provider side.

Run:
  bench --site msuite.provider.v2 execute msuite.setup_ads_e2e.run

Idempotent: safe to re-run. Creates the ADS product + ADS-TRIAL plan
and grants it to the E2E client. Nothing deleted, nothing destructive.
"""
import frappe
from frappe.utils import today


ADS_FEATURES = [
    ("ads_meta",                 "Boolean", "Meta Ads",                        10),
    ("ads_google",               "Boolean", "Google Ads",                      20),
    ("ads_lead_gen",             "Boolean", "Meta Lead Gen Forms",             30),
    ("ads_advanced_targeting",   "Boolean", "Custom / Lookalike Audiences",    40),
    ("ads_insights",             "Boolean", "Async insights sync",             50),
    ("ads_bulk_duplicate",       "Boolean", "Bulk duplicate campaigns",        60),
    ("ads_creative_library",     "Boolean", "Creative Library page",           70),
    ("active_ad_campaigns",      "Numeric", "Max concurrent Ad Campaigns",     80),
    ("ad_accounts_per_platform", "Numeric", "Ad Accounts per platform",        90),
    ("ads_monthly_spend",        "Numeric", "Monthly spend ceiling",          100),
]

TRIAL_FEATURE_VALUES = {
    "ads_meta":                 (1, 0),
    "ads_google":               (0, 0),
    "ads_lead_gen":             (1, 0),
    "ads_advanced_targeting":   (1, 0),
    "ads_insights":             (1, 0),
    "ads_bulk_duplicate":       (1, 0),
    "ads_creative_library":     (1, 0),
    "active_ad_campaigns":      (1, 5),
    "ad_accounts_per_platform": (1, 2),
    "ads_monthly_spend":        (1, 0),
}

CLIENT_CUSTOMER = "E2E Client Co"
PLAN_CODE = "ADS-TRIAL"


def run():
    _upsert_product()
    _upsert_plan()
    _grant()
    _report()


def _upsert_product() -> None:
    existing = frappe.db.exists("MSuite Product", {"product_code": "ADS"})
    if existing:
        doc = frappe.get_doc("MSuite Product", existing)
    else:
        doc = frappe.new_doc("MSuite Product")
        doc.product_code = "ADS"
        doc.product_name = "Ads"
        doc.is_active = 1
        doc.description = "Meta + Google Ads publishing, lead gen, insights."

    current_keys = {row.feature_key: row for row in (doc.features or [])}
    for key, ftype, label, order in ADS_FEATURES:
        if key in current_keys:
            row = current_keys[key]
            row.feature_label = label
            row.feature_type = ftype
            row.display_order = order
        else:
            doc.append("features", {
                "feature_key": key,
                "feature_label": label,
                "feature_type": ftype,
                "display_order": order,
            })

    doc.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"[OK] Product ADS {'updated' if existing else 'created'} ({doc.name})")


def _upsert_plan() -> None:
    product_name = frappe.db.get_value("MSuite Product", {"product_code": "ADS"}, "name")
    if not product_name:
        raise Exception("ADS product missing after upsert — aborting plan setup.")

    existing = frappe.db.exists("MSuite Plan", {"plan_code": PLAN_CODE})
    if existing:
        plan = frappe.get_doc("MSuite Plan", existing)
    else:
        plan = frappe.new_doc("MSuite Plan")
        plan.plan_code = PLAN_CODE
        plan.plan_name = "Ads Trial"
        plan.tier = "Trial"
        plan.is_active = 1
        plan.activated_on = today()

    plan.product = product_name

    pf_rows = frappe.get_all(
        "MSuite Product Feature",
        filters={"parent": product_name, "parenttype": "MSuite Product"},
        fields=["name", "feature_key"],
    )
    key_to_pf = {r.feature_key: r.name for r in pf_rows}

    existing_feature_rows = {row.product_feature: row for row in (plan.features or [])}
    for key, (enabled, limit_value) in TRIAL_FEATURE_VALUES.items():
        pf_name = key_to_pf.get(key)
        if not pf_name:
            print(f"  [skip] No product feature '{key}' to link")
            continue
        if pf_name in existing_feature_rows:
            row = existing_feature_rows[pf_name]
            row.is_enabled = enabled
            row.limit_value = limit_value
        else:
            plan.append("features", {
                "product_feature": pf_name,
                "is_enabled": enabled,
                "limit_value": limit_value,
            })

    plan.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"[OK] Plan {PLAN_CODE} {'updated' if existing else 'created'} ({plan.name})")


def _grant() -> None:
    if frappe.db.exists(
        "MSuite Customer Grant",
        {"customer": CLIENT_CUSTOMER, "granted_plan": PLAN_CODE, "status": "Active"},
    ):
        print(f"[OK] Grant already active for {CLIENT_CUSTOMER} → {PLAN_CODE}")
        return

    anchor_sub = frappe.db.get_value(
        "MSuite Customer Grant",
        {"customer": CLIENT_CUSTOMER, "status": "Active"},
        "source_subscription",
    )

    grant = frappe.get_doc({
        "doctype": "MSuite Customer Grant",
        "customer": CLIENT_CUSTOMER,
        "granted_plan": PLAN_CODE,
        "grant_type": "Complimentary",
        "status": "Active",
        "granted_on": today(),
        "source_subscription": anchor_sub or "",
    })
    grant.insert(ignore_permissions=True)
    frappe.db.commit()
    print(f"[OK] Grant {grant.name} → {CLIENT_CUSTOMER} / {PLAN_CODE}")


def _report() -> None:
    grants = frappe.get_all(
        "MSuite Customer Grant",
        filters={"customer": CLIENT_CUSTOMER, "status": "Active"},
        fields=["name", "granted_plan", "grant_type", "granted_on"],
    )
    print(f"\n{CLIENT_CUSTOMER} — active grants:")
    for g in grants:
        print(f"  {g.name}: {g.granted_plan} [{g.grant_type}] on {g.granted_on}")
