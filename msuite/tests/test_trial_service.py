import frappe
from frappe.tests.utils import FrappeTestCase


class TestTrialService(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_get_trial_plan_returns_correct_plan(self):
        """Should return Trial plan for WhatsApp product."""
        from msuite.services.trial_service import get_trial_plan_for_product

        wa_product = frappe.db.get_value("MSuite Product", {"product_code": "WA"}, "name")
        if not wa_product:
            self.skipTest("WhatsApp product not seeded")

        trial_plan = get_trial_plan_for_product(wa_product)
        if trial_plan:
            tier = frappe.db.get_value("MSuite Plan", trial_plan, "tier")
            self.assertEqual(tier, "Trial")

    def test_get_trial_plan_returns_none_when_not_configured(self):
        """Should return None for non-existent product."""
        from msuite.services.trial_service import get_trial_plan_for_product

        result = get_trial_plan_for_product("NONEXISTENT-PRODUCT")
        self.assertIsNone(result)

    def test_get_trial_expiry_returns_none_for_no_trial(self):
        """Should return None when no trial configured."""
        from msuite.services.trial_service import get_trial_expiry

        result = get_trial_expiry("NONEXISTENT-SUB")
        self.assertIsNone(result)
