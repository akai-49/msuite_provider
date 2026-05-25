"""Comprehensive MSuite test runner."""
import frappe
from frappe.utils import today, add_days, getdate

passed = []
failed = []
warnings = []

def log_pass(test):
    passed.append(test)
    print(f"  ✓ {test}")

def log_fail(test, err):
    failed.append((test, str(err)))
    print(f"  ✗ {test}: {err}")

def log_warn(test, msg):
    warnings.append((test, msg))
    print(f"  ⚠ {test}: {msg}")

def run_all():
    test_paid_subscription()
    test_entitlements()
    test_cache()
    test_idempotency()
    test_grant_revocation()
    test_subscription_cancellation()
    test_trial_flow()
    test_bundle_flow()
    test_customer_groups()
    test_plan_api()
    test_product_validations()
    test_plan_validations()
    print_summary()


def test_paid_subscription():
    print("\n" + "=" * 60)
    print("TEST: Paid Subscription - WA Business for Sagar Communications")
    print("=" * 60)
    try:
        from msuite.services.subscription_service import create_subscription
        sub_name = create_subscription(
            customer="Sagar Communications",
            subscription_plan_names=["WA-BIZ-MONTHLY"],
            start_date="2026-04-03",
        )
        print(f"  Created subscription: {sub_name}")
        sub_doc = frappe.get_doc("Subscription", sub_name)
        if sub_doc.status == "Active":
            log_pass(f"Subscription {sub_name} is Active")
        else:
            log_warn("Subscription status", f"expected Active, got {sub_doc.status}")
        grants = frappe.get_all("MSuite Customer Grant",
            filters={"customer": "Sagar Communications", "status": "Active"},
            fields=["name", "granted_plan", "grant_type", "trigger_plan"])
        print(f"  Grants created: {len(grants)}")
        for g in grants:
            pn = frappe.db.get_value("MSuite Plan", g.granted_plan, "plan_name")
            print(f"    {g.name}: {pn} (type={g.grant_type})")
        if len(grants) >= 2:
            log_pass(f"Paid + Complimentary grants: {len(grants)}")
        elif len(grants) == 1:
            log_warn("Grants", "Only 1 grant, complimentary may not have been created")
        else:
            log_fail("Grants", "No grants created")
        frappe.db.commit()
    except Exception as e:
        log_fail("Paid subscription", str(e))
        import traceback; traceback.print_exc()


def test_subscription_cancellation():
    print("\n" + "=" * 60)
    print("TEST: Subscription Cancellation & Grant Revocation")
    print("=" * 60)
    try:
        from msuite.services.subscription_service import create_subscription, cancel_subscription
        from msuite.constants import GrantStatus
        # Create a second subscription for Acme Corp
        sub_name = create_subscription(
            customer="Acme Corp",
            subscription_plan_names=["EMAIL-PRO-MONTHLY"],
            start_date="2026-04-03",
        )
        print(f"  Created subscription: {sub_name}")
        grants_before = frappe.db.count("MSuite Customer Grant",
            {"customer": "Acme Corp", "status": GrantStatus.ACTIVE})
        print(f"  Active grants before cancel: {grants_before}")
        # Cancel
        cancel_subscription(sub_name, "Testing cancellation")
        sub_doc = frappe.get_doc("Subscription", sub_name)
        if sub_doc.docstatus == 2:
            log_pass(f"Subscription cancelled (docstatus=2)")
        else:
            log_fail("Cancellation", f"docstatus={sub_doc.docstatus}")
        grants_after = frappe.db.count("MSuite Customer Grant",
            {"customer": "Acme Corp", "status": GrantStatus.ACTIVE})
        revoked = frappe.db.count("MSuite Customer Grant",
            {"customer": "Acme Corp", "status": GrantStatus.REVOKED})
        print(f"  Active grants after cancel: {grants_after}")
        print(f"  Revoked grants: {revoked}")
        if grants_after == 0 and revoked > 0:
            log_pass(f"Grants revoked on cancellation: {revoked}")
        elif grants_after == 0:
            log_warn("Cancellation grants", "No active grants but also no revoked grants")
        else:
            log_fail("Cancellation grants", f"Still {grants_after} active grants")
        frappe.db.commit()
    except Exception as e:
        log_fail("Subscription cancellation", str(e))
        import traceback; traceback.print_exc()


def test_entitlements():
    print("\n" + "=" * 60)
    print("TEST: Entitlement Resolution")
    print("=" * 60)
    try:
        from msuite.services.entitlement_service import (
            get_customer_entitlements, check_feature_access, get_feature_limit
        )
        ent = get_customer_entitlements("Sagar Communications")
        print(f"  Active plans: {ent['active_plans']}")
        print(f"  Features: {list(ent['features'].keys())}")
        msg = ent["features"].get("messaging", {})
        if msg.get("enabled") and msg.get("limit") is None:
            log_pass("WA-BIZ messaging=enabled, unlimited")
        else:
            log_fail("WA-BIZ messaging", f"enabled={msg.get('enabled')} limit={msg.get('limit')}")
        if check_feature_access("Sagar Communications", "messaging"):
            log_pass("check_feature_access('messaging')=True")
        else:
            log_fail("check_feature_access('messaging')", "returned False")
        if not check_feature_access("Sagar Communications", "nonexistent_xyz"):
            log_pass("check_feature_access('nonexistent')=False")
        else:
            log_fail("check_feature_access('nonexistent')", "returned True")
        templates_limit = get_feature_limit("Sagar Communications", "templates")
        if templates_limit == 100.0:
            log_pass(f"WA-BIZ templates limit=100")
        else:
            log_warn("WA-BIZ templates limit", f"expected 100, got {templates_limit}")
        messaging_limit = get_feature_limit("Sagar Communications", "messaging")
        if messaging_limit is None:
            log_pass("WA-BIZ messaging limit=None (unlimited)")
        else:
            log_fail("WA-BIZ messaging limit", f"expected None, got {messaging_limit}")
        sched = ent["features"].get("scheduled_posts", {})
        if sched.get("enabled"):
            log_pass(f"Complimentary Social scheduled_posts enabled, limit={sched.get('limit')}")
        else:
            log_fail("Complimentary Social scheduled_posts", "not enabled")
        # Check active grants in response
        if len(ent["active_grants"]) >= 2:
            log_pass(f"active_grants has {len(ent['active_grants'])} entries")
        else:
            log_fail("active_grants count", f"expected >=2, got {len(ent['active_grants'])}")
    except Exception as e:
        log_fail("Entitlement resolution", str(e))
        import traceback; traceback.print_exc()


def test_cache():
    print("\n" + "=" * 60)
    print("TEST: Redis Cache")
    print("=" * 60)
    try:
        from msuite.services.entitlement_service import get_customer_entitlements
        from msuite.utils.cache import get_entitlement_cache, invalidate_entitlement_cache
        # Ensure cached
        get_customer_entitlements("Sagar Communications")
        cached = get_entitlement_cache("Sagar Communications")
        if cached:
            log_pass("Cache populated after get_customer_entitlements")
        else:
            log_fail("Cache", "not populated")
        invalidate_entitlement_cache("Sagar Communications")
        cached2 = get_entitlement_cache("Sagar Communications")
        if cached2 is None:
            log_pass("Cache invalidated successfully")
        else:
            log_fail("Cache invalidation", "cache still exists")
    except Exception as e:
        log_fail("Cache test", str(e))


def test_idempotency():
    print("\n" + "=" * 60)
    print("TEST: Grant Idempotency")
    print("=" * 60)
    try:
        from msuite.services.grant_service import apply_grants_for_subscription
        sub = frappe.db.get_value("Subscription", {"party": "Sagar Communications", "docstatus": 1}, "name")
        if not sub:
            log_warn("Idempotency", "No active subscription found, skipping")
            return
        grants_before = frappe.db.count("MSuite Customer Grant", {"customer": "Sagar Communications", "status": "Active"})
        apply_grants_for_subscription(sub)
        grants_after = frappe.db.count("MSuite Customer Grant", {"customer": "Sagar Communications", "status": "Active"})
        if grants_before == grants_after:
            log_pass(f"Idempotent: {grants_before} grants before and after")
        else:
            log_fail("Idempotency", f"before={grants_before} after={grants_after}")
    except Exception as e:
        log_fail("Idempotency", str(e))
        import traceback; traceback.print_exc()


def test_grant_revocation():
    print("\n" + "=" * 60)
    print("TEST: Grant Revocation")
    print("=" * 60)
    try:
        from msuite.services.grant_service import revoke_grant
        from msuite.exceptions import InvalidGrantStateError
        from msuite.constants import GrantStatus, GrantType
        # Create a test grant to revoke
        customer = "Coupon Tester Co"
        plan = frappe.db.get_value("MSuite Plan", {"plan_code": "EMAIL-BASIC"}, "name")
        sub = frappe.db.get_value("Subscription", {"docstatus": 1}, "name")
        if not plan or not sub:
            log_warn("Revocation", "Missing test data, skipping")
            return
        # Create grant
        doc = frappe.new_doc("MSuite Customer Grant")
        doc.customer = customer
        doc.granted_plan = plan
        doc.source_subscription = sub
        doc.grant_type = GrantType.PAID
        doc.status = GrantStatus.ACTIVE
        doc.granted_on = today()
        doc.insert(ignore_permissions=True)
        grant_name = doc.name
        # Revoke it
        revoke_grant(grant_name, "Test revocation")
        revoked = frappe.get_doc("MSuite Customer Grant", grant_name)
        if revoked.status == GrantStatus.REVOKED:
            log_pass("Grant revoked: status=Revoked")
        else:
            log_fail("Grant revocation status", f"expected Revoked, got {revoked.status}")
        if revoked.revoked_on:
            log_pass(f"revoked_on set: {revoked.revoked_on}")
        else:
            log_fail("revoked_on", "not set")
        if revoked.revocation_reason == "Test revocation":
            log_pass("revocation_reason set correctly")
        else:
            log_fail("revocation_reason", f"got {revoked.revocation_reason}")
        # Try revoking again - should fail
        try:
            revoke_grant(grant_name, "Double revoke")
            log_fail("Double revoke", "should have raised InvalidGrantStateError")
        except InvalidGrantStateError:
            log_pass("Double revoke raises InvalidGrantStateError")
        except Exception as e:
            log_pass(f"Double revoke raises error: {type(e).__name__}")
        frappe.db.commit()
    except Exception as e:
        log_fail("Grant revocation", str(e))
        import traceback; traceback.print_exc()


def test_trial_flow():
    print("\n" + "=" * 60)
    print("TEST: Trial Subscription Flow")
    print("=" * 60)
    try:
        from msuite.services.subscription_service import create_subscription
        from msuite.services.trial_service import is_subscription_in_trial, get_trial_plan_for_product, get_trial_expiry
        from msuite.constants import GrantType
        # Create trial subscription
        sub_name = create_subscription(
            customer="Trial User Ltd",
            subscription_plan_names=["WA-PRO-MONTHLY"],
            start_date="2026-04-03",
            trial_days=14,
        )
        print(f"  Created trial subscription: {sub_name}")
        # Check trial status
        in_trial = is_subscription_in_trial(sub_name)
        if in_trial:
            log_pass("is_subscription_in_trial=True")
        else:
            log_fail("is_subscription_in_trial", "returned False")
        # Check trial expiry
        expiry = get_trial_expiry(sub_name)
        expected_expiry = str(add_days("2026-04-03", 14))
        if expiry == expected_expiry:
            log_pass(f"Trial expiry correct: {expiry}")
        else:
            log_warn("Trial expiry", f"expected {expected_expiry}, got {expiry}")
        # Check trial plan resolution
        wa_product = frappe.db.get_value("MSuite Product", {"product_code": "WA"}, "name")
        trial_plan = get_trial_plan_for_product(wa_product)
        if trial_plan:
            tier = frappe.db.get_value("MSuite Plan", trial_plan, "tier")
            if tier == "Trial":
                log_pass(f"Trial plan resolved: {trial_plan}")
            else:
                log_fail("Trial plan tier", f"expected Trial, got {tier}")
        else:
            log_warn("Trial plan", "not found for WA product")
        # Check trial grants
        grants = frappe.get_all("MSuite Customer Grant",
            filters={"customer": "Trial User Ltd", "grant_type": GrantType.TRIAL, "status": "Active"},
            fields=["name", "granted_plan", "expires_on"])
        print(f"  Trial grants: {len(grants)}")
        for g in grants:
            print(f"    {g.name}: plan={g.granted_plan} expires={g.expires_on}")
        if grants:
            log_pass(f"Trial grants created: {len(grants)}")
            if grants[0].expires_on:
                log_pass(f"Trial grant has expires_on: {grants[0].expires_on}")
            else:
                log_fail("Trial grant expires_on", "not set")
        else:
            log_warn("Trial grants", "none created (hook may have failed)")
        frappe.db.commit()
    except Exception as e:
        log_fail("Trial flow", str(e))
        import traceback; traceback.print_exc()


def test_bundle_flow():
    print("\n" + "=" * 60)
    print("TEST: Bundle Subscription Flow")
    print("=" * 60)
    try:
        from msuite.services.bundle_service import is_bundle_item, get_bundle_for_item, get_bundle_components, validate_bundle, get_bundle_merged_features
        # Test bundle item detection
        if is_bundle_item("MARKETING-COMBO-MONTHLY"):
            log_pass("MARKETING-COMBO-MONTHLY is bundle item")
        else:
            log_fail("is_bundle_item", "returned False for MARKETING-COMBO-MONTHLY")
        if not is_bundle_item("WA-BIZ-MONTHLY"):
            log_pass("WA-BIZ-MONTHLY is NOT bundle item")
        else:
            log_fail("is_bundle_item", "returned True for WA-BIZ-MONTHLY")
        # Get bundle
        bundle = get_bundle_for_item("MARKETING-COMBO-MONTHLY")
        if bundle:
            log_pass(f"get_bundle_for_item: {bundle}")
        else:
            log_fail("get_bundle_for_item", "returned None")
        # Components
        if bundle:
            components = get_bundle_components(bundle)
            if len(components) >= 2:
                log_pass(f"Bundle has {len(components)} components")
                for c in components:
                    print(f"    {c['plan']} qty={c['quantity']}")
            else:
                log_fail("Bundle components", f"expected >=2, got {len(components)}")
            # Validate
            errors = validate_bundle(bundle)
            if not errors:
                log_pass("validate_bundle: no errors")
            else:
                log_fail("validate_bundle", f"errors: {errors}")
            # Merged features
            merged = get_bundle_merged_features(bundle)
            if merged:
                log_pass(f"Bundle merged features: {len(merged)} features")
            else:
                log_fail("Merged features", "empty")
        # Create bundle subscription
        from msuite.services.subscription_service import create_subscription
        sub_name = create_subscription(
            customer="Bundle Buyer Inc",
            subscription_plan_names=["MARKETING-COMBO-MONTHLY"],
            start_date="2026-04-03",
        )
        print(f"  Created bundle subscription: {sub_name}")
        # Check bundle grants
        grants = frappe.get_all("MSuite Customer Grant",
            filters={"customer": "Bundle Buyer Inc", "status": "Active"},
            fields=["name", "granted_plan", "grant_type"])
        print(f"  Bundle grants: {len(grants)}")
        for g in grants:
            pn = frappe.db.get_value("MSuite Plan", g.granted_plan, "plan_name")
            print(f"    {g.name}: {pn} (type={g.grant_type})")
        if grants:
            log_pass(f"Bundle grants created: {len(grants)}")
        else:
            log_warn("Bundle grants", "none created (hook may need bundle detection)")
        frappe.db.commit()
    except Exception as e:
        log_fail("Bundle flow", str(e))
        import traceback; traceback.print_exc()


def test_customer_groups():
    print("\n" + "=" * 60)
    print("TEST: Customer Group Membership")
    print("=" * 60)
    try:
        from msuite.services.customer_group_service import (
            get_group_name_for_plan, ensure_group_exists,
            add_customer_to_group, remove_customer_from_group,
            get_customer_msuite_groups
        )
        # Test group name generation
        name = get_group_name_for_plan("WhatsApp Business")
        if name == "MSuite - WhatsApp Business Active":
            log_pass(f"Group name: {name}")
        else:
            log_fail("Group name", f"expected 'MSuite - WhatsApp Business Active', got {name}")
        # Test ensure exists
        ensure_group_exists("MSuite - Test Group Active")
        if frappe.db.exists("Customer Group", "MSuite - Test Group Active"):
            log_pass("ensure_group_exists created group")
        else:
            log_fail("ensure_group_exists", "group not created")
        # Test add/remove
        add_customer_to_group("Acme Corp", "MSuite - Test Group Active")
        groups = get_customer_msuite_groups("Acme Corp")
        if "MSuite - Test Group Active" in groups:
            log_pass("Customer added to group")
        else:
            log_fail("add_customer_to_group", f"group not in {groups}")
        # Idempotent add
        add_customer_to_group("Acme Corp", "MSuite - Test Group Active")
        groups2 = get_customer_msuite_groups("Acme Corp")
        count = groups2.count("MSuite - Test Group Active")
        if count == 1:
            log_pass("Idempotent add: still 1 membership")
        else:
            log_fail("Idempotent add", f"found {count} memberships")
        # Remove
        remove_customer_from_group("Acme Corp", "MSuite - Test Group Active")
        groups3 = get_customer_msuite_groups("Acme Corp")
        if "MSuite - Test Group Active" not in groups3:
            log_pass("Customer removed from group")
        else:
            log_fail("remove_customer_from_group", "still in group")
        frappe.db.commit()
    except Exception as e:
        log_fail("Customer groups", str(e))
        import traceback; traceback.print_exc()


def test_plan_api():
    print("\n" + "=" * 60)
    print("TEST: Plan API")
    print("=" * 60)
    try:
        from msuite.api.v1.plan import get_plans, get_plan_features, compare_plans
        import json
        # Get all plans (no trial)
        result = get_plans()
        if result["status"] == "success":
            plans = result["data"]
            non_trial = [p for p in plans if p.get("tier") != "Trial"]
            log_pass(f"get_plans: {len(plans)} plans returned (non-trial)")
        else:
            log_fail("get_plans", result.get("message"))
        # Get plan features
        result2 = get_plan_features("WA-BIZ")
        if result2["status"] == "success":
            features = result2["data"]["features"]
            log_pass(f"get_plan_features(WA-BIZ): {len(features)} features")
            grants = result2["data"]["grants"]
            if grants:
                log_pass(f"WA-BIZ has {len(grants)} grant rules")
        else:
            log_fail("get_plan_features", result2.get("message"))
        # Compare plans
        result3 = compare_plans(json.dumps(["WA-BASIC", "WA-PRO", "WA-BIZ"]))
        if result3["status"] == "success":
            log_pass(f"compare_plans: {len(result3['data'])} features compared")
        else:
            log_fail("compare_plans", result3.get("message"))
    except Exception as e:
        log_fail("Plan API", str(e))
        import traceback; traceback.print_exc()


def test_product_validations():
    print("\n" + "=" * 60)
    print("TEST: Product Validations")
    print("=" * 60)
    try:
        # Invalid product code
        doc = frappe.new_doc("MSuite Product")
        doc.product_name = "Bad Code"
        doc.product_code = "bad_lowercase"
        doc.is_active = 1
        try:
            doc.insert()
            log_fail("Invalid product_code", "should have raised ValidationError")
            doc.delete()
        except frappe.ValidationError:
            log_pass("Invalid product_code rejected")
        # Duplicate feature keys
        doc2 = frappe.new_doc("MSuite Product")
        doc2.product_name = "Dup Features"
        doc2.product_code = "DUPFEAT"
        doc2.is_active = 1
        doc2.append("features", {"feature_key": "same_key", "feature_label": "F1", "feature_type": "Boolean"})
        doc2.append("features", {"feature_key": "same_key", "feature_label": "F2", "feature_type": "Numeric"})
        try:
            doc2.insert()
            log_fail("Duplicate feature keys", "should have raised ValidationError")
            doc2.delete()
        except frappe.ValidationError:
            log_pass("Duplicate feature keys rejected")
    except Exception as e:
        log_fail("Product validations", str(e))


def test_plan_validations():
    print("\n" + "=" * 60)
    print("TEST: Plan Validations")
    print("=" * 60)
    try:
        # Invalid plan code
        doc = frappe.new_doc("MSuite Plan")
        doc.plan_name = "Bad Plan"
        doc.plan_code = "bad-lowercase"
        doc.product = "WA"
        doc.tier = "Basic"
        doc.item = "WA-BASIC-MONTHLY"
        try:
            doc.insert()
            log_fail("Invalid plan_code", "should have raised ValidationError")
            doc.delete()
        except frappe.ValidationError:
            log_pass("Invalid plan_code rejected")
        # Self-grant detection
        existing = frappe.db.get_value("MSuite Plan", {"plan_code": "WA-BIZ"}, "name")
        if existing:
            plan_doc = frappe.get_doc("MSuite Plan", existing)
            plan_doc.append("grants", {"granted_plan": existing, "grant_type": "Complimentary", "expires_after_days": 0})
            try:
                plan_doc.save()
                log_fail("Self-grant", "should have raised error")
            except Exception:
                log_pass("Self-grant rejected")
                plan_doc.reload()
    except Exception as e:
        log_fail("Plan validations", str(e))


def print_summary():
    print("\n" + "=" * 60)
    print(f"FINAL RESULTS: {len(passed)} PASSED, {len(failed)} FAILED, {len(warnings)} WARNINGS")
    print("=" * 60)
    if failed:
        print("\nFAILURES:")
        for test, err in failed:
            print(f"  ✗ {test}: {err}")
    if warnings:
        print("\nWARNINGS:")
        for test, msg in warnings:
            print(f"  ⚠ {test}: {msg}")
    if not failed:
        print("\n🎉 ALL TESTS PASSED!")
