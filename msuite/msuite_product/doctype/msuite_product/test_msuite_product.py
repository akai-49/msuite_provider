import frappe
from frappe.tests.utils import FrappeTestCase


class TestMSuiteProduct(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_product_code_validation(self):
        doc = frappe.new_doc("MSuite Product")
        doc.product_name = "Test Product"
        doc.product_code = "invalid"
        doc.is_active = 1
        self.assertRaises(frappe.ValidationError, doc.insert)

    def test_valid_product_creation(self):
        if frappe.db.exists("MSuite Product", {"product_code": "TESTPROD"}):
            frappe.delete_doc("MSuite Product", "TESTPROD", force=True)
        doc = frappe.new_doc("MSuite Product")
        doc.product_name = "Test Product"
        doc.product_code = "TESTPROD"
        doc.is_active = 1
        doc.append("features", {
            "feature_key": "test_feature",
            "feature_label": "Test Feature",
            "feature_type": "Boolean",
        })
        doc.insert()
        self.assertEqual(doc.product_code, "TESTPROD")
        doc.delete(force=True)

    def test_duplicate_feature_keys_rejected(self):
        doc = frappe.new_doc("MSuite Product")
        doc.product_name = "Test Dup"
        doc.product_code = "TESTDUP"
        doc.is_active = 1
        doc.append("features", {
            "feature_key": "same_key",
            "feature_label": "Feature 1",
            "feature_type": "Boolean",
        })
        doc.append("features", {
            "feature_key": "same_key",
            "feature_label": "Feature 2",
            "feature_type": "Numeric",
        })
        self.assertRaises(frappe.ValidationError, doc.insert)
