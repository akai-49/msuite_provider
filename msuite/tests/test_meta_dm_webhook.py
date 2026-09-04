"""
Provider-side Meta DM webhook: signature, tenant routing, durable forward.

What this proves:

  • X-Hub-Signature-256 is actually verified (a wrong signature is
    rejected; a right one passes).
  • A Facebook Page event and an Instagram event each resolve to the
    Connected Account of the client that owns that asset — and never to
    another tenant's.
  • An asset nobody owns is reported, not silently dropped.
  • Forwarding is durable: a client outage leaves a retryable
    MSuite Webhook Delivery with backoff, and the retry job drains it.
  • Forwarding is idempotent: Meta re-delivery does not double-post.
  • Failed per-Page webhook subscriptions are repaired by the scheduled
    job using the account's current token.

No network I/O — `requests` is mocked everywhere.
"""
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
import frappe.utils.password
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now

from msuite.api.v1 import webhook as provider_webhook

_MARK = "DM Webhook Test"
_FB_PAGE_ID = "PROV_PAGE_1"
_IG_USER_ID = "PROV_IG_1"
_APP_SECRET = "test-app-secret"


# ── Fixtures ──────────────────────────────────────────────────────────


def _customer(name_suffix: str) -> str:
    """MSuite Client requires a real ERPNext Customer link."""
    cust_name = f"{_MARK} Cust {name_suffix}"
    if frappe.db.exists("Customer", cust_name):
        return cust_name
    doc = frappe.get_doc({"doctype": "Customer", "customer_name": cust_name})
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.insert()
    return doc.name


def _client(name_suffix: str, status="Active") -> str:
    doc = frappe.get_doc(
        {
            "doctype": "MSuite Client",
            "client_name": f"{_MARK} {name_suffix}",
            "customer": _customer(name_suffix),
            "client_url": f"https://{name_suffix.lower()}.example.test",
            "status": status,
            "api_key": f"key-{name_suffix}",
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()
    doc.api_secret = f"secret-{name_suffix}"
    doc.save(ignore_permissions=True)
    return doc.name


def _connected_account(client: str, platform: str, account_id: str, status="Active") -> str:
    doc = frappe.get_doc(
        {
            "doctype": "MSuite Connected Account",
            "client": client,
            "platform": platform,
            "account_id": account_id,
            "display_name": f"{_MARK} {platform}",
            "status": status,
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()
    doc.access_token = "page-token"
    doc.save(ignore_permissions=True)
    return doc.name


# Every account id these tests create. Connected Accounts are purged by
# id, not by display_name: one test renames the account, and a
# name-based filter then failed to match it — the row survived teardown
# and collided with the next run's unique (client, platform, account_id)
# check.
_TEST_ACCOUNT_IDS = (_FB_PAGE_ID, _IG_USER_ID, "PROV_PAGE_OTHER")


def _purge() -> None:
    for row in frappe.get_all(
        "MSuite Connected Account", filters={"account_id": ("in", _TEST_ACCOUNT_IDS)}, pluck="name"
    ):
        try:
            frappe.delete_doc(
                "MSuite Connected Account", row, force=True, ignore_permissions=True, delete_permanently=True
            )
        except Exception:
            pass
    for dt, filters in (
        ("MSuite Webhook Delivery", {"endpoint": ("like", "%inbox%")}),
        ("MSuite Client", {"client_name": ("like", f"%{_MARK}%")}),
        ("Customer", {"customer_name": ("like", f"%{_MARK}%")}),
    ):
        for row in frappe.get_all(dt, filters=filters, pluck="name"):
            try:
                frappe.delete_doc(dt, row, force=True, ignore_permissions=True, delete_permanently=True)
            except Exception:
                pass
    frappe.db.commit()


def _fb_dm_payload():
    return {
        "object": "page",
        "entry": [
            {
                "id": _FB_PAGE_ID,
                "time": 1751000000000,
                "messaging": [
                    {
                        "sender": {"id": "PSID_1"},
                        "recipient": {"id": _FB_PAGE_ID},
                        "timestamp": 1751000000000,
                        "message": {"mid": "m_prov_1", "text": "hello"},
                    }
                ],
            }
        ],
    }


def _ig_dm_payload():
    return {
        "object": "instagram",
        "entry": [
            {
                "id": _IG_USER_ID,
                "time": 1751000001000,
                "messaging": [
                    {
                        "sender": {"id": "IGSID_1"},
                        "recipient": {"id": _IG_USER_ID},
                        "timestamp": 1751000001000,
                        "message": {"mid": "m_prov_ig_1", "text": "hola"},
                    }
                ],
            }
        ],
    }


# ── Signature verification ────────────────────────────────────────────


class TestMetaSignature(IntegrationTestCase):
    def setUp(self):
        self.app = frappe.get_all(
            "MSuite App", filters={"platform": "Meta Social", "is_active": 1}, pluck="name"
        )

    def _validate_with(self, body: bytes, signature: str) -> bool:
        request = MagicMock()
        request.headers = {"X-Hub-Signature-256": signature}
        request.get_data.return_value = body
        with patch.object(provider_webhook.frappe, "request", request), patch.object(
            provider_webhook.frappe.db, "get_value", return_value="APP-1"
        ), patch.object(provider_webhook, "get_decrypted_password", return_value=_APP_SECRET):
            return provider_webhook._validate_meta_signature("Meta Social")

    def test_valid_signature_accepted(self):
        body = json.dumps(_fb_dm_payload()).encode()
        sig = "sha256=" + hmac.new(_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(self._validate_with(body, sig))

    def test_invalid_signature_rejected(self):
        body = json.dumps(_fb_dm_payload()).encode()
        with patch.object(provider_webhook.frappe, "log_error"):
            self.assertFalse(self._validate_with(body, "sha256=" + "0" * 64))

    def test_tampered_body_rejected(self):
        body = json.dumps(_fb_dm_payload()).encode()
        sig = "sha256=" + hmac.new(_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        with patch.object(provider_webhook.frappe, "log_error"):
            self.assertFalse(self._validate_with(body + b" ", sig))

    def test_missing_signature_rejected(self):
        request = MagicMock()
        request.headers = {}
        with patch.object(provider_webhook.frappe, "request", request):
            self.assertFalse(provider_webhook._validate_meta_signature("Meta Social"))


# ── Tenant routing ────────────────────────────────────────────────────


class TestDMRouting(IntegrationTestCase):
    def setUp(self):
        _purge()
        self.client_a = _client("Alpha")
        self.client_b = _client("Bravo")
        _connected_account(self.client_a, "Facebook", _FB_PAGE_ID)
        _connected_account(self.client_a, "Instagram", _IG_USER_ID)
        # Bravo owns different assets — must never receive Alpha's events.
        _connected_account(self.client_b, "Facebook", "PROV_PAGE_OTHER")

    def tearDown(self):
        _purge()

    def test_facebook_dm_routes_to_owning_client(self):
        with patch.object(provider_webhook.frappe, "enqueue") as mock_enqueue:
            provider_webhook._dispatch_dm(_FB_PAGE_ID, _fb_dm_payload()["entry"][0], "page")
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["client_name"], self.client_a)
        self.assertEqual(kwargs["endpoint"], "msuite_workspace.inbox.api.v1.webhook.receive_meta_dm")
        self.assertEqual(kwargs["payload"]["object"], "page")

    def test_instagram_dm_routes_to_owning_client(self):
        with patch.object(provider_webhook.frappe, "enqueue") as mock_enqueue:
            provider_webhook._dispatch_dm(_IG_USER_ID, _ig_dm_payload()["entry"][0], "instagram")
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["client_name"], self.client_a)
        self.assertEqual(kwargs["payload"]["object"], "instagram")

    def test_instagram_id_is_not_matched_against_facebook_accounts(self):
        """An `instagram` object must resolve through Instagram rows only.
        Matching it against a Facebook Page is how an event could reach
        the wrong tenant."""
        with patch.object(provider_webhook.frappe, "enqueue") as mock_enqueue, patch.object(
            provider_webhook.frappe, "log_error"
        ):
            provider_webhook._dispatch_dm("PROV_PAGE_OTHER", _ig_dm_payload()["entry"][0], "instagram")
        mock_enqueue.assert_not_called()

    def test_unmapped_asset_is_reported(self):
        with patch.object(provider_webhook.frappe, "enqueue") as mock_enqueue, patch.object(
            provider_webhook.frappe, "log_error"
        ) as mock_log:
            provider_webhook._dispatch_dm("NOBODY_OWNS_THIS", _fb_dm_payload()["entry"][0], "page")
        mock_enqueue.assert_not_called()
        self.assertTrue(mock_log.called)

    def test_inactive_client_is_not_forwarded(self):
        frappe.db.set_value("MSuite Client", self.client_a, "status", "Suspended")
        with patch.object(provider_webhook.frappe, "enqueue") as mock_enqueue:
            provider_webhook._dispatch_dm(_FB_PAGE_ID, _fb_dm_payload()["entry"][0], "page")
        mock_enqueue.assert_not_called()

    def test_inbound_event_stamps_subscription_health(self):
        with patch.object(provider_webhook.frappe, "enqueue"):
            provider_webhook._dispatch_dm(_FB_PAGE_ID, _fb_dm_payload()["entry"][0], "page")
        ca = frappe.db.get_value(
            "MSuite Connected Account",
            {"account_id": _FB_PAGE_ID, "platform": "Facebook"},
            "last_inbound_event_at",
        )
        self.assertIsNotNone(ca)


# ── Durable forwarding ────────────────────────────────────────────────


class TestDurableForwarding(IntegrationTestCase):
    def setUp(self):
        _purge()
        self.client = _client("Delta")
        self.endpoint = "msuite_workspace.inbox.api.v1.webhook.receive_meta_dm"

    def tearDown(self):
        _purge()

    def _deliveries(self):
        return frappe.get_all(
            "MSuite Webhook Delivery",
            filters={"client": self.client},
            fields=["name", "status", "attempts", "last_error", "next_attempt_at"],
        )

    def test_successful_forward_marks_delivered(self):
        with patch.object(provider_webhook, "_post_to_client", return_value=(True, "")):
            provider_webhook.forward_webhook_job(self.client, self.endpoint, _fb_dm_payload())
        rows = self._deliveries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "Delivered")
        self.assertEqual(rows[0].attempts, 1)

    def test_client_outage_leaves_a_retryable_record(self):
        """The provider has already ACKed Meta — a client 502 must not
        lose the message."""
        with patch.object(provider_webhook, "_post_to_client", return_value=(False, "HTTP 502")):
            provider_webhook.forward_webhook_job(self.client, self.endpoint, _fb_dm_payload())
        rows = self._deliveries()
        self.assertEqual(rows[0].status, "Queued")
        self.assertEqual(rows[0].attempts, 1)
        self.assertIn("502", rows[0].last_error)
        self.assertIsNotNone(rows[0].next_attempt_at)

    def test_retry_job_drains_a_recovered_client(self):
        with patch.object(provider_webhook, "_post_to_client", return_value=(False, "HTTP 502")):
            provider_webhook.forward_webhook_job(self.client, self.endpoint, _fb_dm_payload())
        name = self._deliveries()[0].name
        # Backoff elapsed, client back up.
        frappe.db.set_value(
            "MSuite Webhook Delivery", name, "next_attempt_at", add_to_date(now(), minutes=-1)
        )
        with patch.object(provider_webhook, "_post_to_client", return_value=(True, "")):
            provider_webhook.retry_pending_webhook_deliveries()
        self.assertEqual(frappe.db.get_value("MSuite Webhook Delivery", name, "status"), "Delivered")

    def test_backoff_grows_and_exhausts_into_failed(self):
        with patch.object(provider_webhook, "_post_to_client", return_value=(False, "HTTP 500")):
            provider_webhook.forward_webhook_job(self.client, self.endpoint, _fb_dm_payload())
            name = self._deliveries()[0].name
            with patch.object(provider_webhook.frappe, "log_error"):
                for _ in range(provider_webhook.MAX_FORWARD_ATTEMPTS):
                    frappe.db.set_value(
                        "MSuite Webhook Delivery", name, "next_attempt_at", add_to_date(now(), minutes=-1)
                    )
                    provider_webhook.deliver_webhook(name)
        row = frappe.db.get_value(
            "MSuite Webhook Delivery", name, ["status", "attempts"], as_dict=True
        )
        self.assertEqual(row.status, "Failed")
        self.assertGreaterEqual(row.attempts, provider_webhook.MAX_FORWARD_ATTEMPTS)

    def test_duplicate_forward_is_idempotent(self):
        """Meta retries and provider retries collapse onto one delivery —
        the client must not receive (and persist) the same event twice."""
        with patch.object(provider_webhook, "_post_to_client", return_value=(True, "")) as mock_post:
            provider_webhook.forward_webhook_job(self.client, self.endpoint, _fb_dm_payload())
            provider_webhook.forward_webhook_job(self.client, self.endpoint, _fb_dm_payload())
        self.assertEqual(len(self._deliveries()), 1)
        self.assertEqual(mock_post.call_count, 1)

    def test_different_payloads_are_separate_deliveries(self):
        with patch.object(provider_webhook, "_post_to_client", return_value=(True, "")):
            provider_webhook.forward_webhook_job(self.client, self.endpoint, _fb_dm_payload())
            provider_webhook.forward_webhook_job(self.client, self.endpoint, _ig_dm_payload())
        self.assertEqual(len(self._deliveries()), 2)


# ── Reconnect / account lifecycle ─────────────────────────────────────


class TestReconnectReactivates(IntegrationTestCase):
    """Re-authorising a disconnected asset must make it Active again.

    Webhook routing filters on `status == "Active"`. When `status` was
    only set on INSERT, an account that had been disconnected stayed
    `Disconnected` after a successful reconnect — token valid, Page
    subscribed, everything looking healthy — while every inbound event
    for it was silently dropped.
    """

    def setUp(self):
        _purge()
        self.client = _client("Foxtrot")
        self.account = _connected_account(self.client, "Facebook", _FB_PAGE_ID)
        frappe.db.set_value(
            "MSuite Connected Account",
            self.account,
            {"status": "Disconnected", "needs_reauth": 1},
        )
        frappe.db.commit()

    def tearDown(self):
        _purge()

    def test_reconnect_restores_active_status(self):
        from msuite.services.oauth.base import upsert_connected_account

        upsert_connected_account(
            self.client,
            "Facebook",
            _FB_PAGE_ID,
            {"display_name": "Reconnected Page", "access_token": "fresh-page-token"},
        )
        row = frappe.db.get_value(
            "MSuite Connected Account", self.account, ["status", "needs_reauth"], as_dict=True
        )
        self.assertEqual(row.status, "Active")
        self.assertFalse(row.needs_reauth)

    def test_reconnected_account_routes_inbound_dms_again(self):
        """The end the user actually feels: DMs flow after a reconnect."""
        from msuite.services.oauth.base import upsert_connected_account

        # Disconnected → the event is dropped.
        with patch.object(provider_webhook.frappe, "enqueue") as mock_enqueue, patch.object(
            provider_webhook.frappe, "log_error"
        ):
            provider_webhook._dispatch_dm(_FB_PAGE_ID, _fb_dm_payload()["entry"][0], "page")
        mock_enqueue.assert_not_called()

        upsert_connected_account(
            self.client, "Facebook", _FB_PAGE_ID, {"access_token": "fresh-page-token"}
        )

        with patch.object(provider_webhook.frappe, "enqueue") as mock_enqueue:
            provider_webhook._dispatch_dm(_FB_PAGE_ID, _fb_dm_payload()["entry"][0], "page")
        self.assertEqual(mock_enqueue.call_args.kwargs["client_name"], self.client)

    def test_metadata_only_update_does_not_reactivate(self):
        """No fresh token means no re-authorisation — a plain metadata
        refresh must not silently revive a disconnected account."""
        from msuite.services.oauth.base import upsert_connected_account

        upsert_connected_account(
            self.client, "Facebook", _FB_PAGE_ID, {"display_name": "Renamed Page"}
        )
        self.assertEqual(
            frappe.db.get_value("MSuite Connected Account", self.account, "status"), "Disconnected"
        )


# ── Subscription repair ───────────────────────────────────────────────


class TestSubscriptionRepair(IntegrationTestCase):
    """`retry_failed_page_subscriptions` sweeps EVERY account whose last
    subscription failed — including real ones on a developer's site. Each
    test therefore hands out a token only for its own fixture, so the
    sweep skips (`continue`s on) every other account instead of firing a
    mocked "success" that would overwrite genuine subscription state.
    """

    def setUp(self):
        _purge()
        self.client = _client("Echo")
        self.account = _connected_account(self.client, "Facebook", _FB_PAGE_ID)
        frappe.db.set_value(
            "MSuite Connected Account", self.account, "last_subscription_error", "HTTP 400: bad token"
        )

    def tearDown(self):
        _purge()

    def _only_our_token(self):
        """Patch the token reader so only this test's fixture has one."""
        return patch.object(
            frappe.utils.password,
            "get_decrypted_password",
            side_effect=lambda dt, name, field, **kw: ("page-token" if name == self.account else ""),
        )

    def test_page_webhook_fields_are_all_valid_for_the_page_object(self):
        """Meta validates `subscribed_fields` atomically: one name that
        isn't a Page field rejects the whole call with (#100), leaving the
        Page subscribed to nothing and the tenant receiving no webhooks.
        `comments` is an Instagram-object field and took Page DM delivery
        down with it — guard against it coming back."""
        from msuite.services.oauth import meta_social

        self.assertNotIn("comments", meta_social.PAGE_WEBHOOK_FIELDS)
        # The DM fields are the whole point of the subscription.
        for field in ("messages", "message_echoes", "message_deliveries", "message_reads"):
            self.assertIn(field, meta_social.PAGE_WEBHOOK_FIELDS)

    def test_repair_resubscribes_and_clears_the_error(self):
        from msuite.services.oauth import meta_social

        resp = MagicMock()
        resp.ok = True
        resp.content = b'{"success": true}'
        resp.json.return_value = {"success": True}
        with patch.object(meta_social.requests, "post", return_value=resp) as mock_post, self._only_our_token():
            meta_social.retry_failed_page_subscriptions()

        self.assertTrue(mock_post.called)
        # The subscription must request the DM fields.
        sent_fields = mock_post.call_args.kwargs["data"]["subscribed_fields"]
        for field in ("messages", "message_echoes", "message_deliveries", "message_reads"):
            self.assertIn(field, sent_fields)
        self.assertNotIn("comments", sent_fields)

        row = frappe.db.get_value(
            "MSuite Connected Account",
            self.account,
            ["last_subscription_error", "subscribed_fields", "subscribed_at"],
            as_dict=True,
        )
        self.assertEqual(row.last_subscription_error, "")
        self.assertIn("messages", row.subscribed_fields)
        self.assertIsNotNone(row.subscribed_at)

    def test_repair_records_a_still_failing_subscription(self):
        from msuite.services.oauth import meta_social

        resp = MagicMock()
        resp.ok = False
        resp.status_code = 400
        resp.content = b'{"error": {"message": "Invalid OAuth token"}}'
        resp.json.return_value = {"error": {"message": "Invalid OAuth token"}}
        resp.text = '{"error": {"message": "Invalid OAuth token"}}'
        with patch.object(meta_social.requests, "post", return_value=resp), self._only_our_token():
            meta_social.retry_failed_page_subscriptions()

        err = frappe.db.get_value("MSuite Connected Account", self.account, "last_subscription_error")
        self.assertIn("Invalid OAuth token", err)

    def test_healthy_accounts_are_not_retried(self):
        """Only accounts carrying a subscription error are re-subscribed.
        Asserted per-account rather than globally: this site may hold
        other real accounts whose subscriptions are genuinely failing,
        and the job is supposed to retry those."""
        from msuite.services.oauth import meta_social

        frappe.db.set_value("MSuite Connected Account", self.account, "last_subscription_error", "")
        resp = MagicMock()
        resp.ok = True
        resp.content = b'{"success": true}'
        resp.json.return_value = {"success": True}
        with patch.object(meta_social.requests, "post", return_value=resp) as mock_post, self._only_our_token():
            meta_social.retry_failed_page_subscriptions()
        called_urls = [c.args[0] for c in mock_post.call_args_list if c.args]
        self.assertFalse([u for u in called_urls if _FB_PAGE_ID in u])


class TestMetaWebhookFallback(IntegrationTestCase):
    def test_meta_webhook_unauthorized_without_relay_token(self):
        req = MagicMock()
        req.method = "POST"
        req.headers = {}
        with patch.object(provider_webhook.frappe, "request", req), \
             patch("msuite.api.v1.bulk_relay._relay_authorised", return_value=False):
            with self.assertRaises(frappe.AuthenticationError):
                provider_webhook.meta_webhook()

    def test_meta_webhook_accepts_valid_relay_token(self):
        req = MagicMock()
        req.method = "POST"
        req.headers = {"X-MSuite-Relay-Token": "test-relay-token"}
        req.get_json.return_value = {
            "object": "page",
            "entry": [{"id": "UNKNOWN_PAGE", "time": 1751000000000, "messaging": [{"message": {"text": "hi"}}]}]
        }
        with patch("msuite.api.v1.bulk_relay._relay_authorised", return_value=True), \
             patch.object(provider_webhook.frappe, "request", req):
            res = provider_webhook.meta_webhook()
            self.assertEqual(res, {"status": "ok"})

    def test_meta_webhook_dispatches_waba(self):
        req = MagicMock()
        req.method = "POST"
        req.headers = {"X-MSuite-Relay-Token": "test-relay-token"}
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "WABA_123", "changes": [{"value": {"messages": [{"text": {"body": "hi"}}]}}]}]
        }
        req.get_json.return_value = payload
        with patch("msuite.api.v1.bulk_relay._relay_authorised", return_value=True), \
             patch.object(provider_webhook.frappe, "request", req), \
             patch.object(provider_webhook, "_forward_to_client") as mock_forward:
            res = provider_webhook.meta_webhook()
            self.assertEqual(res, {"status": "ok"})
            mock_forward.assert_called_once_with("WABA_123", payload)
