import frappe
from frappe.tests.utils import FrappeTestCase


class TestBundleService(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_is_bundle_item_false_for_regular_item(self):
        """Regular item should not be a bundle."""
        from msuite.services.bundle_service import is_bundle_item

        regular_item = frappe.db.get_value("Item", {"is_msuite_bundle": 0}, "name")
        if regular_item:
            self.assertFalse(is_bundle_item(regular_item))

    def test_is_bundle_item_true_for_tagged_item(self):
        """Bundle-tagged item should return True."""
        from msuite.services.bundle_service import is_bundle_item

        bundle_item = frappe.db.get_value("Item", {"is_msuite_bundle": 1}, "name")
        if bundle_item:
            self.assertTrue(is_bundle_item(bundle_item))
        else:
            self.skipTest("No bundle item found")

    def test_get_bundle_components_returns_all_plans(self):
        """Should return all component plans."""
        from msuite.services.bundle_service import get_bundle_components

        bundle = frappe.db.get_value("MSuite Bundle", {"bundle_code": "MARKETING-COMBO"}, "name")
        if not bundle:
            self.skipTest("Marketing Combo bundle not seeded")

        components = get_bundle_components(bundle)
        self.assertGreaterEqual(len(components), 2)

    def test_validate_bundle_returns_empty_on_valid(self):
        """Valid bundle should return no errors."""
        from msuite.services.bundle_service import validate_bundle

        bundle = frappe.db.get_value("MSuite Bundle", {"bundle_code": "MARKETING-COMBO"}, "name")
        if not bundle:
            self.skipTest("Marketing Combo bundle not seeded")

        errors = validate_bundle(bundle)
        self.assertEqual(errors, [])
