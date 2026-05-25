import frappe
from frappe.tests.utils import FrappeTestCase


class TestCouponService(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_validate_coupon_not_found_raises(self):
        """Non-existent coupon should raise."""
        from msuite.services.coupon_service import validate_coupon_for_customer
        from msuite.exceptions import CouponValidationError

        self.assertRaises(
            CouponValidationError,
            validate_coupon_for_customer,
            "NONEXISTENT-COUPON",
            "MSuite Test Customer",
        )

    def test_record_coupon_usage_is_idempotent(self):
        """Recording usage twice should not create duplicate."""
        from msuite.services.coupon_service import record_coupon_usage

        # This test requires a Coupon Code to exist - skip if not available
        coupon = frappe.db.get_value("Coupon Code", {}, "name")
        if not coupon:
            self.skipTest("No Coupon Code available")

        customer = self._get_or_create_test_customer()
        invoice = frappe.db.get_value("Sales Invoice", {}, "name")
        if not invoice:
            self.skipTest("No Sales Invoice available")

        # Clean up any existing usage
        existing = frappe.db.get_value(
            "MSuite Coupon Usage",
            {"coupon_code": coupon, "customer": customer},
            "name",
        )
        if existing:
            frappe.delete_doc("MSuite Coupon Usage", existing, force=True)

        record_coupon_usage(coupon, customer, invoice)
        record_coupon_usage(coupon, customer, invoice)  # Second call should not error

        count = frappe.db.count(
            "MSuite Coupon Usage",
            {"coupon_code": coupon, "customer": customer},
        )
        self.assertEqual(count, 1)

    def _get_or_create_test_customer(self):
        name = "MSuite Coupon Test Customer"
        if not frappe.db.exists("Customer", name):
            doc = frappe.new_doc("Customer")
            doc.customer_name = name
            doc.customer_group = "All Customer Groups"
            doc.territory = "All Territories"
            doc.insert(ignore_permissions=True)
        return name
