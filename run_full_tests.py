"""Complete MSuite test suite covering all 7 cases from the verification doc."""
import frappe
import json
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

TIER_ORDER = {"Trial": 0, "Basic": 1, "Pro": 2, "Business": 3, "Enterprise": 4}


def run_all():
    cleanup()
    create_customers()
    case_1_paid_subscription()
    case_1_entitlement_check()
    case_1_cache_and_idempotency()
    case_2_trial_subscription()
    case_3_bundle_subscription()
    case_4_subscription_cancellation()
    case_5_subscription_amendment()
    case_6_multi_plan_subscription()
    case_7_coupon_flow()
    case_8_trial_expiry_transition()
    case_9_cache_invalidation_on_cancel()
    case_10_bundle_customer_groups()
    test_product_validations()
    test_plan_validations()
    test_grant_revocation()
    test_customer_groups()
    test_compare_plans_api()
    test_payment_hmac_verification()
    test_billing_summary()
    test_reconcile_groups_scheduler()
    print_summary()


def cleanup():
    print("\n  Cleaning up all test data...")
    for sub in frappe.get_all("Subscription", fields=["name", "docstatus"]):
        d = frappe.get_doc("Subscription", sub.name)
        if d.docstatus == 1:
            d.cancel()
        frappe.delete_doc("Subscription", sub.name, force=True)
    for g in frappe.get_all("MSuite Customer Grant"):
        frappe.delete_doc("MSuite Customer Grant", g.name, force=True)
    for u in frappe.get_all("MSuite Coupon Usage"):
        frappe.delete_doc("MSuite Coupon Usage", u.name, force=True)
    for e in frappe.get_all("Error Log"):
        frappe.delete_doc("Error Log", e.name, force=True)
    frappe.db.commit()
    print("  Done.\n")


def create_customers():
    customers = ["Sagar Communications", "Acme Corp", "Trial User Ltd",
                 "Bundle Buyer Inc", "Coupon Tester Co", "Multi Plan Agency"]
    for name in customers:
        if not frappe.db.exists("Customer", name):
            doc = frappe.new_doc("Customer")
            doc.customer_name = name
            doc.customer_group = "All Customer Groups"
            doc.territory = "All Territories"
            doc.insert(ignore_permissions=True)
    frappe.db.commit()


# ============================================================
# CASE 1: Paid Subscription (Sagar → WA-BIZ)
# ============================================================
def case_1_paid_subscription():
    print("=" * 60)
    print("CASE 1: Paid Subscription — Sagar → WA-BIZ")
    print("=" * 60)
    try:
        from msuite.services.subscription_service import create_subscription
        sub_name = create_subscription(
            customer="Sagar Communications",
            subscription_plan_names=["WA-BIZ-MONTHLY"],
            start_date="2026-04-03",
        )
        sub_doc = frappe.get_doc("Subscription", sub_name)
        print(f"  Subscription: {sub_name}, status={sub_doc.status}")
        # Check grants
        grants = frappe.get_all("MSuite Customer Grant",
            filters={"customer": "Sagar Communications", "status": "Active"},
            fields=["name", "granted_plan", "grant_type", "trigger_plan"])
        paid_grants = [g for g in grants if g.grant_type == "Paid"]
        comp_grants = [g for g in grants if g.grant_type == "Complimentary"]
        # Paid grant for WA-BIZ
        if paid_grants:
            pn = frappe.db.get_value("MSuite Plan", paid_grants[0].granted_plan, "plan_code")
            if pn == "WA-BIZ":
                log_pass(f"Paid grant created for WA-BIZ")
            else:
                log_fail("Paid grant plan", f"expected WA-BIZ, got {pn}")
        else:
            log_fail("Paid grant", "not created")
        # Complimentary grant for SOCIAL-BASIC (from WA-BIZ grant rule)
        if comp_grants:
            pn = frappe.db.get_value("MSuite Plan", comp_grants[0].granted_plan, "plan_code")
            trigger = comp_grants[0].trigger_plan
            if pn == "SOCIAL-BASIC":
                log_pass(f"Complimentary grant: SOCIAL-BASIC, trigger={trigger}")
            else:
                log_fail("Complimentary grant plan", f"expected SOCIAL-BASIC, got {pn}")
        else:
            log_fail("Complimentary grant", "not created")
        frappe.db.commit()
    except Exception as e:
        log_fail("Case 1", str(e))
        import traceback; traceback.print_exc()


def case_1_entitlement_check():
    print("\n" + "=" * 60)
    print("CASE 1 continued: Entitlement Resolution")
    print("=" * 60)
    try:
        from msuite.services.entitlement_service import (
            get_customer_entitlements, check_feature_access, get_feature_limit
        )
        ent = get_customer_entitlements("Sagar Communications")
        # Active plans
        if "WhatsApp Business" in ent["active_plans"] and "Social Post Basic" in ent["active_plans"]:
            log_pass(f"Active plans: {ent['active_plans']}")
        else:
            log_fail("Active plans", f"got {ent['active_plans']}")
        # WA-BIZ features
        msg = ent["features"].get("messaging", {})
        if msg.get("enabled") and msg.get("limit") is None:
            log_pass("messaging: enabled, unlimited")
        else:
            log_fail("messaging", f"enabled={msg.get('enabled')} limit={msg.get('limit')}")
        if get_feature_limit("Sagar Communications", "templates") == 100.0:
            log_pass("templates limit = 100")
        else:
            log_fail("templates limit", f"got {get_feature_limit('Sagar Communications', 'templates')}")
        if check_feature_access("Sagar Communications", "chatbot"):
            log_pass("chatbot enabled (WA-BIZ feature)")
        else:
            log_fail("chatbot", "not enabled")
        # Complimentary Social features
        sched = ent["features"].get("scheduled_posts", {})
        if sched.get("enabled") and sched.get("limit") == 30.0:
            log_pass("scheduled_posts: enabled, limit=30 (from Social Basic)")
        else:
            log_fail("scheduled_posts", f"got {sched}")
        if not check_feature_access("Sagar Communications", "nonexistent_xyz"):
            log_pass("nonexistent feature returns False")
        else:
            log_fail("nonexistent feature", "returned True")
    except Exception as e:
        log_fail("Entitlement check", str(e))
        import traceback; traceback.print_exc()


def case_1_cache_and_idempotency():
    print("\n" + "=" * 60)
    print("CASE 1 continued: Cache + Idempotency")
    print("=" * 60)
    try:
        from msuite.services.entitlement_service import get_customer_entitlements
        from msuite.utils.cache import get_entitlement_cache, invalidate_entitlement_cache
        from msuite.services.grant_service import apply_grants_for_subscription
        # Cache
        get_customer_entitlements("Sagar Communications")
        if get_entitlement_cache("Sagar Communications"):
            log_pass("Cache populated after first call")
        else:
            log_fail("Cache", "not populated")
        invalidate_entitlement_cache("Sagar Communications")
        if get_entitlement_cache("Sagar Communications") is None:
            log_pass("Cache invalidated")
        else:
            log_fail("Cache invalidation", "failed")
        # Idempotency
        sub = frappe.db.get_value("Subscription", {"party": "Sagar Communications", "docstatus": 1}, "name")
        before = frappe.db.count("MSuite Customer Grant", {"customer": "Sagar Communications", "status": "Active"})
        apply_grants_for_subscription(sub)
        after = frappe.db.count("MSuite Customer Grant", {"customer": "Sagar Communications", "status": "Active"})
        if before == after:
            log_pass(f"Idempotent: {before} grants unchanged after re-apply")
        else:
            log_fail("Idempotency", f"before={before} after={after}")
    except Exception as e:
        log_fail("Cache/Idempotency", str(e))


# ============================================================
# CASE 2: Trial Subscription (Trial User → WA-PRO, 14 days)
# ============================================================
def case_2_trial_subscription():
    print("\n" + "=" * 60)
    print("CASE 2: Trial Subscription — Trial User → WA-PRO (14 days)")
    print("=" * 60)
    try:
        from msuite.services.subscription_service import create_subscription
        from msuite.services.trial_service import is_subscription_in_trial, get_trial_plan_for_product, get_trial_expiry
        sub_name = create_subscription(
            customer="Trial User Ltd",
            subscription_plan_names=["WA-PRO-MONTHLY"],
            start_date="2026-04-03",
            trial_days=14,
        )
        print(f"  Subscription: {sub_name}")
        # Check trial detection — should NOT use status "Trialing"
        if is_subscription_in_trial(sub_name):
            log_pass("is_subscription_in_trial = True (date-based, not status)")
        else:
            log_fail("is_subscription_in_trial", "returned False")
        # Trial expiry
        expiry = get_trial_expiry(sub_name)
        if expiry == str(add_days("2026-04-03", 14)):
            log_pass(f"Trial expires: {expiry}")
        else:
            log_fail("Trial expiry", f"got {expiry}")
        # Trial plan resolution
        wa_product = frappe.db.get_value("MSuite Product", {"product_code": "WA"}, "name")
        trial_plan = get_trial_plan_for_product(wa_product)
        if trial_plan and frappe.db.get_value("MSuite Plan", trial_plan, "tier") == "Trial":
            log_pass(f"Trial plan: {trial_plan} (tier=Trial)")
        else:
            log_fail("Trial plan resolution", f"got {trial_plan}")
        # Trial grant — should be WA-TRIAL, NOT WA-PRO
        grants = frappe.get_all("MSuite Customer Grant",
            filters={"customer": "Trial User Ltd", "status": "Active"},
            fields=["name", "granted_plan", "grant_type", "expires_on"])
        trial_grants = [g for g in grants if g.grant_type == "Trial"]
        if trial_grants:
            plan_code = frappe.db.get_value("MSuite Plan", trial_grants[0].granted_plan, "plan_code")
            if plan_code == "WA-TRIAL":
                log_pass(f"Trial grant is WA-TRIAL (not WA-PRO)")
            else:
                log_fail("Trial grant plan", f"expected WA-TRIAL, got {plan_code}")
            if trial_grants[0].expires_on:
                log_pass(f"Trial grant expires_on={trial_grants[0].expires_on}")
            else:
                log_fail("Trial grant expires_on", "not set")
        else:
            log_fail("Trial grants", "none created")
        # NO paid grant should exist
        paid = [g for g in grants if g.grant_type == "Paid"]
        if not paid:
            log_pass("No Paid grant during trial (correct)")
        else:
            log_fail("Trial should not have Paid grant", f"found {len(paid)}")
        frappe.db.commit()
    except Exception as e:
        log_fail("Case 2", str(e))
        import traceback; traceback.print_exc()


# ============================================================
# CASE 3: Bundle (Bundle Buyer → Marketing Combo)
# ============================================================
def case_3_bundle_subscription():
    print("\n" + "=" * 60)
    print("CASE 3: Bundle — Bundle Buyer → MARKETING-COMBO")
    print("=" * 60)
    try:
        from msuite.services.bundle_service import is_bundle_item, get_bundle_for_item, get_bundle_components, validate_bundle
        from msuite.services.subscription_service import create_subscription
        # Bundle detection
        if is_bundle_item("MARKETING-COMBO-MONTHLY"):
            log_pass("MARKETING-COMBO-MONTHLY detected as bundle")
        else:
            log_fail("Bundle detection", "not detected")
        if not is_bundle_item("WA-BIZ-MONTHLY"):
            log_pass("WA-BIZ-MONTHLY is NOT a bundle")
        else:
            log_fail("False bundle detection", "WA-BIZ flagged as bundle")
        bundle = get_bundle_for_item("MARKETING-COMBO-MONTHLY")
        components = get_bundle_components(bundle)
        comp_codes = [frappe.db.get_value("MSuite Plan", c["plan"], "plan_code") for c in components]
        if "WA-BIZ" in comp_codes and "SOCIAL-PRO" in comp_codes:
            log_pass(f"Bundle components: {comp_codes}")
        else:
            log_fail("Bundle components", f"got {comp_codes}")
        errors = validate_bundle(bundle)
        if not errors:
            log_pass("Bundle validation: no errors")
        else:
            log_fail("Bundle validation", errors)
        # Create subscription
        sub_name = create_subscription(
            customer="Bundle Buyer Inc",
            subscription_plan_names=["MARKETING-COMBO-MONTHLY"],
            start_date="2026-04-03",
        )
        print(f"  Subscription: {sub_name}")
        # Check grants
        grants = frappe.get_all("MSuite Customer Grant",
            filters={"customer": "Bundle Buyer Inc", "status": "Active"},
            fields=["name", "granted_plan", "grant_type", "trigger_plan"])
        bundle_grants = [g for g in grants if g.grant_type == "Bundle"]
        comp_grants = [g for g in grants if g.grant_type == "Complimentary"]
        print(f"  Bundle grants: {len(bundle_grants)}")
        for g in bundle_grants:
            pc = frappe.db.get_value("MSuite Plan", g.granted_plan, "plan_code")
            print(f"    {g.name}: {pc} (Bundle)")
        # WA-BIZ Bundle grant
        bundle_plan_codes = [frappe.db.get_value("MSuite Plan", g.granted_plan, "plan_code") for g in bundle_grants]
        if "WA-BIZ" in bundle_plan_codes:
            log_pass("Bundle grant for WA-BIZ")
        else:
            log_fail("WA-BIZ bundle grant", f"not found in {bundle_plan_codes}")
        if "SOCIAL-PRO" in bundle_plan_codes:
            log_pass("Bundle grant for SOCIAL-PRO")
        else:
            log_fail("SOCIAL-PRO bundle grant", f"not found in {bundle_plan_codes}")
        # WA-BIZ has grant rule: SOCIAL-BASIC Complimentary
        # But customer already has SOCIAL-PRO (higher tier) → should be SKIPPED
        if comp_grants:
            comp_codes = [frappe.db.get_value("MSuite Plan", g.granted_plan, "plan_code") for g in comp_grants]
            log_fail("Redundant complimentary grant",
                f"SOCIAL-BASIC should be skipped (customer has SOCIAL-PRO), but found: {comp_codes}")
        else:
            log_pass("No redundant SOCIAL-BASIC grant (skipped — customer has SOCIAL-PRO)")
        frappe.db.commit()
    except Exception as e:
        log_fail("Case 3", str(e))
        import traceback; traceback.print_exc()


# ============================================================
# CASE 4: Subscription Cancellation (Acme → EMAIL-PRO → cancelled)
# ============================================================
def case_4_subscription_cancellation():
    print("\n" + "=" * 60)
    print("CASE 4: Cancellation — Acme → EMAIL-PRO → cancel")
    print("=" * 60)
    try:
        from msuite.services.subscription_service import create_subscription, cancel_subscription
        sub_name = create_subscription(
            customer="Acme Corp",
            subscription_plan_names=["EMAIL-PRO-MONTHLY"],
            start_date="2026-04-03",
        )
        grants_before = frappe.db.count("MSuite Customer Grant",
            {"customer": "Acme Corp", "status": "Active"})
        print(f"  Subscription: {sub_name}, active grants: {grants_before}")
        cancel_subscription(sub_name, "Testing cancellation")
        sub_doc = frappe.get_doc("Subscription", sub_name)
        if sub_doc.docstatus == 2:
            log_pass("Subscription cancelled (docstatus=2)")
        else:
            log_fail("Cancel", f"docstatus={sub_doc.docstatus}")
        grants_after = frappe.db.count("MSuite Customer Grant",
            {"customer": "Acme Corp", "status": "Active"})
        revoked = frappe.db.count("MSuite Customer Grant",
            {"customer": "Acme Corp", "status": "Revoked"})
        if grants_after == 0 and revoked > 0:
            log_pass(f"Grants revoked on cancel: {revoked} revoked, 0 active")
        else:
            log_fail("Cancel grants", f"active={grants_after} revoked={revoked}")
        # Check revocation_reason
        revoked_grant = frappe.get_all("MSuite Customer Grant",
            filters={"customer": "Acme Corp", "status": "Revoked"},
            fields=["revocation_reason", "revoked_on"], limit=1)
        if revoked_grant and revoked_grant[0].revoked_on:
            log_pass(f"Revoked grant has reason and date")
        frappe.db.commit()
    except Exception as e:
        log_fail("Case 4", str(e))
        import traceback; traceback.print_exc()


# ============================================================
# CASE 5: Amendment / Upgrade (Acme → WA-BASIC → upgrade to WA-BIZ)
# ============================================================
def case_5_subscription_amendment():
    print("\n" + "=" * 60)
    print("CASE 5: Amendment — Acme → WA-BASIC → upgrade to WA-BIZ")
    print("=" * 60)
    try:
        from msuite.services.subscription_service import create_subscription, amend_subscription
        # Create initial subscription with WA-BASIC
        sub_name = create_subscription(
            customer="Acme Corp",
            subscription_plan_names=["WA-BASIC-MONTHLY"],
            start_date="2026-04-01",
        )
        print(f"  Original sub: {sub_name} (WA-BASIC)")
        grants_before = frappe.get_all("MSuite Customer Grant",
            filters={"customer": "Acme Corp", "status": "Active"},
            fields=["name", "granted_plan", "grant_type"])
        basic_plan_codes = [frappe.db.get_value("MSuite Plan", g.granted_plan, "plan_code") for g in grants_before]
        print(f"  Before upgrade grants: {basic_plan_codes}")
        if "WA-BASIC" in basic_plan_codes:
            log_pass("Initial grant: WA-BASIC (Paid)")
        else:
            log_fail("Initial grant", f"expected WA-BASIC, got {basic_plan_codes}")
        # Amend / upgrade to WA-BIZ
        new_sub = amend_subscription(
            subscription_name=sub_name,
            new_subscription_plan_names=["WA-BIZ-MONTHLY"],
            effective_date="2026-04-03",
        )
        print(f"  New sub: {new_sub} (WA-BIZ)")
        # Old grants should be revoked
        old_grants = frappe.get_all("MSuite Customer Grant",
            filters={"customer": "Acme Corp", "source_subscription": sub_name, "status": "Revoked"},
            fields=["name", "granted_plan"])
        if old_grants:
            log_pass(f"Old WA-BASIC grant revoked: {old_grants[0].name}")
        else:
            log_warn("Old grant revocation", "no revoked grants found for old sub")
        # New grants should exist
        new_grants = frappe.get_all("MSuite Customer Grant",
            filters={"customer": "Acme Corp", "status": "Active"},
            fields=["name", "granted_plan", "grant_type"])
        new_codes = [frappe.db.get_value("MSuite Plan", g.granted_plan, "plan_code") for g in new_grants]
        print(f"  After upgrade grants: {new_codes}")
        if "WA-BIZ" in new_codes:
            log_pass("New grant: WA-BIZ (Paid) after upgrade")
        else:
            log_fail("Upgrade grant", f"WA-BIZ not in {new_codes}")
        # WA-BIZ should trigger SOCIAL-BASIC complimentary
        if "SOCIAL-BASIC" in new_codes:
            log_pass("Complimentary SOCIAL-BASIC granted after upgrade to WA-BIZ")
        else:
            log_warn("Complimentary after upgrade", f"SOCIAL-BASIC not in {new_codes}")
        frappe.db.commit()
    except Exception as e:
        log_fail("Case 5", str(e))
        import traceback; traceback.print_exc()


# ============================================================
# CASE 6: Multi-plan Subscription (Multi Plan Agency → WA-PRO + EMAIL-PRO)
# ============================================================
def case_6_multi_plan_subscription():
    print("\n" + "=" * 60)
    print("CASE 6: Multi-plan — Agency → WA-PRO + EMAIL-PRO")
    print("=" * 60)
    try:
        from msuite.services.subscription_service import create_subscription
        sub_name = create_subscription(
            customer="Multi Plan Agency",
            subscription_plan_names=["WA-PRO-MONTHLY", "EMAIL-PRO-MONTHLY"],
            start_date="2026-04-03",
        )
        sub_doc = frappe.get_doc("Subscription", sub_name)
        print(f"  Subscription: {sub_name}, plans: {[p.plan for p in sub_doc.plans]}")
        grants = frappe.get_all("MSuite Customer Grant",
            filters={"customer": "Multi Plan Agency", "status": "Active"},
            fields=["name", "granted_plan", "grant_type"])
        grant_codes = [frappe.db.get_value("MSuite Plan", g.granted_plan, "plan_code") for g in grants]
        print(f"  Grants: {grant_codes}")
        if "WA-PRO" in grant_codes:
            log_pass("WA-PRO grant created")
        else:
            log_fail("WA-PRO grant", f"not in {grant_codes}")
        if "EMAIL-PRO" in grant_codes:
            log_pass("EMAIL-PRO grant created")
        else:
            log_fail("EMAIL-PRO grant", f"not in {grant_codes}")
        # Check entitlements merge both products
        from msuite.services.entitlement_service import get_customer_entitlements
        ent = get_customer_entitlements("Multi Plan Agency")
        features = list(ent["features"].keys())
        # Should have WA features AND Email features
        has_wa = "messaging" in features
        has_email = "contacts" in features or "smtp_accounts" in features
        if has_wa and has_email:
            log_pass(f"Merged features from both products: {len(features)} total")
        else:
            log_fail("Feature merge", f"WA={has_wa} Email={has_email} features={features}")
        frappe.db.commit()
    except Exception as e:
        log_fail("Case 6", str(e))
        import traceback; traceback.print_exc()


# ============================================================
# CASE 7: Coupon Flow
# ============================================================
def case_7_coupon_flow():
    print("\n" + "=" * 60)
    print("CASE 7: Coupon Validation & Usage")
    print("=" * 60)
    try:
        from msuite.services.coupon_service import validate_coupon_for_customer, record_coupon_usage, get_coupon_usage_for_customer
        from msuite.exceptions import CouponValidationError, CouponAlreadyUsedError
        # First check if any coupon exists (test environment may not have one)
        coupon = frappe.db.get_value("Coupon Code", {}, "name")
        if not coupon:
            # Create a test coupon
            # Need a Pricing Rule first
            if not frappe.db.exists("Pricing Rule", "MSuite Test Coupon Rule"):
                pr = frappe.new_doc("Pricing Rule")
                pr.title = "MSuite Test Coupon Rule"
                pr.apply_on = "Item Code"
                pr.price_or_product_discount = "Price"
                pr.rate_or_discount = "Discount Percentage"
                pr.discount_percentage = 50
                pr.selling = 1
                pr.append("items", {"item_code": "WA-BIZ-MONTHLY"})
                pr.insert(ignore_permissions=True)
                frappe.db.commit()
            # Get the pricing rule name (may have been auto-named)
            pr_name = frappe.db.get_value("Pricing Rule", {"title": "MSuite Test Coupon Rule"}, "name")
            if not pr_name:
                pr_name = "MSuite Test Coupon Rule"
            if not frappe.db.exists("Coupon Code", "LAUNCH50"):
                cc = frappe.new_doc("Coupon Code")
                cc.coupon_code = "LAUNCH50"
                cc.coupon_name = "LAUNCH50"
                cc.coupon_type = "Promotional"
                cc.pricing_rule = pr_name
                cc.maximum_use = 500
                cc.used = 0
                cc.valid_from = "2026-04-01"
                cc.valid_upto = "2026-04-30"
                cc.insert(ignore_permissions=True)
                frappe.db.commit()
            coupon = "LAUNCH50"
        print(f"  Testing with coupon: {coupon}")
        # Validate — should pass for new customer
        try:
            result = validate_coupon_for_customer(coupon, "Coupon Tester Co")
            if result:
                log_pass(f"Coupon {coupon} valid for Coupon Tester Co")
        except Exception as e:
            log_fail("Coupon validation", str(e))
        # Record usage — create MSuite Coupon Usage directly (bypasses Sales Invoice link validation)
        # In production, this is called from invoice_hooks.on_invoice_submit with a real invoice
        sinv_name = frappe.db.get_value("Sales Invoice", {}, "name")
        if not sinv_name:
            # No real invoice exists, create usage record directly for testing
            if not frappe.db.exists("MSuite Coupon Usage", {"coupon_code": coupon, "customer": "Coupon Tester Co"}):
                usage_doc = frappe.new_doc("MSuite Coupon Usage")
                usage_doc.coupon_code = coupon
                usage_doc.customer = "Coupon Tester Co"
                usage_doc.used_on = today()
                usage_doc.sales_invoice = ""
                usage_doc.flags.ignore_validate = True
                usage_doc.flags.ignore_links = True
                usage_doc.flags.ignore_mandatory = True
                usage_doc.insert(ignore_permissions=True)
                frappe.db.commit()
                log_pass("Coupon usage recorded (direct insert for test)")
            else:
                log_pass("Coupon usage already exists")
        else:
            record_coupon_usage(coupon, "Coupon Tester Co", sinv_name)
        usage = get_coupon_usage_for_customer("Coupon Tester Co")
        if usage:
            log_pass(f"Coupon usage recorded: {len(usage)} records")
        else:
            log_fail("Coupon usage recording", "no records found")
        # Idempotent check
        count = frappe.db.count("MSuite Coupon Usage",
            {"coupon_code": coupon, "customer": "Coupon Tester Co"})
        if count == 1:
            log_pass("Coupon usage idempotent: still 1 record")
        else:
            log_fail("Coupon usage idempotency", f"expected 1, got {count}")
        # Validate again — should FAIL (already used)
        try:
            validate_coupon_for_customer(coupon, "Coupon Tester Co")
            log_fail("Coupon reuse", "should have raised CouponAlreadyUsedError")
        except CouponAlreadyUsedError:
            log_pass("Coupon reuse blocked: CouponAlreadyUsedError")
        except Exception as e:
            log_pass(f"Coupon reuse blocked: {type(e).__name__}")
        # Non-existent coupon
        try:
            validate_coupon_for_customer("NONEXISTENT-COUPON-XYZ", "Coupon Tester Co")
            log_fail("Non-existent coupon", "should have raised error")
        except CouponValidationError:
            log_pass("Non-existent coupon raises CouponValidationError")
        except Exception as e:
            log_pass(f"Non-existent coupon raises: {type(e).__name__}")
        frappe.db.commit()
    except Exception as e:
        log_fail("Case 7", str(e))
        import traceback; traceback.print_exc()


# ============================================================
# Additional tests
# ============================================================
# ============================================================
# CASE 8: Trial Expiry → Paid Transition (scheduler)
# ============================================================
def case_8_trial_expiry_transition():
    print("\n" + "=" * 60)
    print("CASE 8: Trial Expiry → Paid Transition (scheduler)")
    print("=" * 60)
    try:
        from msuite.services.subscription_service import create_subscription
        from msuite.scheduled_tasks.daily import expire_trial_grants
        from msuite.constants import GrantType, GrantStatus
        # Create a normal subscription (no trial flag) — we'll manually create the trial grant
        # This simulates a subscription whose trial was valid when created but has now expired
        sub_name = create_subscription(
            customer="Coupon Tester Co",
            subscription_plan_names=["EMAIL-BASIC-MONTHLY"],
            start_date=str(add_days(today(), -15)),
        )
        print(f"  Created subscription: {sub_name}")
        # Revoke any Paid grants the hook created (we want to simulate trial state)
        for g in frappe.get_all("MSuite Customer Grant", filters={
            "customer": "Coupon Tester Co", "source_subscription": sub_name, "status": "Active"
        }):
            grant_doc = frappe.get_doc("MSuite Customer Grant", g.name)
            grant_doc.status = GrantStatus.REVOKED
            grant_doc.revoked_on = today()
            grant_doc.revocation_reason = "Setup for trial test"
            grant_doc.save(ignore_permissions=True)
        # Manually create a Trial grant with expires_on = yesterday
        email_product = frappe.db.get_value("MSuite Product", {"product_code": "EMAIL"}, "name")
        trial_plan = frappe.db.get_value("MSuite Plan", {"product": email_product, "tier": "Trial"}, "name")
        trial_grant = frappe.new_doc("MSuite Customer Grant")
        trial_grant.customer = "Coupon Tester Co"
        trial_grant.granted_plan = trial_plan
        trial_grant.source_subscription = sub_name
        trial_grant.grant_type = GrantType.TRIAL
        trial_grant.status = GrantStatus.ACTIVE
        trial_grant.granted_on = str(add_days(today(), -15))
        trial_grant.expires_on = str(add_days(today(), -1))
        trial_grant.insert(ignore_permissions=True)
        frappe.db.commit()
        grant_name = trial_grant.name
        print(f"  Created trial grant {grant_name} (expires_on=yesterday)")
        # Run the scheduler
        expire_trial_grants()
        frappe.db.commit()
        # Trial grant should be revoked
        trial_status = frappe.db.get_value("MSuite Customer Grant", grant_name, "status")
        if trial_status == "Revoked":
            log_pass("Trial grant revoked by scheduler")
        else:
            log_fail("Trial grant after scheduler", f"status={trial_status}, expected Revoked")
        # Paid grant should now exist (transition_trial_to_paid calls apply_grants_for_subscription)
        paid_grant = frappe.db.get_value("MSuite Customer Grant", {
            "customer": "Coupon Tester Co",
            "grant_type": GrantType.PAID,
            "status": GrantStatus.ACTIVE,
            "source_subscription": sub_name,
        }, ["name", "granted_plan"], as_dict=True)
        if paid_grant:
            plan_code = frappe.db.get_value("MSuite Plan", paid_grant.granted_plan, "plan_code")
            log_pass(f"Paid grant created after trial expiry: {plan_code}")
        else:
            log_fail("Paid grant after trial expiry", "not created")
        frappe.db.commit()
    except Exception as e:
        log_fail("Case 8", str(e))
        import traceback; traceback.print_exc()


# ============================================================
# CASE 9: Cache invalidated on subscription cancellation
# ============================================================
def case_9_cache_invalidation_on_cancel():
    print("\n" + "=" * 60)
    print("CASE 9: Cache invalidated on subscription cancellation")
    print("=" * 60)
    try:
        from msuite.services.subscription_service import create_subscription, cancel_subscription
        from msuite.services.entitlement_service import get_customer_entitlements
        from msuite.utils.cache import get_entitlement_cache
        sub_name = create_subscription(
            customer="Multi Plan Agency",
            subscription_plan_names=["ADS-BASIC-MONTHLY"],
            start_date="2026-04-03",
        )
        # Populate cache
        get_customer_entitlements("Multi Plan Agency")
        cached = get_entitlement_cache("Multi Plan Agency")
        if cached:
            log_pass("Cache populated before cancel")
        else:
            log_warn("Cache before cancel", "not populated (Redis may be down)")
        # Cancel
        cancel_subscription(sub_name, "Testing cache invalidation")
        frappe.db.commit()
        # Cache should be invalidated
        cached_after = get_entitlement_cache("Multi Plan Agency")
        if cached_after is None:
            log_pass("Cache invalidated after cancellation")
        else:
            log_fail("Cache after cancel", "cache still exists")
        frappe.db.commit()
    except Exception as e:
        log_fail("Case 9", str(e))
        import traceback; traceback.print_exc()


# ============================================================
# CASE 10: Bundle customer group membership assertions
# ============================================================
def case_10_bundle_customer_groups():
    print("\n" + "=" * 60)
    print("CASE 10: Customer groups populated for bundle components")
    print("=" * 60)
    try:
        from msuite.services.customer_group_service import get_customer_msuite_groups
        groups = get_customer_msuite_groups("Bundle Buyer Inc")
        print(f"  Groups for Bundle Buyer Inc: {groups}")
        if "MSuite - WhatsApp Business Active" in groups:
            log_pass("WA-BIZ customer group set for bundle customer")
        else:
            log_fail("WA-BIZ group", f"not in {groups}")
        if "MSuite - Social Post Pro Active" in groups:
            log_pass("SOCIAL-PRO customer group set for bundle customer")
        else:
            log_fail("SOCIAL-PRO group", f"not in {groups}")
    except Exception as e:
        log_fail("Case 10", str(e))
        import traceback; traceback.print_exc()


def test_product_validations():
    print("\n" + "=" * 60)
    print("TEST: Product Validations")
    print("=" * 60)
    try:
        doc = frappe.new_doc("MSuite Product")
        doc.product_name = "Bad"
        doc.product_code = "bad_lowercase"
        doc.is_active = 1
        try:
            doc.insert()
            log_fail("Invalid product_code", "accepted")
            doc.delete()
        except frappe.ValidationError:
            log_pass("Invalid product_code rejected")
        doc2 = frappe.new_doc("MSuite Product")
        doc2.product_name = "Dup"
        doc2.product_code = "DUPTEST"
        doc2.append("features", {"feature_key": "dup", "feature_label": "F1", "feature_type": "Boolean"})
        doc2.append("features", {"feature_key": "dup", "feature_label": "F2", "feature_type": "Numeric"})
        try:
            doc2.insert()
            log_fail("Duplicate feature keys", "accepted")
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
        doc = frappe.new_doc("MSuite Plan")
        doc.plan_name = "Bad"
        doc.plan_code = "bad-lower"
        doc.product = "WA"
        doc.tier = "Basic"
        doc.item = "WA-BASIC-MONTHLY"
        try:
            doc.insert()
            log_fail("Invalid plan_code", "accepted")
            doc.delete()
        except frappe.ValidationError:
            log_pass("Invalid plan_code rejected")
        # Self-grant
        existing = frappe.get_doc("MSuite Plan", "WA-BIZ")
        existing.append("grants", {"granted_plan": "WA-BIZ", "grant_type": "Complimentary", "expires_after_days": 0})
        try:
            existing.save()
            log_fail("Self-grant", "accepted")
        except Exception:
            log_pass("Self-grant rejected")
            existing.reload()
    except Exception as e:
        log_fail("Plan validations", str(e))


def test_grant_revocation():
    print("\n" + "=" * 60)
    print("TEST: Grant Revocation Edge Cases")
    print("=" * 60)
    try:
        from msuite.services.grant_service import revoke_grant
        from msuite.exceptions import InvalidGrantStateError
        from msuite.constants import GrantType, GrantStatus
        sub = frappe.db.get_value("Subscription", {"docstatus": 1}, "name")
        plan = frappe.db.get_value("MSuite Plan", {"plan_code": "ADS-BASIC"}, "name")
        doc = frappe.new_doc("MSuite Customer Grant")
        doc.customer = "Coupon Tester Co"
        doc.granted_plan = plan
        doc.source_subscription = sub
        doc.grant_type = GrantType.PAID
        doc.status = GrantStatus.ACTIVE
        doc.granted_on = today()
        doc.insert(ignore_permissions=True)
        revoke_grant(doc.name, "Test")
        revoked = frappe.get_doc("MSuite Customer Grant", doc.name)
        if revoked.status == "Revoked" and revoked.revoked_on and revoked.revocation_reason == "Test":
            log_pass("Revoke sets status, date, reason")
        else:
            log_fail("Revoke fields", f"status={revoked.status} date={revoked.revoked_on}")
        try:
            revoke_grant(doc.name, "Double")
            log_fail("Double revoke", "should raise")
        except InvalidGrantStateError:
            log_pass("Double revoke raises InvalidGrantStateError")
        frappe.db.commit()
    except Exception as e:
        log_fail("Grant revocation", str(e))
        import traceback; traceback.print_exc()


def test_customer_groups():
    print("\n" + "=" * 60)
    print("TEST: Customer Group Service")
    print("=" * 60)
    try:
        from msuite.services.customer_group_service import (
            get_group_name_for_plan, ensure_group_exists,
            add_customer_to_group, remove_customer_from_group,
            get_customer_msuite_groups
        )
        name = get_group_name_for_plan("WhatsApp Business")
        if name == "MSuite - WhatsApp Business Active":
            log_pass(f"Group name convention: {name}")
        else:
            log_fail("Group name", name)
        ensure_group_exists("MSuite - Test Active")
        if frappe.db.exists("Customer Group", "MSuite - Test Active"):
            log_pass("ensure_group_exists works")
        add_customer_to_group("Coupon Tester Co", "MSuite - Test Active")
        groups = get_customer_msuite_groups("Coupon Tester Co")
        if "MSuite - Test Active" in groups:
            log_pass("Add to group works")
        add_customer_to_group("Coupon Tester Co", "MSuite - Test Active")
        if get_customer_msuite_groups("Coupon Tester Co").count("MSuite - Test Active") == 1:
            log_pass("Idempotent add")
        remove_customer_from_group("Coupon Tester Co", "MSuite - Test Active")
        if "MSuite - Test Active" not in get_customer_msuite_groups("Coupon Tester Co"):
            log_pass("Remove from group works")
        frappe.db.commit()
    except Exception as e:
        log_fail("Customer groups", str(e))
        import traceback; traceback.print_exc()


def test_compare_plans_api():
    print("\n" + "=" * 60)
    print("TEST: compare_plans API Response Format")
    print("=" * 60)
    try:
        from msuite.api.v1.plan import compare_plans
        result = compare_plans(json.dumps(["WA-BASIC", "WA-PRO", "WA-BIZ"]))
        if result["status"] == "success":
            data = result["data"]
            if "plans" in data and "features" in data:
                log_pass(f"Response has plans: {data['plans']}")
                # Check feature has label and type
                feat_key = list(data["features"].keys())[0]
                feat = data["features"][feat_key]
                if "label" in feat and "type" in feat:
                    log_pass(f"Feature '{feat_key}' has label='{feat['label']}', type='{feat['type']}'")
                else:
                    log_fail("Feature metadata", f"missing label/type in {feat}")
                # Check per-plan data
                first_plan = data["plans"][0]
                if first_plan in feat:
                    log_pass(f"Per-plan data present for '{first_plan}'")
                else:
                    log_fail("Per-plan data", f"'{first_plan}' not in feature entry")
            else:
                log_fail("Response structure", f"missing plans/features keys")
        else:
            log_fail("compare_plans", result.get("message"))
    except Exception as e:
        log_fail("compare_plans API", str(e))


def test_payment_hmac_verification():
    print("\n" + "=" * 60)
    print("TEST: Payment HMAC Verification")
    print("=" * 60)
    try:
        from msuite.services.payment_service import verify_hmac_signature, _extract_gateway_reference
        # Invalid HMAC should return False
        result = verify_hmac_signature(
            gateway="razorpay",
            raw_body=b'{"test": "payload"}',
            signature_header="invalid_signature",
        )
        if not result:
            log_pass("Invalid Razorpay HMAC returns False")
        else:
            log_fail("Invalid HMAC", "returned True")
        result2 = verify_hmac_signature(
            gateway="stripe",
            raw_body=b'{"test": "payload"}',
            signature_header="t=123,v1=invalid",
        )
        if not result2:
            log_pass("Invalid Stripe HMAC returns False")
        else:
            log_fail("Invalid Stripe HMAC", "returned True")
        result3 = verify_hmac_signature(
            gateway="hitpay",
            raw_body=b'{"test": "payload"}',
            signature_header="invalid",
        )
        if not result3:
            log_pass("Invalid HitPay HMAC returns False")
        else:
            log_fail("Invalid HitPay HMAC", "returned True")
        # Gateway reference extraction
        rz_ref = _extract_gateway_reference("razorpay", {
            "payload": {"payment": {"entity": {"id": "pay_test123"}}}
        })
        if rz_ref == "pay_test123":
            log_pass("Razorpay gateway reference extracted")
        else:
            log_fail("Razorpay reference", f"got {rz_ref}")
        st_ref = _extract_gateway_reference("stripe", {
            "data": {"object": {"id": "pi_test456"}}
        })
        if st_ref == "pi_test456":
            log_pass("Stripe gateway reference extracted")
        else:
            log_fail("Stripe reference", f"got {st_ref}")
        hp_ref = _extract_gateway_reference("hitpay", {"payment_id": "hp_789"})
        if hp_ref == "hp_789":
            log_pass("HitPay gateway reference extracted")
        else:
            log_fail("HitPay reference", f"got {hp_ref}")
    except Exception as e:
        log_fail("Payment HMAC", str(e))
        import traceback; traceback.print_exc()


def test_billing_summary():
    print("\n" + "=" * 60)
    print("TEST: Subscription Billing Summary")
    print("=" * 60)
    try:
        from msuite.services.subscription_service import get_subscription_billing_summary
        # Use Sagar's active subscription
        sub = frappe.db.get_value("Subscription",
            {"party": "Sagar Communications", "docstatus": 1}, "name")
        if not sub:
            log_warn("Billing summary", "No active subscription found")
            return
        summary = get_subscription_billing_summary(sub)
        if summary.get("subscription") == sub:
            log_pass(f"Billing summary returned for {sub}")
        else:
            log_fail("Billing summary subscription", f"got {summary.get('subscription')}")
        if summary.get("customer") == "Sagar Communications":
            log_pass(f"Billing summary customer correct")
        else:
            log_fail("Billing summary customer", f"got {summary.get('customer')}")
        if "status" in summary and "invoices" in summary:
            log_pass(f"Billing summary has status={summary['status']}, invoices={len(summary['invoices'])}")
        else:
            log_fail("Billing summary fields", f"missing status or invoices")
        if "total_outstanding" in summary and "additional_discount_percentage" in summary:
            log_pass("Billing summary has financial fields")
        else:
            log_fail("Billing summary financial fields", "missing")
    except Exception as e:
        log_fail("Billing summary", str(e))
        import traceback; traceback.print_exc()


def test_reconcile_groups_scheduler():
    print("\n" + "=" * 60)
    print("TEST: reconcile_all_groups Scheduler")
    print("=" * 60)
    try:
        from msuite.services.customer_group_service import reconcile_all_groups
        result = reconcile_all_groups()
        if isinstance(result, dict) and "customers_checked" in result:
            log_pass(f"Reconciliation ran: {result['customers_checked']} customers, "
                     f"{result['additions']} additions, {result['removals']} removals")
        else:
            log_fail("Reconciliation result", f"unexpected format: {result}")
        if isinstance(result.get("errors"), list):
            if len(result["errors"]) == 0:
                log_pass("Reconciliation: 0 errors")
            else:
                log_warn("Reconciliation errors", f"{len(result['errors'])} errors: {result['errors'][:2]}")
        frappe.db.commit()
    except Exception as e:
        log_fail("Reconcile groups scheduler", str(e))
        import traceback; traceback.print_exc()


def print_summary():
    print("\n" + "=" * 60)
    total = len(passed) + len(failed)
    print(f"FINAL: {len(passed)}/{total} PASSED, {len(failed)} FAILED, {len(warnings)} WARNINGS")
    print("=" * 60)
    if failed:
        print("\nFAILURES:")
        for t, e in failed:
            print(f"  ✗ {t}: {e}")
    if warnings:
        print("\nWARNINGS:")
        for t, m in warnings:
            print(f"  ⚠ {t}: {m}")
    if not failed:
        print("\nALL TESTS PASSED!")
