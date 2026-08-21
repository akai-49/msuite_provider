"""
Webhook relay for the AWS bulk-messaging execution plane
(`api/v1/bulk_relay.py`).

What this proves:

  • The routing cache actually caches — one DB read per client_code, not one
    per delivery receipt. A large blast produces one receipt per recipient,
    so this sits in a genuinely hot path.
  • **Misses are cached too.** The relay endpoint is public; without this a
    stale webhook URL or a probe reaches the database on every request.
  • Deactivating a client takes effect immediately, not after the TTL. A
    revoked tenant must stop receiving relayed webhooks at once, and the
    `on_update` hook is what makes that true.
  • An unknown route answers 200, not an error — AWS would otherwise redrive
    a stale webhook URL forever.
  • Auth fails closed when no relay token is configured.

No network I/O — the forwarder is mocked.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite.api.v1 import bulk_relay
from msuite.utils.cache import get_relay_route_cache, invalidate_relay_route_cache

PREFIX = "ZZRelay"
CODE = "zzrelay-client-1"


def _client(status: str = "Active") -> str:
	name = f"{PREFIX}-CLIENT"
	if frappe.db.exists("MSuite Client", name):
		frappe.db.set_value("MSuite Client", name, "status", status)
		return name

	customer = frappe.db.get_value("Customer", {}, "name")
	if not customer:
		raise RuntimeError("test needs one Customer on the provider site")

	doc = frappe.get_doc(
		{
			"doctype": "MSuite Client",
			"client_name": name,
			"customer": customer,
			"client_code": CODE,
			"client_url": "https://client.zz.test",
			"status": status,
			"api_key": "zz-key",
			"api_secret": "zz-secret",
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


class TestRelayRouteCache(IntegrationTestCase):
	def setUp(self):
		invalidate_relay_route_cache(CODE)

	def tearDown(self):
		invalidate_relay_route_cache(CODE)
		frappe.db.rollback()
		frappe.db.sql("DELETE FROM `tabMSuite Client` WHERE name LIKE %s", (f"{PREFIX}%",))
		frappe.db.commit()

	def test_second_lookup_does_not_touch_the_database(self):
		_client()
		self.assertEqual(bulk_relay._client_name_for(CODE), f"{PREFIX}-CLIENT")

		# The cache is warm now; a DB read here would mean it is not working.
		with patch.object(frappe.db, "get_value", side_effect=AssertionError("hit the DB")) as db:
			self.assertEqual(bulk_relay._client_name_for(CODE), f"{PREFIX}-CLIENT")
			self.assertEqual(db.call_count, 0)

	def test_a_miss_is_cached_so_probes_do_not_reach_the_database(self):
		"""The relay endpoint is public. An unknown code must cost one query,
		not one per request."""
		self.assertIsNone(bulk_relay._client_name_for("no-such-client"))
		self.assertEqual(get_relay_route_cache("no-such-client"), "")

		with patch.object(frappe.db, "get_value", side_effect=AssertionError("hit the DB")):
			self.assertIsNone(bulk_relay._client_name_for("no-such-client"))

		invalidate_relay_route_cache("no-such-client")

	def test_only_the_doc_name_is_cached_never_a_secret(self):
		_client()
		bulk_relay._client_name_for(CODE)
		cached = get_relay_route_cache(CODE)

		self.assertEqual(cached, f"{PREFIX}-CLIENT")
		for secret in ("zz-secret", "zz-key", "https://client.zz.test"):
			self.assertNotIn(secret, str(cached))

	def test_deactivating_a_client_takes_effect_immediately(self):
		"""Not after the TTL. A revoked tenant stops receiving webhooks now."""
		name = _client()
		bulk_relay._client_name_for(CODE)
		self.assertIsNotNone(get_relay_route_cache(CODE))

		doc = frappe.get_doc("MSuite Client", name)
		doc.status = "Suspended"
		doc.flags.ignore_permissions = True
		doc.save()

		self.assertIsNone(get_relay_route_cache(CODE), "on_update must clear the route cache")
		client_doc, _ = bulk_relay._resolve_route(f"{CODE}:SMS-1")
		self.assertIsNone(client_doc, "a suspended client must not resolve")

	def test_route_splits_into_client_and_account(self):
		_client()
		client_doc, account = bulk_relay._resolve_route(f"{CODE}:SMS-Primary")
		self.assertEqual(client_doc.client_code, CODE)
		self.assertEqual(account, "SMS-Primary")

	def test_a_malformed_route_resolves_to_nothing(self):
		for route in ("", "no-colon", ":", "unknown:acct"):
			self.assertEqual(bulk_relay._resolve_route(route), (None, None), route)


class TestRelayEndpoint(IntegrationTestCase):
	def tearDown(self):
		invalidate_relay_route_cache(CODE)
		frappe.db.rollback()
		frappe.db.sql("DELETE FROM `tabMSuite Client` WHERE name LIKE %s", (f"{PREFIX}%",))
		frappe.db.commit()

	def test_auth_fails_closed_with_no_token_configured(self):
		with patch.object(bulk_relay, "_relay_authorised", wraps=bulk_relay._relay_authorised):
			self.assertFalse(bulk_relay._relay_authorised({}))
			self.assertFalse(bulk_relay._relay_authorised({"x-msuite-relay-token": "anything"}))

	def test_an_unknown_route_answers_success_not_error(self):
		"""AWS redrives an error. A stale webhook URL is not worth retrying
		forever, so it is acknowledged and logged instead."""
		with patch.object(bulk_relay, "_relay_authorised", return_value=True), patch.object(
			frappe, "request", frappe._dict(headers={"X-MSuite-Relay-Provider": "msg91", "X-MSuite-Relay-Route": "nope:x"})
		):
			out = bulk_relay.relay_webhook()

		self.assertEqual(out["status"], "success")
		self.assertFalse(out["data"]["forwarded"])

	def test_an_unmapped_provider_is_refused(self):
		with patch.object(bulk_relay, "_relay_authorised", return_value=True), patch.object(
			frappe, "request", frappe._dict(headers={"X-MSuite-Relay-Provider": "carrier-pigeon"})
		):
			out = bulk_relay.relay_webhook()

		self.assertEqual(out["status"], "error")
		self.assertEqual(out["error_code"], "UNKNOWN_PROVIDER")

	def test_an_inbound_sms_goes_to_receive_inbound_not_receive_dlr(self):
		"""Two different client functions behind one gateway URL. Routing a
		reply to the DLR handler drops it silently — and a reply can carry
		STOP, so it also loses the opt-out."""
		self.assertEqual(
			bulk_relay._CLIENT_ENDPOINT[("twilio", "chat")],
			"msuite_workspace.sms.api.v1.webhook.receive_inbound",
		)
		self.assertEqual(
			bulk_relay._CLIENT_ENDPOINT[("twilio", "dlr")],
			"msuite_workspace.sms.api.v1.webhook.receive_dlr",
		)
		self.assertEqual(
			bulk_relay._CLIENT_ENDPOINT[("msg91", "chat")],
			"msuite_workspace.sms.api.v1.webhook.receive_inbound",
		)

	def test_meta_uses_one_endpoint_for_both_kinds(self):
		"""`feed_whatsapp_payload` already branches on messages[] vs
		statuses[] internally, and one payload can carry both."""
		self.assertEqual(
			bulk_relay._CLIENT_ENDPOINT[("meta", "chat")],
			bulk_relay._CLIENT_ENDPOINT[("meta", "dlr")],
		)

	def test_the_kind_header_picks_the_endpoint(self):
		name = _client()
		captured = {}

		def fake_forward(client_doc, endpoint, raw_body, source_headers, query="", timeout=15):
			captured["endpoint"] = endpoint
			return True, ""

		with patch.object(bulk_relay, "_relay_authorised", return_value=True), patch(
			"msuite.services.client_service.post_raw_to_client", side_effect=fake_forward
		), patch.object(
			frappe,
			"request",
			frappe._dict(
				headers={
					"X-MSuite-Relay-Provider": "twilio",
					"X-MSuite-Relay-Route": f"{CODE}:SMS-1",
					"X-MSuite-Relay-Kind": "chat",
				},
				get_data=lambda: b"Body=STOP",
			),
		):
			out = bulk_relay.relay_webhook()

		self.assertEqual(out["data"]["kind"], "chat")
		self.assertEqual(captured["endpoint"], "msuite_workspace.sms.api.v1.webhook.receive_inbound")

	def test_a_missing_kind_header_defaults_to_dlr(self):
		"""Back-compat: an older Lambda that does not send the header must
		keep working, and DLR is the safe default (it is idempotent)."""
		name = _client()
		captured = {}

		def fake_forward(client_doc, endpoint, raw_body, source_headers, query="", timeout=15):
			captured["endpoint"] = endpoint
			return True, ""

		with patch.object(bulk_relay, "_relay_authorised", return_value=True), patch(
			"msuite.services.client_service.post_raw_to_client", side_effect=fake_forward
		), patch.object(
			frappe,
			"request",
			frappe._dict(
				headers={"X-MSuite-Relay-Provider": "twilio", "X-MSuite-Relay-Route": f"{CODE}:SMS-1"},
				get_data=lambda: b"MessageStatus=delivered",
			),
		):
			bulk_relay.relay_webhook()

		self.assertEqual(captured["endpoint"], "msuite_workspace.sms.api.v1.webhook.receive_dlr")

	def test_the_body_is_forwarded_as_bytes_not_json(self):
		"""The property Standard-path tracking depends on: the client
		recomputes an HMAC over exactly these bytes."""
		name = _client()
		raw = b'{"MessageSid":"SM1","MessageStatus":"delivered"}   '
		captured = {}

		def fake_forward(client_doc, endpoint, raw_body, source_headers, query="", timeout=15):
			captured.update(body=raw_body, endpoint=endpoint, query=query, headers=source_headers)
			return True, ""

		with patch.object(bulk_relay, "_relay_authorised", return_value=True), patch(
			"msuite.services.client_service.post_raw_to_client", side_effect=fake_forward
		), patch.object(
			frappe,
			"request",
			frappe._dict(
				headers={
					"X-MSuite-Relay-Provider": "twilio",
					"X-MSuite-Relay-Route": f"{CODE}:SMS-1",
					"X-Twilio-Signature": "sig123",
				},
				get_data=lambda: raw,
			),
		):
			out = bulk_relay.relay_webhook()

		self.assertEqual(out["status"], "success")
		self.assertEqual(captured["body"], raw, "body was altered in transit")
		self.assertEqual(captured["query"], "account=SMS-1")
		self.assertEqual(captured["endpoint"], "msuite_workspace.sms.api.v1.webhook.receive_dlr")
		self.assertEqual(captured["headers"]["X-Twilio-Signature"], "sig123")
