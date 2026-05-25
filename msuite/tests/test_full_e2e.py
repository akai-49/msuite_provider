"""
Full end-to-end test: Products → Plans → Bundles → Subscriptions → Grants → Push to Client.
Run: bench --site msuite.provider.v2 execute msuite.tests.test_full_e2e.run
"""
import frappe
from frappe.utils import today

P = "PASS"
F = "FAIL"
results = []
CLIENT_URL = "http://localhost:8000"


def check(label, condition):
    results.append((label, condition))
    print(f"  [{P if condition else F}] {label}")


def run():
    print("\n" + "=" * 60)
    print("  FULL E2E: Product → Plan → Bundle → Subscribe → Push")
    print("=" * 60)

    # ── CLEANUP ──
    print("\n── Cleanup ──")
    _cleanup()
    print("  Done")

    # ── STEP 1: Create Products ──
    print("\n── Step 1: Create Products ──")
    wa = frappe.new_doc("MSuite Product")
    wa.product_name = "WhatsApp"
    wa.product_code = "WA"
    wa.is_active = 1
    for f in [
        {"feature_key": "messaging", "feature_label": "Messaging", "feature_type": "Numeric", "display_order": 1},
        {"feature_key": "templates", "feature_label": "Templates", "feature_type": "Numeric", "display_order": 2},
        {"feature_key": "calling", "feature_label": "Voice Calling", "feature_type": "Boolean", "display_order": 3},
        {"feature_key": "chatbot", "feature_label": "Chatbot Builder", "feature_type": "Boolean", "display_order": 4},
    ]:
        wa.append("features", f)
    wa.insert(ignore_permissions=True)

    social = frappe.new_doc("MSuite Product")
    social.product_name = "Social Post"
    social.product_code = "SOCIAL"
    social.is_active = 1
    for f in [
        {"feature_key": "scheduled_posts", "feature_label": "Scheduled Posts", "feature_type": "Numeric", "display_order": 1},
        {"feature_key": "platforms", "feature_label": "Connected Platforms", "feature_type": "Numeric", "display_order": 2},
        {"feature_key": "ai_captions", "feature_label": "AI Caption Generator", "feature_type": "Boolean", "display_order": 3},
    ]:
        social.append("features", f)
    social.insert(ignore_permissions=True)
    frappe.db.commit()

    wa_features = {f.feature_key: f.name for f in wa.features}
    social_features = {f.feature_key: f.name for f in social.features}
    check("2 products created", frappe.db.count("MSuite Product") == 2)

    # ── STEP 2: Create Plans ──
    print("\n── Step 2: Create Plans ──")

    # WA-TRIAL
    p = frappe.new_doc("MSuite Plan")
    p.plan_name = "WhatsApp Trial"
    p.plan_code = "WA-TRIAL"
    p.product = "WA"
    p.tier = "Trial"
    p.append("features", {"product_feature": wa_features["messaging"], "is_enabled": 1, "limit_value": 100, "limit_label": "per month"})
    p.append("features", {"product_feature": wa_features["templates"], "is_enabled": 1, "limit_value": 3})
    p.append("features", {"product_feature": wa_features["calling"], "is_enabled": 0})
    p.append("features", {"product_feature": wa_features["chatbot"], "is_enabled": 0})
    p.insert(ignore_permissions=True)
    from msuite.msuite_plan.doctype.msuite_plan.msuite_plan import activate_plan
    activate_plan("WA-TRIAL")

    # WA-BASIC
    p = frappe.new_doc("MSuite Plan")
    p.plan_name = "WhatsApp Basic"
    p.plan_code = "WA-BASIC"
    p.product = "WA"
    p.tier = "Basic"
    p.append("pricing", {"billing_interval": "Monthly", "rate": 999})
    p.append("features", {"product_feature": wa_features["messaging"], "is_enabled": 1, "limit_value": 1000, "limit_label": "per month"})
    p.append("features", {"product_feature": wa_features["templates"], "is_enabled": 1, "limit_value": 5})
    p.append("features", {"product_feature": wa_features["calling"], "is_enabled": 0})
    p.append("features", {"product_feature": wa_features["chatbot"], "is_enabled": 0})
    p.insert(ignore_permissions=True)
    activate_plan("WA-BASIC")

    # SOCIAL-BASIC
    p = frappe.new_doc("MSuite Plan")
    p.plan_name = "Social Post Basic"
    p.plan_code = "SOCIAL-BASIC"
    p.product = "SOCIAL"
    p.tier = "Basic"
    p.append("pricing", {"billing_interval": "Monthly", "rate": 599})
    p.append("features", {"product_feature": social_features["scheduled_posts"], "is_enabled": 1, "limit_value": 30, "limit_label": "per month"})
    p.append("features", {"product_feature": social_features["platforms"], "is_enabled": 1, "limit_value": 3})
    p.append("features", {"product_feature": social_features["ai_captions"], "is_enabled": 0})
    p.insert(ignore_permissions=True)
    activate_plan("SOCIAL-BASIC")

    # SOCIAL-PRO
    p = frappe.new_doc("MSuite Plan")
    p.plan_name = "Social Post Pro"
    p.plan_code = "SOCIAL-PRO"
    p.product = "SOCIAL"
    p.tier = "Pro"
    p.append("pricing", {"billing_interval": "Monthly", "rate": 1199})
    p.append("features", {"product_feature": social_features["scheduled_posts"], "is_enabled": 1, "limit_value": 100, "limit_label": "per month"})
    p.append("features", {"product_feature": social_features["platforms"], "is_enabled": 1, "limit_value": 5})
    p.append("features", {"product_feature": social_features["ai_captions"], "is_enabled": 1})
    p.insert(ignore_permissions=True)
    activate_plan("SOCIAL-PRO")

    # WA-BIZ (with grant rule → SOCIAL-BASIC)
    p = frappe.new_doc("MSuite Plan")
    p.plan_name = "WhatsApp Business"
    p.plan_code = "WA-BIZ"
    p.product = "WA"
    p.tier = "Business"
    p.append("pricing", {"billing_interval": "Monthly", "rate": 2999})
    p.append("features", {"product_feature": wa_features["messaging"], "is_enabled": 1, "limit_label": "per month"})
    p.append("features", {"product_feature": wa_features["templates"], "is_enabled": 1, "limit_value": 100})
    p.append("features", {"product_feature": wa_features["calling"], "is_enabled": 1})
    p.append("features", {"product_feature": wa_features["chatbot"], "is_enabled": 1})
    p.append("grants", {"granted_plan": "SOCIAL-BASIC", "grant_type": "Complimentary", "expires_after_days": 0})
    p.insert(ignore_permissions=True)
    activate_plan("WA-BIZ")
    frappe.db.commit()

    check("5 plans created and activated", frappe.db.count("MSuite Plan", {"is_active": 1}) == 5)
    check("WA-BIZ-MONTHLY Item exists", frappe.db.exists("Item", "WA-BIZ-MONTHLY"))
    check("Pricing Rule for grant rule exists", frappe.db.exists("Pricing Rule", {"title": ["like", "MSuite - WA-BIZ%"]}))

    # ── STEP 3: Create Bundle ──
    print("\n── Step 3: Create Bundle ──")
    b = frappe.new_doc("MSuite Bundle")
    b.bundle_name = "Marketing Combo"
    b.bundle_code = "MARKETING-COMBO"
    b.append("components", {"plan": "WA-BIZ", "quantity": 1})
    b.append("components", {"plan": "SOCIAL-PRO", "quantity": 1})
    b.append("pricing", {"billing_interval": "Monthly", "rate": 3499})
    b.insert(ignore_permissions=True)
    from msuite.msuite_bundle.doctype.msuite_bundle.msuite_bundle import activate_bundle
    activate_bundle("MARKETING-COMBO")
    frappe.db.commit()

    check("Bundle created and activated", frappe.db.get_value("MSuite Bundle", "MARKETING-COMBO", "is_active") == 1)
    check("MARKETING-COMBO-MONTHLY Item exists", frappe.db.exists("Item", "MARKETING-COMBO-MONTHLY"))

    # ── STEP 4: Create Customer + Subscription (WA-BIZ — has grant rule) ──
    print("\n── Step 4: Customer + WA-BIZ Subscription ──")
    cust = _create_customer("E2E Client Co")

    sub = frappe.new_doc("Subscription")
    sub.party_type = "Customer"
    sub.party = cust
    sub.start_date = today()
    sub.append("plans", {"plan": "WA-BIZ-MONTHLY", "qty": 1})
    sub.insert(ignore_permissions=True)
    frappe.db.commit()

    grants = frappe.get_all("MSuite Customer Grant", filters={"customer": cust, "status": "Active"}, fields=["granted_plan", "grant_type"])
    grant_map = {g.granted_plan: g.grant_type for g in grants}
    check("WA-BIZ Paid grant", grant_map.get("WA-BIZ") == "Paid")
    check("SOCIAL-BASIC Complimentary grant", grant_map.get("SOCIAL-BASIC") == "Complimentary")

    # Check entitlements
    from msuite.services.entitlement_service import get_customer_entitlements
    ent = get_customer_entitlements(cust)
    features = ent.get("features", {})
    check("messaging=unlimited", features.get("messaging", {}).get("enabled") and features["messaging"].get("limit") is None)
    check("templates=100", features.get("templates", {}).get("limit") == 100)
    check("chatbot=enabled", features.get("chatbot", {}).get("enabled"))
    check("scheduled_posts=30 (complimentary)", features.get("scheduled_posts", {}).get("limit") == 30)
    check("ai_captions=disabled (SOCIAL-BASIC)", not features.get("ai_captions", {}).get("enabled", False))

    # ── STEP 5: Create MSuite Client + Activate + Push ──
    print("\n── Step 5: Activate Client + Push Plan ──")
    if frappe.db.exists("MSuite Client", "E2E-CLIENT"):
        frappe.delete_doc("MSuite Client", "E2E-CLIENT", force=True, ignore_permissions=True)
        frappe.db.commit()

    client = frappe.new_doc("MSuite Client")
    client.client_name = "E2E-CLIENT"
    client.customer = cust
    client.client_url = CLIENT_URL
    client.insert(ignore_permissions=True)
    frappe.db.commit()

    # Test connection
    from msuite.msuite_client.doctype.msuite_client.msuite_client import test_connection, activate_client as act_client, push_plan
    conn = test_connection("E2E-CLIENT")
    check("Client connection OK", conn.get("status") == "ok")

    # Activate
    act = act_client("E2E-CLIENT")
    check("Client activated", act.get("status") == "success")
    client.reload()
    check("Client status=Active", client.status == "Active")

    # Push plan
    push = push_plan("E2E-CLIENT")
    check("Plan pushed", push.get("status") == "success")

    # ── STEP 6: Verify client received plan data ──
    print("\n── Step 6: Verify Client Data ──")
    import requests
    resp = requests.get(f"{CLIENT_URL}/api/method/msuite_workspace.api.v1.connect.sync.health_check", timeout=10)
    check("Client healthy", resp.status_code == 200)

    # ── STEP 7: Bundle subscription ──
    print("\n── Step 7: Bundle Subscription ──")
    cust2 = _create_customer("Bundle Client Co")

    sub2 = frappe.new_doc("Subscription")
    sub2.party_type = "Customer"
    sub2.party = cust2
    sub2.start_date = today()
    sub2.append("plans", {"plan": "MARKETING-COMBO-MONTHLY", "qty": 1})
    sub2.insert(ignore_permissions=True)
    frappe.db.commit()

    grants2 = frappe.get_all("MSuite Customer Grant", filters={"customer": cust2, "status": "Active"}, fields=["granted_plan", "grant_type"])
    grant_map2 = {g.granted_plan: g.grant_type for g in grants2}
    check("Bundle: WA-BIZ grant", "WA-BIZ" in grant_map2)
    check("Bundle: SOCIAL-PRO grant", "SOCIAL-PRO" in grant_map2)
    check("Bundle: NO SOCIAL-BASIC (higher tier skip)", "SOCIAL-BASIC" not in grant_map2)

    ent2 = get_customer_entitlements(cust2)
    features2 = ent2.get("features", {})
    check("Bundle: messaging=unlimited", features2.get("messaging", {}).get("limit") is None)
    check("Bundle: scheduled_posts=100 (PRO)", features2.get("scheduled_posts", {}).get("limit") == 100)
    check("Bundle: ai_captions=enabled (PRO)", features2.get("ai_captions", {}).get("enabled"))

    # ── STEP 8: Build client plan data ──
    print("\n── Step 8: Verify Plan Data Format ──")
    from msuite.services.client_service import build_client_plan_data
    plan_data = build_client_plan_data(cust)
    print(f"  Plan name: {plan_data.get('plan_name')}")
    print(f"  Features: {list(plan_data.get('features', {}).keys())}")
    check("Plan data has plan_name", bool(plan_data.get("plan_name")))
    check("Plan data has features dict", bool(plan_data.get("features")))
    check("Features have labels", all("label" in f for f in plan_data["features"].values()))
    check("messaging label is 'Messaging'", plan_data["features"].get("messaging", {}).get("label") == "Messaging")

    # ── Summary ──
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    print(f"  FULL E2E: {passed}/{len(results)} passed")
    print("=" * 60)
    if failed:
        print("  Failed:")
        for label, ok in results:
            if not ok:
                print(f"    [{F}] {label}")
    print("")


def _create_customer(name):
    if not frappe.db.exists("Customer", name):
        c = frappe.new_doc("Customer")
        c.customer_name = name
        c.customer_group = "All Customer Groups"
        c.territory = "All Territories"
        c.customer_type = "Company"
        c.insert(ignore_permissions=True)
        frappe.db.commit()
    return name


def _cleanup():
    """Clean slate for E2E test."""
    # Grants
    for g in frappe.get_all("MSuite Customer Grant", pluck="name"):
        frappe.delete_doc("MSuite Customer Grant", g, force=True, ignore_permissions=True)
    # Subscriptions
    for s in frappe.get_all("Subscription", pluck="name"):
        try:
            frappe.delete_doc("Subscription", s, force=True, ignore_permissions=True)
        except Exception:
            pass
    # MSuite Clients
    for c in frappe.get_all("MSuite Client", pluck="name"):
        frappe.delete_doc("MSuite Client", c, force=True, ignore_permissions=True)
    # Bundles
    for b in frappe.get_all("MSuite Bundle", pluck="name"):
        frappe.delete_doc("MSuite Bundle", b, force=True, ignore_permissions=True)
    # Plans
    for p in frappe.get_all("MSuite Plan", pluck="name"):
        frappe.delete_doc("MSuite Plan", p, force=True, ignore_permissions=True)
    # Products
    for p in frappe.get_all("MSuite Product", pluck="name"):
        frappe.delete_doc("MSuite Product", p, force=True, ignore_permissions=True)
    # Pricing Rules
    for pr in frappe.get_all("Pricing Rule", filters={"title": ["like", "MSuite%"]}, pluck="name"):
        frappe.delete_doc("Pricing Rule", pr, force=True, ignore_permissions=True)
    # Items + Sub Plans
    for item in frappe.get_all("Item", filters={"name": ["like", "WA-%"]}, pluck="name"):
        try: frappe.delete_doc("Item", item, force=True, ignore_permissions=True)
        except Exception: pass
    for item in frappe.get_all("Item", filters={"name": ["like", "SOCIAL-%"]}, pluck="name"):
        try: frappe.delete_doc("Item", item, force=True, ignore_permissions=True)
        except Exception: pass
    for item in frappe.get_all("Item", filters={"name": ["like", "MARKETING-%"]}, pluck="name"):
        try: frappe.delete_doc("Item", item, force=True, ignore_permissions=True)
        except Exception: pass
    for sp in frappe.get_all("Subscription Plan", filters={"name": ["like", "WA-%"]}, pluck="name"):
        try: frappe.delete_doc("Subscription Plan", sp, force=True, ignore_permissions=True)
        except Exception: pass
    for sp in frappe.get_all("Subscription Plan", filters={"name": ["like", "SOCIAL-%"]}, pluck="name"):
        try: frappe.delete_doc("Subscription Plan", sp, force=True, ignore_permissions=True)
        except Exception: pass
    for sp in frappe.get_all("Subscription Plan", filters={"name": ["like", "MARKETING-%"]}, pluck="name"):
        try: frappe.delete_doc("Subscription Plan", sp, force=True, ignore_permissions=True)
        except Exception: pass
    # Test customers
    for c in ["E2E Client Co", "Bundle Client Co"]:
        if frappe.db.exists("Customer", c):
            try: frappe.delete_doc("Customer", c, force=True, ignore_permissions=True)
            except Exception: pass
    # Customer groups
    for cg in frappe.get_all("Customer Group", filters={"name": ["like", "MSuite -%"]}, pluck="name"):
        try: frappe.delete_doc("Customer Group", cg, force=True, ignore_permissions=True)
        except Exception: pass
    frappe.db.commit()


run()
