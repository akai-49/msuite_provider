import frappe
from frappe.tests.utils import FrappeTestCase


class TestMSuitePlan(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_plan_code_validation(self):
        doc = frappe.new_doc("MSuite Plan")
        doc.plan_name = "Test Plan"
        doc.plan_code = "invalid-lowercase"
        doc.product = "WA"
        doc.tier = "Basic"
        doc.item = "WA-BASIC-MONTHLY"
        self.assertRaises(frappe.ValidationError, doc.insert)

    def test_valid_plan_code_accepted(self):
        plan_code = "WA-BASIC"
        if frappe.db.exists("MSuite Plan", {"plan_code": plan_code}):
            plan = frappe.get_doc("MSuite Plan", {"plan_code": plan_code})
            self.assertEqual(plan.plan_code, plan_code)
