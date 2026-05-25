import frappe
from frappe.tests.utils import FrappeTestCase

from msuite.constants import GrantStatus, GrantType
from msuite.exceptions import InvalidGrantStateError


class TestGrantService(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_revoke_grant_sets_status_and_reason(self):
        """Revoking a grant should set status, date, and reason."""
        from msuite.services.grant_service import revoke_grant

        customer = self._get_or_create_test_customer()
        grant = self._create_test_grant(customer)

        revoke_grant(grant.name, "Test revocation")

        grant_doc = frappe.get_doc("MSuite Customer Grant", grant.name)
        self.assertEqual(grant_doc.status, GrantStatus.REVOKED)
        self.assertIsNotNone(grant_doc.revoked_on)
        self.assertEqual(grant_doc.revocation_reason, "Test revocation")

    def test_revoke_grant_raises_on_already_revoked(self):
        """Revoking an already revoked grant should raise."""
        from msuite.services.grant_service import revoke_grant

        customer = self._get_or_create_test_customer()
        grant = self._create_test_grant(customer)
        revoke_grant(grant.name, "First revoke")

        self.assertRaises(InvalidGrantStateError, revoke_grant, grant.name, "Second revoke")

    def test_idempotency_no_duplicate_on_double_apply(self):
        """Applying grants twice should not create duplicates."""
        from msuite.services.grant_service import _apply_single_plan_grant, _grant_exists

        customer = self._get_or_create_test_customer()
        plan = self._get_test_plan()
        sub = self._get_or_create_test_subscription(customer)

        if not plan or not sub:
            return

        name1 = _apply_single_plan_grant(customer, plan, sub, GrantType.PAID)
        name2 = _apply_single_plan_grant(customer, plan, sub, GrantType.PAID)

        self.assertEqual(name1, name2)

    def _get_or_create_test_customer(self):
        name = "MSuite Grant Test Customer"
        if not frappe.db.exists("Customer", name):
            doc = frappe.new_doc("Customer")
            doc.customer_name = name
            doc.customer_group = "All Customer Groups"
            doc.territory = "All Territories"
            doc.insert(ignore_permissions=True)
        return name

    def _get_test_plan(self):
        return frappe.db.get_value("MSuite Plan", {"plan_code": "WA-BASIC"}, "name")

    def _get_or_create_test_subscription(self, customer):
        existing = frappe.db.get_value(
            "Subscription", {"party": customer, "status": "Active"}, "name"
        )
        if existing:
            return existing
        return None

    def _create_test_grant(self, customer):
        plan = self._get_test_plan()
        if not plan:
            self.skipTest("No test plan available")

        doc = frappe.new_doc("MSuite Customer Grant")
        doc.customer = customer
        doc.granted_plan = plan
        doc.source_subscription = "SUB-TEST-001"
        doc.grant_type = GrantType.PAID
        doc.status = GrantStatus.ACTIVE
        doc.granted_on = frappe.utils.today()
        doc.insert(ignore_permissions=True)
        return doc
