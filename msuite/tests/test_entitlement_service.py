import frappe
from frappe.tests.utils import FrappeTestCase


class TestEntitlementService(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_no_active_subscriptions_returns_empty_features(self):
        """Customer with no subscriptions should return empty features."""
        from msuite.services.entitlement_service import get_customer_entitlements

        customer = self._get_or_create_test_customer()
        result = get_customer_entitlements(customer)
        self.assertEqual(result["features"], {})
        self.assertEqual(result["active_plans"], [])

    def test_check_feature_access_returns_false_for_nonexistent_feature(self):
        """Non-existent feature should return False."""
        from msuite.services.entitlement_service import check_feature_access

        customer = self._get_or_create_test_customer()
        self.assertFalse(check_feature_access(customer, "nonexistent_feature"))

    def test_get_feature_limit_returns_none_for_nonexistent(self):
        """Non-existent feature should return None."""
        from msuite.services.entitlement_service import get_feature_limit

        customer = self._get_or_create_test_customer()
        self.assertIsNone(get_feature_limit(customer, "nonexistent_feature"))

    def test_entitlement_error_for_nonexistent_customer(self):
        """Should raise for non-existent customer."""
        from msuite.services.entitlement_service import get_customer_entitlements
        from msuite.exceptions import EntitlementError

        self.assertRaises(EntitlementError, get_customer_entitlements, "NONEXISTENT-CUSTOMER-XYZ")

    def test_cache_populated_after_first_call(self):
        """After first call, cache should be populated."""
        from msuite.services.entitlement_service import get_customer_entitlements
        from msuite.utils.cache import get_entitlement_cache, invalidate_entitlement_cache

        customer = self._get_or_create_test_customer()
        invalidate_entitlement_cache(customer)
        get_customer_entitlements(customer)
        cached = get_entitlement_cache(customer)
        self.assertIsNotNone(cached)

    def _get_or_create_test_customer(self):
        name = "MSuite Test Customer"
        if not frappe.db.exists("Customer", name):
            doc = frappe.new_doc("Customer")
            doc.customer_name = name
            doc.customer_group = "All Customer Groups"
            doc.territory = "All Territories"
            doc.insert(ignore_permissions=True)
        return name
