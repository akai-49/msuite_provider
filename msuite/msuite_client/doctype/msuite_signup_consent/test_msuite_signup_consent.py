# Copyright (c) 2026, MSuite and Contributors
# See license.txt
"""
MSuite Signup Consent doctype — one audit record per signup event.

The controller has no custom logic (see msuite_signup_consent.py: `pass`),
so what's under test is the schema itself: mandatory fields, defaults, and
that this is a plain per-event log (no uniqueness constraint — a client can
retry a signup and each attempt gets its own row).

No network I/O.
"""
import json

import frappe
from frappe.tests import IntegrationTestCase

# See the identical note in test_msuite_connected_account.py: MSuite Client
# and MSuite Connected Account are direct Link fields here whose own
# transitive dependencies (Customer -> ERPNext's global fiscal-year
# bootstrap) fail on this site. Fixtures here are built directly instead.
IGNORE_TEST_RECORD_DEPENDENCIES = ["MSuite Client", "MSuite Connected Account"]

_MARK = "Consent Doctype Test"


def _customer() -> str:
    name = f"{_MARK} Cust"
    if frappe.db.exists("Customer", name):
        return name
    doc = frappe.get_doc({"doctype": "Customer", "customer_name": name})
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.insert()
    return doc.name


def _client() -> str:
    name = f"{_MARK} Client"
    if frappe.db.exists("MSuite Client", name):
        return name
    doc = frappe.get_doc(
        {
            "doctype": "MSuite Client",
            "client_name": name,
            "customer": _customer(),
            "client_url": "https://consent-doctype.example.test",
            "status": "Active",
            "api_key": "key-consent-doctype",
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()
    doc.api_secret = "secret-consent-doctype"
    doc.save(ignore_permissions=True)
    return doc.name


def _consent(**overrides):
    data = {
        "doctype": "MSuite Signup Consent",
        "client": _client(),
        "platform": "WhatsApp",
        "event_type": "FINISH",
    }
    data.update(overrides)
    doc = frappe.get_doc(data)
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc


def _purge():
    for dt, filters in (
        ("MSuite Signup Consent", {"client": ("like", f"%{_MARK}%")}),
        ("MSuite Client", {"client_name": ("like", f"%{_MARK}%")}),
        ("Customer", {"customer_name": ("like", f"%{_MARK}%")}),
    ):
        for row in frappe.get_all(dt, filters=filters, pluck="name"):
            frappe.delete_doc(dt, row, force=True, ignore_permissions=True, delete_permanently=True)
    frappe.db.commit()


class IntegrationTestMSuiteSignupConsent(IntegrationTestCase):
    def setUp(self):
        _purge()

    def tearDown(self):
        _purge()

    def test_client_is_mandatory(self):
        """client is a Link field with no default — the only one of the
        three `reqd` fields that can actually be left empty. `platform` and
        `event_type` are Select fields whose options list starts with a
        real value (not a blank first line), so Frappe itself always fills
        them with that first option on a bare new_doc/get_doc — `reqd`
        never has anything to enforce on either."""
        doc = frappe.get_doc(
            {"doctype": "MSuite Signup Consent", "platform": "WhatsApp", "event_type": "FINISH"}
        )
        doc.flags.ignore_permissions = True
        with self.assertRaises(frappe.MandatoryError):
            doc.insert()

    def test_platform_and_event_type_default_to_their_first_option(self):
        # A Select field with no explicit "default" only falls back to its
        # first option when the doc goes through frappe.new_doc()'s
        # set_default_values (as the Desk UI does) -- a bare
        # frappe.get_doc({...}) dict construction skips that step entirely,
        # leaving these fields None.
        doc = frappe.new_doc("MSuite Signup Consent")
        doc.client = _client()
        self.assertEqual(doc.platform, "WhatsApp")
        self.assertEqual(doc.event_type, "FINISH")

    def test_consent_status_defaults_to_granted(self):
        doc = _consent()
        self.assertEqual(doc.consent_status, "Granted")

    def test_token_valid_defaults_true(self):
        doc = _consent()
        self.assertEqual(doc.token_valid, 1)

    def test_repeated_signup_attempts_are_separate_rows(self):
        """No uniqueness constraint — a CANCEL followed by a FINISH for the
        same client/platform must both be kept for the audit trail."""
        cancelled = _consent(event_type="CANCEL", consent_status="Cancelled", cancelled_step="phone_verify")
        finished = _consent(event_type="FINISH", consent_status="Granted")
        self.assertNotEqual(cancelled.name, finished.name)
        self.assertEqual(
            frappe.db.count("MSuite Signup Consent", {"client": cancelled.client}), 2
        )

    def test_connected_account_link_is_optional(self):
        """Null for CANCEL/ERROR events — no account was ever created."""
        doc = _consent(event_type="ERROR", consent_status="Failed", error_message="oauth_denied")
        self.assertFalse(doc.connected_account)

    def test_connected_account_link_must_exist_when_set(self):
        with self.assertRaises(frappe.LinkValidationError):
            _consent(connected_account="NOT-A-REAL-ACCOUNT")

    def test_granular_scopes_json_round_trips(self):
        scopes = [{"waba_id": "waba-1", "scopes": ["whatsapp_business_messaging"]}]
        doc = _consent(granular_scopes=json.dumps(scopes))
        reloaded = frappe.get_doc("MSuite Signup Consent", doc.name)
        self.assertEqual(json.loads(reloaded.granular_scopes), scopes)

    def test_platform_is_restricted_to_meta_signup_flows(self):
        """The signup consent flow only exists for Meta's embedded-signup
        products — Outlook/Gmail never go through this doctype."""
        with self.assertRaises(frappe.ValidationError):
            _consent(platform="Outlook")
