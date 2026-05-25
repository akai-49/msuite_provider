import frappe
from frappe.tests.utils import FrappeTestCase


class TestMSuiteBundle(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_bundle_requires_minimum_two_components(self):
        doc = frappe.new_doc("MSuite Bundle")
        doc.bundle_name = "Single Bundle"
        doc.bundle_code = "SINGLE-TEST"
        doc.is_active = 1
        wa_biz = frappe.db.get_value("MSuite Plan", {"plan_code": "WA-BIZ"}, "name")
        if wa_biz:
            doc.append("components", {"plan": wa_biz, "quantity": 1})
            self.assertRaises(Exception, doc.insert)
