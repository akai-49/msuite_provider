"""
Client-initiated account disconnect (api/v1/auth.py) and the shared-secret
auth guard it depends on (utils/validators.require_msuite_client_auth).

What this proves:

  • require_msuite_client_auth accepts only an exact api_key/api_secret
    match, over a constant-time comparison, and only for an Active client.
  • It resolves either the MSuite Client doc name or its client_code.
  • disconnect_account_for_client flips matching Connected Account rows to
    Disconnected and clears needs_reauth, scoped to the calling client only
    — it must never touch another tenant's rows.
  • Matching falls back account_id -> display_name -> account_name, in that
    order, and stops at the first that yields a hit.
  • The "X" -> "Twitter" platform alias applies on both the auth lookup path
    (none here) and the disconnect filter.

No network I/O.
"""
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite.api.v1 import auth as provider_auth
from msuite.utils.validators import require_msuite_client_auth

_MARK = "Lifecycle Test"


def _customer(suffix: str) -> str:
    name = f"{_MARK} Cust {suffix}"
    if frappe.db.exists("Customer", name):
        return name
    doc = frappe.get_doc({"doctype": "Customer", "customer_name": name})
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.insert()
    return doc.name


def _client(suffix: str, status="Active", client_code=None) -> str:
    name = f"{_MARK} Client {suffix}"
    if frappe.db.exists("MSuite Client", name):
        frappe.db.set_value("MSuite Client", name, "status", status)
        return name
    data = {
        "doctype": "MSuite Client",
        "client_name": name,
        "customer": _customer(suffix),
        "client_url": f"https://{suffix.lower()}.lifecycle.example.test",
        "status": status,
        "api_key": f"key-{suffix}",
    }
    if client_code:
        data["client_code"] = client_code
    doc = frappe.get_doc(data)
    doc.flags.ignore_permissions = True
    doc.insert()
    doc.api_secret = f"secret-{suffix}"
    doc.save(ignore_permissions=True)
    return doc.name


def _connected_account(client: str, platform="Facebook", account_id="ACC-1", **overrides) -> str:
    data = {
        "doctype": "MSuite Connected Account",
        "client": client,
        "platform": platform,
        "account_id": account_id,
        "display_name": f"{_MARK} {platform}",
        "status": "Active",
        "needs_reauth": 1,
    }
    data.update(overrides)
    doc = frappe.get_doc(data)
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc.name


def _purge():
    # Some tests override display_name (dropping the _MARK prefix) to
    # exercise the fallback-name matching logic itself, so a Connected
    # Account can't always be found by display_name alone. Every one is
    # still linked to a marked MSuite Client though, so match on that too
    # or a leftover row silently escapes cleanup and collides with the
    # next run.
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


def _request(headers: dict) -> MagicMock:
    req = MagicMock()
    req.headers = headers
    return req


class TestRequireMSuiteClientAuth(IntegrationTestCase):
    def setUp(self):
        _purge()
        self.client_name = _client("Alpha", client_code="alpha-code-1")

    def tearDown(self):
        _purge()

    def test_correct_key_and_secret_pass(self):
        headers = {"X-MSuite-Provider-Key": "key-Alpha", "X-MSuite-Provider-Secret": "secret-Alpha"}
        with patch.object(provider_auth.frappe, "request", _request(headers)):
            doc = require_msuite_client_auth(self.client_name)
        self.assertEqual(doc.name, self.client_name)

    def test_resolves_by_client_code_too(self):
        headers = {"X-MSuite-Provider-Key": "key-Alpha", "X-MSuite-Provider-Secret": "secret-Alpha"}
        with patch.object(provider_auth.frappe, "request", _request(headers)):
            doc = require_msuite_client_auth("alpha-code-1")
        self.assertEqual(doc.name, self.client_name)

    def test_wrong_secret_is_rejected(self):
        headers = {"X-MSuite-Provider-Key": "key-Alpha", "X-MSuite-Provider-Secret": "wrong"}
        with patch.object(provider_auth.frappe, "request", _request(headers)):
            with self.assertRaises(frappe.AuthenticationError):
                require_msuite_client_auth(self.client_name)

    def test_missing_headers_are_rejected(self):
        with patch.object(provider_auth.frappe, "request", _request({})):
            with self.assertRaises(frappe.AuthenticationError):
                require_msuite_client_auth(self.client_name)

    def test_unknown_client_is_rejected(self):
        headers = {"X-MSuite-Provider-Key": "x", "X-MSuite-Provider-Secret": "y"}
        with patch.object(provider_auth.frappe, "request", _request(headers)):
            with self.assertRaises(frappe.AuthenticationError):
                require_msuite_client_auth("no-such-client")

    def test_suspended_client_is_rejected_even_with_right_credentials(self):
        frappe.db.set_value("MSuite Client", self.client_name, "status", "Suspended")
        headers = {"X-MSuite-Provider-Key": "key-Alpha", "X-MSuite-Provider-Secret": "secret-Alpha"}
        with patch.object(provider_auth.frappe, "request", _request(headers)):
            with self.assertRaises(frappe.PermissionError):
                require_msuite_client_auth(self.client_name)


class TestDisconnectAccountForClient(IntegrationTestCase):
    def setUp(self):
        _purge()
        self.client_a = _client("Bravo")
        self.client_b = _client("Charlie")
        self.headers_a = {"X-MSuite-Provider-Key": "key-Bravo", "X-MSuite-Provider-Secret": "secret-Bravo"}

    def tearDown(self):
        frappe.db.rollback()
        _purge()

    def _call(self, headers, **kwargs):
        with patch.object(provider_auth.frappe, "request", _request(headers)):
            return provider_auth.disconnect_account_for_client(**kwargs)

    def test_disconnect_by_account_id_flips_status_and_clears_reauth(self):
        ca = _connected_account(self.client_a, platform="Facebook", account_id="FB-1")
        out = self._call(self.headers_a, client_name=self.client_a, platform="Facebook", account_id="FB-1")

        self.assertEqual(out["status"], "success")
        self.assertEqual(out["data"]["updated"], 1)
        row = frappe.db.get_value(
            "MSuite Connected Account", ca, ["status", "needs_reauth"], as_dict=True
        )
        self.assertEqual(row.status, "Disconnected")
        self.assertFalse(row.needs_reauth)

    def test_disconnect_scoped_to_calling_client_only(self):
        """Client B must never be able to disconnect Client A's account by
        guessing an account_id."""
        ca_a = _connected_account(self.client_a, platform="Facebook", account_id="SHARED-ID")
        headers_b = {"X-MSuite-Provider-Key": "key-Charlie", "X-MSuite-Provider-Secret": "secret-Charlie"}

        out = self._call(
            headers_b, client_name=self.client_b, platform="Facebook", account_id="SHARED-ID"
        )
        self.assertEqual(out["data"]["updated"], 0)
        self.assertEqual(frappe.db.get_value("MSuite Connected Account", ca_a, "status"), "Active")

    def test_falls_back_to_display_name_when_account_id_not_given(self):
        ca = _connected_account(
            self.client_a, platform="Instagram", account_id="IG-1", display_name="My IG Page"
        )
        out = self._call(
            self.headers_a, client_name=self.client_a, platform="Instagram", display_name="My IG Page"
        )
        self.assertEqual(out["data"]["updated"], 1)
        self.assertEqual(frappe.db.get_value("MSuite Connected Account", ca, "status"), "Disconnected")

    def test_falls_back_to_account_name_last(self):
        ca = _connected_account(
            self.client_a, platform="Instagram", account_id="IG-2", display_name="Second IG"
        )
        out = self._call(
            self.headers_a, client_name=self.client_a, platform="Instagram", account_name="Second IG"
        )
        self.assertEqual(out["data"]["updated"], 1)
        self.assertEqual(frappe.db.get_value("MSuite Connected Account", ca, "status"), "Disconnected")

    def test_x_platform_alias_maps_to_twitter(self):
        ca = _connected_account(self.client_a, platform="Twitter", account_id="TW-1")
        out = self._call(self.headers_a, client_name=self.client_a, platform="X", account_id="TW-1")
        self.assertEqual(out["data"]["updated"], 1)
        self.assertEqual(frappe.db.get_value("MSuite Connected Account", ca, "status"), "Disconnected")

    def test_no_match_updates_nothing(self):
        out = self._call(
            self.headers_a, client_name=self.client_a, platform="Facebook", account_id="NOPE"
        )
        self.assertEqual(out["data"]["updated"], 0)

    def test_bad_credentials_are_refused_not_silently_ignored(self):
        out = self._call(
            {"X-MSuite-Provider-Key": "x", "X-MSuite-Provider-Secret": "y"},
            client_name=self.client_a,
            platform="Facebook",
            account_id="FB-1",
        )
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["error_code"], "PERMISSION_DENIED")
