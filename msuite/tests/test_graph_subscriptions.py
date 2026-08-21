"""
Phase 5 — Graph change-notification subscriptions + attachment download.

What this proves:

  • The validation handshake echoes the raw token as text/plain. Graph
    refuses the subscription for anything else, JSON wrapping included.
  • A notification is a HINT, never a data source: it triggers the ordinary
    delta sync and nothing is read out of the payload.
  • clientState is verified. The endpoint is public by necessity, so a
    forged notification with the wrong secret must be dropped.
  • One delta run per mailbox per batch, not one per notification.
  • Subscription creation is skipped (not crashed) when the notification URL
    isn't public HTTPS — push degrades to polling.
  • Renewal recreates on 404, and the daily cron registers push for
    mailboxes that have none.
  • Attachment bytes come back as standard base64 from both backends.

No network I/O — `requests` is mocked everywhere.
"""
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from msuite.services.mail import graph, graph_subscriptions as subs

_MARK = "Graph Sub Test"
_MAILBOX = "subtest@contoso.example"
_SUB_ID = "sub-abc-123"


def _resp(payload, status=200, headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
    r.headers = headers or {}
    return r


def _purge():
    for dt, filters in (
        ("MSuite Connected Account", {"account_id": _MAILBOX}),
        ("MSuite Client", {"client_name": ("like", f"%{_MARK}%")}),
        ("Customer", {"customer_name": ("like", f"%{_MARK}%")}),
    ):
        for row in frappe.get_all(dt, filters=filters, pluck="name"):
            try:
                frappe.delete_doc(dt, row, force=True, ignore_permissions=True, delete_permanently=True)
            except Exception:
                pass
    frappe.db.commit()


class _Base(IntegrationTestCase):
    def setUp(self):
        _purge()
        cust = frappe.get_doc({"doctype": "Customer", "customer_name": f"{_MARK} Cust"})
        cust.flags.ignore_permissions = True
        cust.flags.ignore_mandatory = True
        cust.insert()

        client = frappe.get_doc(
            {
                "doctype": "MSuite Client",
                "client_name": f"{_MARK} Client",
                "customer": cust.name,
                "client_url": "https://client.example.test",
                "status": "Active",
                "api_key": "key-sub",
            }
        )
        client.flags.ignore_permissions = True
        client.insert()
        client.api_secret = "secret-sub"
        client.save(ignore_permissions=True)
        self.client_name = client.name

        ca = frappe.get_doc(
            {
                "doctype": "MSuite Connected Account",
                "client": client.name,
                "platform": "Outlook",
                "account_id": _MAILBOX,
                "display_name": "Sub Test",
                "status": "Active",
                "token_expiry": add_to_date(now_datetime(), hours=1),
            }
        )
        ca.flags.ignore_permissions = True
        ca.insert()
        ca.access_token = "at"
        ca.refresh_token = "rt"
        ca.save(ignore_permissions=True)
        frappe.db.commit()
        self.ca_name = ca.name

    def tearDown(self):
        _purge()


class TestSubscriptionLifecycle(_Base):
    def test_create_skipped_without_public_https(self):
        """Graph validates the URL synchronously; localhost can't work.
        Skipping must be quiet — polling still covers ingestion."""
        with patch.object(subs, "_public_base_url", return_value="http://localhost:8000"), patch.object(
            subs, "_request"
        ) as req:
            self.assertIsNone(subs.create_subscription(self.ca_name))
            req.assert_not_called()

    def test_create_stores_id_and_expiry(self):
        with patch.object(subs, "_public_base_url", return_value="https://prov.example.test"), patch.object(
            subs, "_request"
        ) as req:
            req.return_value = _resp(
                {"id": _SUB_ID, "expirationDateTime": "2026-08-18T10:00:00.000Z"}, status=201
            )
            out = subs.create_subscription(self.ca_name)

        self.assertEqual(out["id"], _SUB_ID)
        ca = frappe.get_doc("MSuite Connected Account", self.ca_name)
        self.assertEqual(ca.graph_subscription_id, _SUB_ID)
        self.assertTrue(ca.graph_subscription_expiry)

        body = req.call_args.kwargs["json"]
        self.assertEqual(body["changeType"], "created")
        self.assertTrue(body["notificationUrl"].startswith("https://"))
        self.assertTrue(body["lifecycleNotificationUrl"].startswith("https://"))
        self.assertTrue(body["clientState"])

    def test_create_failure_is_swallowed(self):
        with patch.object(subs, "_public_base_url", return_value="https://prov.example.test"), patch.object(
            subs, "_request"
        ) as req:
            req.return_value = _resp({"error": "bad url"}, status=400)
            self.assertIsNone(subs.create_subscription(self.ca_name))

        # No id stored, and no exception — ingestion falls back to polling.
        self.assertFalse(
            frappe.db.get_value("MSuite Connected Account", self.ca_name, "graph_subscription_id")
        )

    def test_renew_recreates_when_graph_forgot_it(self):
        frappe.db.set_value("MSuite Connected Account", self.ca_name, "graph_subscription_id", _SUB_ID)

        with patch.object(subs, "_public_base_url", return_value="https://prov.example.test"), patch.object(
            subs, "_request"
        ) as req:
            req.side_effect = [
                _resp({}, status=404),  # PATCH — gone
                _resp({"id": "sub-new", "expirationDateTime": "2026-08-18T10:00:00Z"}, status=201),
            ]
            self.assertTrue(subs.renew_subscription(self.ca_name))

        self.assertEqual(
            frappe.db.get_value("MSuite Connected Account", self.ca_name, "graph_subscription_id"),
            "sub-new",
        )

    def test_cron_registers_push_for_unsubscribed_mailbox(self):
        with patch.object(subs, "create_subscription") as create, patch.object(
            subs, "renew_subscription"
        ) as renew:
            subs.renew_graph_subscriptions()
        create.assert_called_once_with(self.ca_name)
        renew.assert_not_called()

    def test_cron_renews_only_when_expiring_soon(self):
        frappe.db.set_value(
            "MSuite Connected Account",
            self.ca_name,
            {
                "graph_subscription_id": _SUB_ID,
                "graph_subscription_expiry": add_to_date(now_datetime(), days=5),
            },
        )
        with patch.object(subs, "renew_subscription") as renew:
            subs.renew_graph_subscriptions()
        renew.assert_not_called()

        frappe.db.set_value(
            "MSuite Connected Account",
            self.ca_name,
            "graph_subscription_expiry",
            add_to_date(now_datetime(), hours=2),
        )
        with patch.object(subs, "renew_subscription") as renew:
            subs.renew_graph_subscriptions()
        renew.assert_called_once_with(self.ca_name)

    def test_client_state_is_per_account_and_verified(self):
        self.assertTrue(subs.verify_client_state(self.ca_name, subs._client_state(self.ca_name)))
        self.assertFalse(subs.verify_client_state(self.ca_name, "guessed"))
        self.assertFalse(subs.verify_client_state(self.ca_name, ""))
        self.assertNotEqual(subs._client_state(self.ca_name), subs._client_state("other-account"))


class TestNotificationEndpoint(_Base):
    def setUp(self):
        super().setUp()
        frappe.db.set_value(
            "MSuite Connected Account", self.ca_name, "graph_subscription_id", _SUB_ID
        )
        frappe.db.commit()
        from msuite.api.v1 import graph_webhook

        self.webhook = graph_webhook

    def _post(self, body: dict | None, validation_token: str | None = None):
        frappe.form_dict = frappe._dict()
        if validation_token is not None:
            frappe.form_dict["validationToken"] = validation_token
        request = MagicMock()
        request.get_json.return_value = body
        request.get_data.return_value = json.dumps(body or {})
        frappe.local.request = request

    def _note(self, **overrides):
        note = {
            "subscriptionId": _SUB_ID,
            "clientState": subs._client_state(self.ca_name),
            "resource": "Users/x/Messages/y",
        }
        note.update(overrides)
        return note

    def test_validation_echoes_raw_token_as_plain_text(self):
        self._post(None, validation_token="Validation: Testing client application")
        resp = self.webhook.receive_graph_notification()

        self.assertEqual(resp.status_code, 200)
        # Must be the bare token — Graph rejects JSON wrapping.
        self.assertEqual(resp.get_data(as_text=True), "Validation: Testing client application")
        self.assertTrue(resp.mimetype.startswith("text/plain"))

    def test_notification_enqueues_delta_sync(self):
        self._post({"value": [self._note()]})
        with patch.object(frappe, "enqueue") as enq:
            resp = self.webhook.receive_graph_notification()

        self.assertEqual(resp.status_code, 202)
        enq.assert_called_once()
        self.assertEqual(enq.call_args.args[0], "msuite.api.v1.webhook.forward_webhook_job")
        self.assertEqual(
            enq.call_args.kwargs["endpoint"],
            "msuite_workspace.msuite_email.api.email_ingest.receive_inbound_push",
        )
        # The hint carries the mailbox only — never message content.
        self.assertEqual(enq.call_args.kwargs["payload"], {"mailbox": _MAILBOX})

    def test_forged_clientstate_is_dropped(self):
        self._post({"value": [self._note(clientState="wrong-secret")]})
        with patch.object(frappe, "enqueue") as enq:
            resp = self.webhook.receive_graph_notification()
        self.assertEqual(resp.status_code, 202)
        enq.assert_not_called()

    def test_unknown_subscription_is_dropped(self):
        self._post({"value": [self._note(subscriptionId="sub-nobody")]})
        with patch.object(frappe, "enqueue") as enq:
            self.webhook.receive_graph_notification()
        enq.assert_not_called()

    def test_batch_enqueues_one_sync_per_mailbox(self):
        """Ten changes in one mailbox is still one delta run."""
        self._post({"value": [self._note() for _ in range(10)]})
        with patch.object(frappe, "enqueue") as enq:
            self.webhook.receive_graph_notification()
        self.assertEqual(enq.call_count, 1)

    def test_garbage_body_does_not_500(self):
        self._post(None)
        resp = self.webhook.receive_graph_notification()
        self.assertEqual(resp.status_code, 202)

    def test_lifecycle_renews_and_missed_also_backfills(self):
        self._post({"value": [self._note(lifecycleEvent="reauthorizationRequired")]})
        with patch.object(subs, "renew_subscription") as renew, patch.object(frappe, "enqueue") as enq:
            self.webhook.receive_graph_lifecycle()
        renew.assert_called_once_with(self.ca_name)
        enq.assert_not_called()

        self._post({"value": [self._note(lifecycleEvent="missed")]})
        with patch.object(subs, "renew_subscription"), patch.object(frappe, "enqueue") as enq:
            self.webhook.receive_graph_lifecycle()
        # Graph dropped notifications — only a delta sweep recovers them.
        enq.assert_called_once()


class TestAttachmentFetch(IntegrationTestCase):
    def test_graph_returns_standard_base64(self):
        with patch.object(graph, "_request") as req:
            req.return_value = _resp(
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "quote.pdf",
                    "contentType": "application/pdf",
                    "size": 9,
                    "contentBytes": "aGVsbG8gcGRm",
                }
            )
            out = graph.fetch_attachment("tok", "msg-1", "att-1")

        import base64

        self.assertEqual(base64.b64decode(out["content"]), b"hello pdf")
        self.assertEqual(out["filename"], "quote.pdf")

    def test_graph_attachment_without_content_raises(self):
        """itemAttachment/referenceAttachment carry no bytes."""
        with patch.object(graph, "_request") as req:
            req.return_value = _resp(
                {"@odata.type": "#microsoft.graph.itemAttachment", "name": "forwarded.eml"}
            )
            with self.assertRaises(graph.GraphError):
                graph.fetch_attachment("tok", "msg-1", "att-1")

    def test_gmail_reencodes_urlsafe_to_standard_base64(self):
        """Gmail uses the URL-safe alphabet; the client must get one
        contract regardless of provider."""
        import base64

        from msuite.api.v1.gmail_relay import fetch_gmail_attachment

        raw = bytes([251, 239, 190]) + b"binary"  # encodes with - and _ in url-safe
        with patch("msuite.api.v1.gmail_relay.requests.get") as get:
            get.return_value = _resp(
                {"data": base64.urlsafe_b64encode(raw).decode(), "size": len(raw)}
            )
            out = fetch_gmail_attachment("tok", "m", "a")

        self.assertEqual(base64.b64decode(out["content"]), raw)
        self.assertNotIn("-", out["content"])
        self.assertNotIn("_", out["content"])
