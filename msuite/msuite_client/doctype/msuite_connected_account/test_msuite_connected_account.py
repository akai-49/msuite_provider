# Copyright (c) 2026, MSuite and Contributors
# See license.txt
"""
MSuite Connected Account doctype — the per-platform token record.

What this proves:

  • Uniqueness is enforced on (client, platform, account_id) — the
    controller's whole reason to exist.
  • A different platform, or a different account_id, is a distinct row.
  • access_token / refresh_token are Password fields: the raw DB column is
    never the plaintext, and get_password() decrypts it back.
  • status defaults to Active on a bare insert (the field's `default`, since
    the controller itself sets no default — every real Active flip goes
    through services.oauth.base.upsert_connected_account instead).

No network I/O.
"""
import frappe
from frappe.tests import IntegrationTestCase

# MSuite Client (a direct Link field here) recursively pulls in its own
# `customer` Link -> Customer -> ERPNext's global BootStrapTestData (fiscal
# years, company, etc.) the moment the auto-loader walks that far — expensive,
# and on this site it collides with a real Fiscal Year already on the books
# and fails setUpClass outright. Every test here builds its own Customer /
# MSuite Client fixtures directly, so the auto-loader isn't needed; ignoring
# it at the source (MSuite Client) blocks the whole chain, since
# IGNORE_TEST_RECORD_DEPENDENCIES only filters a doctype's own *direct* link
# fields, not transitive ones.
IGNORE_TEST_RECORD_DEPENDENCIES = ["MSuite Client", "MSuite Auth Account", "MSuite App"]

_MARK = "CA Doctype Test"


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
            "client_url": "https://ca-doctype.example.test",
            "status": "Active",
            "api_key": "key-ca-doctype",
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()
    doc.api_secret = "secret-ca-doctype"
    doc.save(ignore_permissions=True)
    return doc.name


def _account(platform="Facebook", account_id="ACC-1", **overrides):
    data = {
        "doctype": "MSuite Connected Account",
        "client": _client(),
        "platform": platform,
        "account_id": account_id,
        "display_name": f"{_MARK} {platform}",
    }
    data.update(overrides)
    doc = frappe.get_doc(data)
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc


def _purge():
    # A row can lose its display_name marker mid-test (e.g. the
    # rename-doesn't-self-collide test) -- match every marked client's
    # Connected Accounts too, or a renamed row silently escapes cleanup
    # and its account_id collides with the next run.
    marked_clients = frappe.get_all(
        "MSuite Client", filters={"client_name": ("like", f"%{_MARK}%")}, pluck="name"
    )
    connected_account_names = set(
        frappe.get_all(
            "MSuite Connected Account", filters={"display_name": ("like", f"%{_MARK}%")}, pluck="name"
        )
    )
    if marked_clients:
        connected_account_names.update(
            frappe.get_all(
                "MSuite Connected Account", filters={"client": ("in", marked_clients)}, pluck="name"
            )
        )
    for row in connected_account_names:
        frappe.delete_doc(
            "MSuite Connected Account", row, force=True, ignore_permissions=True, delete_permanently=True
        )
    for dt, filters in (
        ("MSuite Client", {"client_name": ("like", f"%{_MARK}%")}),
        ("Customer", {"customer_name": ("like", f"%{_MARK}%")}),
    ):
        for row in frappe.get_all(dt, filters=filters, pluck="name"):
            frappe.delete_doc(dt, row, force=True, ignore_permissions=True, delete_permanently=True)
    frappe.db.commit()


class IntegrationTestMSuiteConnectedAccount(IntegrationTestCase):
    def setUp(self):
        _purge()

    def tearDown(self):
        _purge()

    def test_duplicate_client_platform_account_id_is_rejected(self):
        _account(platform="Facebook", account_id="DUP-1")
        with self.assertRaises(frappe.ValidationError):
            _account(platform="Facebook", account_id="DUP-1")

    def test_same_account_id_different_platform_is_allowed(self):
        """account_id spaces are per-platform (page_id vs ig_user_id vs
        waba_id) — nothing stops them colliding numerically."""
        _account(platform="Facebook", account_id="SHARED-ID")
        # Must not raise.
        _account(platform="Instagram", account_id="SHARED-ID")

    def test_same_platform_different_account_id_is_allowed(self):
        _account(platform="Facebook", account_id="MULTI-1")
        _account(platform="Facebook", account_id="MULTI-2")

    def test_editing_the_same_row_does_not_trip_the_uniqueness_check(self):
        """The controller excludes `name != self.name` — a plain save of an
        existing row must not self-collide."""
        doc = _account(platform="Facebook", account_id="EDIT-1")
        doc.display_name = "Renamed"
        doc.save(ignore_permissions=True)  # must not raise
        self.assertEqual(
            frappe.db.get_value("MSuite Connected Account", doc.name, "display_name"), "Renamed"
        )

    def test_status_defaults_to_active(self):
        doc = _account(platform="Facebook", account_id="STATUS-1")
        self.assertEqual(doc.status, "Active")

    def test_access_token_is_encrypted_at_rest(self):
        doc = _account(platform="Facebook", account_id="TOKEN-1")
        doc.access_token = "plaintext-fake-token-do-not-use"
        doc.refresh_token = "plaintext-fake-refresh-do-not-use"
        doc.save(ignore_permissions=True)

        raw = frappe.db.get_value("MSuite Connected Account", doc.name, "access_token")
        self.assertNotEqual(raw, "plaintext-fake-token-do-not-use")

        reloaded = frappe.get_doc("MSuite Connected Account", doc.name)
        self.assertEqual(reloaded.get_password("access_token"), "plaintext-fake-token-do-not-use")
        self.assertEqual(reloaded.get_password("refresh_token"), "plaintext-fake-refresh-do-not-use")

    def test_client_is_mandatory(self):
        doc = frappe.get_doc(
            {
                "doctype": "MSuite Connected Account",
                "platform": "Facebook",
                "account_id": "NOCLIENT-1",
                "display_name": "No Client",
            }
        )
        doc.flags.ignore_permissions = True
        with self.assertRaises(frappe.MandatoryError):
            doc.insert()
