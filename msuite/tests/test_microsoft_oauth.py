"""
Provider-side Microsoft OAuth handler (services/oauth/microsoft.py).

What this proves:

  • The authority is /common — so BOTH work/school and personal Microsoft
    accounts can consent. /organizations would reject outlook.com;
    /consumers would reject Microsoft 365 tenants.
  • exchange_token returns the same key set google.exchange_token does, so
    process_oauth_callback needs no special-casing.
  • Discovery creates the Auth Account + Connected Account and pushes
    credentials WITHOUT the refresh token.
  • Two personal mailboxes stay two Auth Accounts. Every personal Microsoft
    account reports the same tenant id, so keying on tenant (instead of the
    per-user oid) would silently merge them — this is the regression guard.
  • A personal account whose /me returns mail:null still resolves via
    userPrincipalName.
  • The rotated refresh token is persisted — the one behaviour that differs
    from Google, and a silent mailbox-killer if dropped.
  • Refresh failure raises rather than no-opping, so refresh_all_tokens can
    record it and eventually flag needs_reauth.

No network I/O — `requests` is mocked everywhere.
"""
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.tests import IntegrationTestCase

from msuite.services.oauth import microsoft

_MARK = "MS OAuth Test"
_APP = f"{_MARK} App"
_REDIRECT = "https://provider.example.test/api/method/msuite.api.v1.auth.auth_callback"

# Every personal Microsoft account reports this same tenant id.
MSA_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"


# ── Fixtures ──────────────────────────────────────────────────────────


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
    doc = frappe.get_doc(
        {
            "doctype": "MSuite Client",
            "client_name": f"{_MARK} Client",
            "customer": _customer(),
            "client_url": "https://client.example.test",
            "status": "Active",
            "api_key": "key-ms",
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()
    doc.api_secret = "secret-ms"
    doc.save(ignore_permissions=True)
    return doc.name


def _app() -> str:
    doc = frappe.get_doc(
        {
            "doctype": "MSuite App",
            "app_name": _APP,
            "platform": "Microsoft",
            "is_active": 1,
            "app_id": "ms-client-id",
            "redirect_uri": _REDIRECT,
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()
    doc.app_secret = "ms-client-secret"
    doc.save(ignore_permissions=True)
    return doc.name


def _me(user_id: str, mail: str | None, upn: str = "", name: str = "Test User") -> dict:
    return {"id": user_id, "mail": mail, "userPrincipalName": upn, "displayName": name}


def _json_response(payload: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.text = str(payload)
    return resp


def _purge() -> None:
    for dt, filters in (
        ("MSuite Connected Account", {"platform": "Outlook"}),
        ("MSuite Auth Account", {"platform": "Microsoft"}),
        ("MSuite App", {"app_name": ("like", f"%{_MARK}%")}),
        ("MSuite Client", {"client_name": ("like", f"%{_MARK}%")}),
        ("Customer", {"customer_name": ("like", f"%{_MARK}%")}),
    ):
        for row in frappe.get_all(dt, filters=filters, pluck="name"):
            try:
                frappe.delete_doc(dt, row, force=True, ignore_permissions=True, delete_permanently=True)
            except Exception:
                pass
    frappe.db.commit()


class TestMicrosoftOAuth(IntegrationTestCase):
    def setUp(self):
        _purge()
        self.app = _app()
        self.client = _client()

    def tearDown(self):
        _purge()

    # ── Auth URL ──────────────────────────────────────────────────────

    def test_auth_url_uses_common_authority(self):
        url = microsoft.build_auth_url(self.client, "state-abc")
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.netloc, "login.microsoftonline.com")
        # /common serves both account types. Guard against a "tighten this
        # up" edit that would silently drop one half of the product.
        self.assertTrue(parsed.path.startswith("/common/"), f"authority is {parsed.path}")
        self.assertNotIn("/organizations/", parsed.path)
        self.assertNotIn("/consumers/", parsed.path)

        scopes = params["scope"][0].split(" ")
        for required in (
            "https://graph.microsoft.com/Mail.ReadWrite",
            "https://graph.microsoft.com/Mail.Send",
            "https://graph.microsoft.com/User.Read",
            "offline_access",
        ):
            self.assertIn(required, scopes)

        self.assertEqual(params["state"][0], "state-abc")
        self.assertEqual(params["client_id"][0], "ms-client-id")
        self.assertEqual(params["redirect_uri"][0], _REDIRECT)
        self.assertEqual(params["response_type"][0], "code")

    # ── Token exchange ────────────────────────────────────────────────

    def test_exchange_token_shape(self):
        with patch.object(microsoft.requests, "post") as post:
            post.return_value = _json_response(
                {"access_token": "at", "refresh_token": "rt", "expires_in": 3599}
            )
            out = microsoft.exchange_token("code-1", {"platform": "microsoft_outlook"})

        # Same key set google.exchange_token returns — process_oauth_callback
        # consumes both through one path.
        self.assertEqual(
            set(out.keys()),
            {"access_token", "refresh_token", "expires_in", "app_name", "platform"},
        )
        self.assertEqual(out["access_token"], "at")
        self.assertEqual(out["refresh_token"], "rt")
        self.assertEqual(out["platform"], "microsoft_outlook")

    def test_exchange_token_failure_throws(self):
        with patch.object(microsoft.requests, "post") as post:
            post.return_value = _json_response({"error": "invalid_grant"}, status=400)
            with self.assertRaises(Exception):
                microsoft.exchange_token("bad-code", {})

    # ── Discovery ─────────────────────────────────────────────────────

    def _discover(self, me_payload: dict, token_data: dict | None = None):
        token_data = token_data or {
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
            "app_name": self.app,
            "platform": "microsoft_outlook",
        }
        with patch.object(microsoft.requests, "get") as get, patch(
            "msuite.services.oauth.microsoft.push_account_to_client"
        ) as push:
            get.return_value = _json_response(me_payload)
            connected = microsoft.discover_accounts(self.client, token_data)
        return connected, push

    def test_discover_creates_account_and_pushes(self):
        connected, push = self._discover(_me("oid-work-1", "user@contoso.com", name="Work User"))

        self.assertEqual(connected, [{"platform": "Outlook", "name": "user@contoso.com"}])

        self.assertTrue(
            frappe.db.exists(
                "MSuite Auth Account",
                {"client": self.client, "platform": "Microsoft", "account_id": "oid-work-1"},
            )
        )
        ca = frappe.db.get_value(
            "MSuite Connected Account",
            {"client": self.client, "platform": "Outlook", "account_id": "user@contoso.com"},
            ["name", "display_name"],
            as_dict=True,
        )
        self.assertTrue(ca)
        self.assertEqual(ca.display_name, "Work User")

        push.assert_called_once()
        _client_arg, platform_arg, payload = push.call_args.args
        self.assertEqual(platform_arg, "Outlook")
        self.assertEqual(payload["outlook_address"], "user@contoso.com")
        self.assertEqual(payload["ms_user_id"], "oid-work-1")
        self.assertEqual(payload["access_token"], "at")
        # The whole point of the provider/client split.
        self.assertNotIn("refresh_token", payload)

    def test_two_personal_accounts_do_not_collide(self):
        """Same MSA tenant, different users → two Auth Accounts.

        Keying the Auth Account on tenant id instead of the user's oid would
        merge every personal mailbox a client connects into a single row.
        """
        self._discover(_me("oid-personal-1", "alice@outlook.com", name="Alice"))
        self._discover(_me("oid-personal-2", "bob@outlook.com", name="Bob"))

        auth_ids = frappe.get_all(
            "MSuite Auth Account",
            filters={"client": self.client, "platform": "Microsoft"},
            pluck="account_id",
        )
        self.assertCountEqual(auth_ids, ["oid-personal-1", "oid-personal-2"])

        addresses = frappe.get_all(
            "MSuite Connected Account",
            filters={"client": self.client, "platform": "Outlook"},
            pluck="account_id",
        )
        self.assertCountEqual(addresses, ["alice@outlook.com", "bob@outlook.com"])

    def test_personal_account_without_mail_falls_back_to_upn(self):
        connected, push = self._discover(
            _me("oid-personal-3", None, upn="carol@hotmail.com", name="Carol")
        )
        self.assertEqual(connected, [{"platform": "Outlook", "name": "carol@hotmail.com"}])
        self.assertEqual(push.call_args.args[2]["outlook_address"], "carol@hotmail.com")

    def test_discover_without_identity_creates_nothing(self):
        connected, push = self._discover({"displayName": "Nobody"})
        self.assertEqual(connected, [])
        push.assert_not_called()
        self.assertFalse(
            frappe.get_all("MSuite Connected Account", filters={"client": self.client, "platform": "Outlook"})
        )

    # ── Refresh ───────────────────────────────────────────────────────

    def _connected_account(self) -> str:
        doc = frappe.get_doc(
            {
                "doctype": "MSuite Connected Account",
                "client": self.client,
                "platform": "Outlook",
                "account_id": "refresh@contoso.com",
                "display_name": "Refresh User",
                "status": "Active",
            }
        )
        doc.flags.ignore_permissions = True
        doc.insert()
        doc.access_token = "old-access"
        doc.refresh_token = "old-refresh"
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return doc.name

    def test_refresh_persists_rotated_refresh_token(self):
        ca_name = self._connected_account()

        with patch.object(microsoft.requests, "post") as post:
            post.return_value = _json_response(
                {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
            )
            microsoft.refresh_token_fn(ca_name)

        ca = frappe.get_doc("MSuite Connected Account", ca_name)
        self.assertEqual(ca.get_password("access_token"), "new-access")
        # Microsoft rotates refresh tokens. Dropping the new one leaves the
        # mailbox working until the old token falls out of the rotation
        # window, then every refresh fails.
        self.assertEqual(ca.get_password("refresh_token"), "new-refresh")

    def test_refresh_without_rotation_keeps_existing_token(self):
        ca_name = self._connected_account()

        with patch.object(microsoft.requests, "post") as post:
            post.return_value = _json_response({"access_token": "new-access", "expires_in": 3600})
            microsoft.refresh_token_fn(ca_name)

        ca = frappe.get_doc("MSuite Connected Account", ca_name)
        self.assertEqual(ca.get_password("refresh_token"), "old-refresh")

    def test_refresh_failure_raises(self):
        ca_name = self._connected_account()

        with patch.object(microsoft.requests, "post") as post:
            post.return_value = _json_response(
                {"error": "invalid_grant", "error_description": "consent revoked"}, status=400
            )
            with self.assertRaises(RuntimeError):
                microsoft.refresh_token_fn(ca_name)

    def test_refresh_without_stored_token_raises(self):
        doc = frappe.get_doc(
            {
                "doctype": "MSuite Connected Account",
                "client": self.client,
                "platform": "Outlook",
                "account_id": "notoken@contoso.com",
                "display_name": "No Token",
                "status": "Active",
            }
        )
        doc.flags.ignore_permissions = True
        doc.insert()
        frappe.db.commit()

        with self.assertRaises(RuntimeError):
            microsoft.refresh_token_fn(doc.name)

    # ── Registry wiring ───────────────────────────────────────────────

    def test_registered_in_all_four_registries(self):
        from msuite.services import oauth

        self.assertIs(oauth._AUTH_URL_BUILDERS["microsoft_outlook"], microsoft.build_auth_url)
        self.assertIs(oauth._TOKEN_EXCHANGERS["microsoft_outlook"], microsoft.exchange_token)
        self.assertIs(oauth._ACCOUNT_DISCOVERERS["microsoft_outlook"], microsoft.discover_accounts)
        # Keyed on Connected Account.platform, not the product key.
        self.assertIs(oauth._TOKEN_REFRESHERS["Outlook"], microsoft.refresh_token_fn)
        # The Connections page lights the card up off this list.
        self.assertTrue(oauth.is_platform_supported("microsoft_outlook"))
