# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
Unit tests for AWS Tenant Routing Sync Service (Control Plane).

Tests:
  - WABA route synchronization (active & suspended)
  - WABA route deletion
  - Client route synchronization (SMS & Email)
  - Client route deletion
  - Bulk tenant backfill (sync_all_tenants)
  - Doctype lifecycle hooks on MSuite Client and MSuite Connected Account
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe
from msuite.services import aws_routing_sync


class TestAWSRoutingSync(unittest.TestCase):
	def setUp(self):
		self._orig_db = getattr(frappe.local, "db", None)
		self._orig_conf = getattr(frappe.local, "conf", None)
		aws_routing_sync._routing_redis_client = None
		aws_routing_sync._routing_redis_cached_url = None
		frappe.local.db = MagicMock()
		frappe.local.conf = frappe._dict()
		self.mock_table = MagicMock()
		self.table_patch = patch.object(
			aws_routing_sync, "get_routing_table", return_value=self.mock_table
		)
		self.table_patch.start()

	def tearDown(self):
		self.table_patch.stop()
		aws_routing_sync._routing_redis_client = None
		aws_routing_sync._routing_redis_cached_url = None
		frappe.local.db = self._orig_db
		frappe.local.conf = self._orig_conf

	@patch("msuite.services.aws_routing_sync.get_decrypted_password", return_value="secret_xyz")
	def test_sync_waba_route_active(self, mock_secret):
		client_doc = MagicMock()
		client_doc.name = "CLI-001-DOC"
		client_doc.client_code = "CLI-001"
		client_doc.client_url = "https://tenant1.msuite.app/"
		client_doc.api_key = "key_abc"
		client_doc.status = "Active"

		result = aws_routing_sync.sync_waba_route("104857291", client_doc)
		self.assertTrue(result)

		self.mock_table.put_item.assert_called_once()
		call_args = self.mock_table.put_item.call_args[1]
		item = call_args["Item"]

		self.assertEqual(item["PK"], "ROUTING#WABA#104857291")
		self.assertEqual(item["SK"], "CONFIG")
		self.assertEqual(item["client_code"], "CLI-001")
		self.assertEqual(item["client_url"], "https://tenant1.msuite.app")
		self.assertEqual(item["api_key"], "key_abc")
		self.assertEqual(item["api_secret"], "secret_xyz")
		self.assertEqual(item["status"], "Active")
		self.assertIn("updated_at", item)

	@patch("msuite.services.aws_routing_sync.get_decrypted_password", return_value="secret_xyz")
	def test_sync_waba_route_suspended(self, mock_secret):
		client_doc = MagicMock()
		client_doc.name = "CLI-002-DOC"
		client_doc.client_code = "CLI-002"
		client_doc.client_url = "https://tenant2.msuite.app"
		client_doc.api_key = "key_def"
		client_doc.status = "Suspended"

		result = aws_routing_sync.sync_waba_route("987654321", client_doc)
		self.assertTrue(result)

		call_args = self.mock_table.put_item.call_args[1]
		item = call_args["Item"]
		self.assertEqual(item["PK"], "ROUTING#WABA#987654321")
		self.assertEqual(item["status"], "Suspended")

	@patch("msuite.services.aws_routing_sync.get_decrypted_password", return_value="secret_xyz")
	def test_sync_waba_route_client_suspended_precedence(self, mock_secret):
		client_doc = MagicMock()
		client_doc.name = "CLI-002-DOC"
		client_doc.client_code = "CLI-002"
		client_doc.client_url = "https://tenant2.msuite.app"
		client_doc.api_key = "key_def"
		client_doc.status = "Suspended"

		# Connected account passes status="Active", but client is Suspended
		result = aws_routing_sync.sync_waba_route("987654321", client_doc, status="Active")
		self.assertTrue(result)

		call_args = self.mock_table.put_item.call_args[1]
		item = call_args["Item"]
		self.assertEqual(item["PK"], "ROUTING#WABA#987654321")
		self.assertEqual(item["status"], "Suspended")

	def test_delete_waba_route(self):
		result = aws_routing_sync.delete_waba_route("104857291")
		self.assertTrue(result)

		self.mock_table.delete_item.assert_called_once_with(
			Key={"PK": "ROUTING#WABA#104857291", "SK": "CONFIG"}
		)

	@patch("msuite.services.aws_routing_sync.get_decrypted_password", return_value="client_secret")
	def test_sync_client_route(self, mock_secret):
		client_doc = MagicMock()
		client_doc.name = "CLI-SMS-DOC"
		client_doc.client_code = "CLI-SMS-001"
		client_doc.client_url = "https://sms-client.msuite.app"
		client_doc.api_key = "sms_key"
		client_doc.status = "Active"

		result = aws_routing_sync.sync_client_route(client_doc)
		self.assertTrue(result)

		self.mock_table.put_item.assert_called_once()
		item = self.mock_table.put_item.call_args[1]["Item"]

		self.assertEqual(item["PK"], "ROUTING#CLIENT#CLI-SMS-001")
		self.assertEqual(item["SK"], "CONFIG")
		self.assertEqual(item["client_code"], "CLI-SMS-001")
		self.assertEqual(item["client_url"], "https://sms-client.msuite.app")
		self.assertEqual(item["api_key"], "sms_key")
		self.assertEqual(item["api_secret"], "client_secret")
		self.assertEqual(item["status"], "Active")

	def test_delete_client_route(self):
		result = aws_routing_sync.delete_client_route("CLI-SMS-001")
		self.assertTrue(result)

		self.mock_table.delete_item.assert_called_once_with(
			Key={"PK": "ROUTING#CLIENT#CLI-SMS-001", "SK": "CONFIG"}
		)

	@patch("msuite.services.aws_routing_sync.sync_instagram_route")
	@patch("msuite.services.aws_routing_sync.sync_page_route")
	@patch("msuite.services.aws_routing_sync.sync_client_route")
	@patch("msuite.services.aws_routing_sync.sync_waba_route")
	@patch("frappe.get_doc")
	@patch("frappe.get_all")
	def test_sync_all_client_routes(
		self, mock_get_all, mock_get_doc, mock_sync_waba, mock_sync_client, mock_sync_page, mock_sync_ig
	):
		client_doc = MagicMock()
		client_doc.name = "CLI-001"
		client_doc.status = "Active"
		mock_get_doc.return_value = client_doc

		def get_all_side_effect(doctype, filters=None, fields=None, **kwargs):
			if doctype == "MSuite Connected Account":
				platform = (filters or {}).get("platform")
				if platform == "WhatsApp":
					return [frappe._dict({"name": "ACC-1", "account_id": "WABA-1", "status": "Active"})]
				elif platform == "Facebook":
					return [frappe._dict({"name": "ACC-FB", "account_id": "PAGE-1", "status": "Active"})]
				elif platform == "Instagram":
					return [frappe._dict({"name": "ACC-IG", "account_id": "IG-1", "status": "Suspended"})]
			return []

		mock_get_all.side_effect = get_all_side_effect

		aws_routing_sync.sync_all_client_routes("CLI-001")

		mock_sync_client.assert_called_once_with(client_doc)
		mock_sync_waba.assert_called_once_with("WABA-1", client_doc, status="Active")
		mock_sync_page.assert_called_once_with("PAGE-1", client_doc, status="Active")
		mock_sync_ig.assert_called_once_with("IG-1", client_doc, status="Suspended")

	@patch("msuite.services.aws_routing_sync.sync_instagram_route", return_value=True)
	@patch("msuite.services.aws_routing_sync.sync_page_route", return_value=True)
	@patch("msuite.services.aws_routing_sync.sync_client_route", return_value=True)
	@patch("msuite.services.aws_routing_sync.sync_waba_route", return_value=True)
	@patch("frappe.get_doc")
	@patch("frappe.get_all")
	def test_sync_all_tenants_bulk(
		self, mock_get_all, mock_get_doc, mock_sync_waba, mock_sync_client, mock_sync_page, mock_sync_ig
	):
		def get_all_side_effect(doctype, filters=None, fields=None, **kwargs):
			if doctype == "MSuite Client":
				return [
					frappe._dict({"name": "CLI-1", "client_code": "code-1", "status": "Active"}),
					frappe._dict({"name": "CLI-2", "client_code": "code-2", "status": "Active"}),
				]
			elif doctype == "MSuite Connected Account":
				client = (filters or {}).get("client")
				platform = (filters or {}).get("platform")
				if client == "CLI-1":
					if platform == "WhatsApp":
						return [frappe._dict({"name": "ACC-1", "account_id": "WABA-101", "status": "Active"})]
					elif platform == "Facebook":
						return [frappe._dict({"name": "ACC-FB1", "account_id": "PAGE-101", "status": "Active"})]
					elif platform == "Instagram":
						return [frappe._dict({"name": "ACC-IG1", "account_id": "IG-101", "status": "Active"})]
				elif client == "CLI-2":
					if platform == "WhatsApp":
						return [
							frappe._dict({"name": "ACC-2", "account_id": "WABA-201", "status": "Active"}),
							frappe._dict({"name": "ACC-3", "account_id": "WABA-202", "status": "Active"}),
						]
					elif platform == "Facebook":
						return []
					elif platform == "Instagram":
						return [frappe._dict({"name": "ACC-IG2", "account_id": "IG-201", "status": "Active"})]
			return []

		mock_get_all.side_effect = get_all_side_effect

		client_doc_1 = MagicMock(name="CLI-1", status="Active", client_code="code-1")
		client_doc_2 = MagicMock(name="CLI-2", status="Active", client_code="code-2")
		mock_get_doc.side_effect = [client_doc_1, client_doc_2]

		summary = aws_routing_sync.sync_all_tenants()

		self.assertEqual(summary["status"], "success")
		self.assertEqual(summary["synced_clients"], 2)
		self.assertEqual(summary["synced_wabas"], 3)
		self.assertEqual(summary["synced_pages"], 1)
		self.assertEqual(summary["synced_instagrams"], 2)
		self.assertEqual(summary["errors"], [])

	@patch("msuite.services.aws_routing_sync.get_routing_redis_client")
	@patch("msuite.services.aws_routing_sync.get_decrypted_password", return_value="secret_fb")
	def test_sync_page_route_active(self, mock_secret, mock_get_redis):
		mock_redis = MagicMock()
		mock_get_redis.return_value = mock_redis

		client_doc = MagicMock()
		client_doc.name = "CLI-FB-DOC"
		client_doc.client_code = "CLI-FB"
		client_doc.client_url = "https://fb.msuite.app/"
		client_doc.api_key = "fb_api_key"
		client_doc.status = "Active"

		result = aws_routing_sync.sync_page_route("PAGE-101", client_doc)
		self.assertTrue(result)

		self.mock_table.put_item.assert_called_once()
		call_args = self.mock_table.put_item.call_args[1]
		item = call_args["Item"]

		self.assertEqual(item["PK"], "ROUTING#PAGE#PAGE-101")
		self.assertEqual(item["SK"], "CONFIG")
		self.assertEqual(item["client_code"], "CLI-FB")
		self.assertEqual(item["client_url"], "https://fb.msuite.app")
		self.assertEqual(item["api_key"], "fb_api_key")
		self.assertEqual(item["api_secret"], "secret_fb")
		self.assertEqual(item["status"], "Active")
		self.assertIn("updated_at", item)

		mock_redis.set.assert_called_once()
		redis_args = mock_redis.set.call_args
		key = redis_args[0][0]
		val_str = redis_args[0][1]
		ex = redis_args[1].get("ex")

		self.assertEqual(key, "routing:page:PAGE-101")
		self.assertEqual(ex, aws_routing_sync.DEFAULT_ROUTING_TTL_SECONDS)
		self.assertIn('"client_code": "CLI-FB"', val_str)

	@patch("msuite.services.aws_routing_sync.get_decrypted_password", return_value="secret_fb")
	def test_sync_page_route_suspended(self, mock_secret):
		client_doc = MagicMock()
		client_doc.name = "CLI-FB-DOC"
		client_doc.client_code = "CLI-FB"
		client_doc.client_url = "https://fb.msuite.app"
		client_doc.api_key = "fb_api_key"
		client_doc.status = "Suspended"

		result = aws_routing_sync.sync_page_route("PAGE-102", client_doc)
		self.assertTrue(result)

		call_args = self.mock_table.put_item.call_args[1]
		item = call_args["Item"]
		self.assertEqual(item["PK"], "ROUTING#PAGE#PAGE-102")
		self.assertEqual(item["status"], "Suspended")

	@patch("msuite.services.aws_routing_sync.get_decrypted_password", return_value="secret_fb")
	def test_sync_page_route_client_suspended_precedence(self, mock_secret):
		client_doc = MagicMock()
		client_doc.name = "CLI-FB-DOC"
		client_doc.client_code = "CLI-FB"
		client_doc.client_url = "https://fb.msuite.app"
		client_doc.api_key = "fb_api_key"
		client_doc.status = "Suspended"

		# Connected account passes status="Active", but client is Suspended
		result = aws_routing_sync.sync_page_route("PAGE-102", client_doc, status="Active")
		self.assertTrue(result)

		call_args = self.mock_table.put_item.call_args[1]
		item = call_args["Item"]
		self.assertEqual(item["PK"], "ROUTING#PAGE#PAGE-102")
		self.assertEqual(item["status"], "Suspended")

	@patch("msuite.services.aws_routing_sync.get_routing_redis_client")
	def test_delete_page_route(self, mock_get_redis):
		mock_redis = MagicMock()
		mock_get_redis.return_value = mock_redis

		result = aws_routing_sync.delete_page_route("PAGE-101")
		self.assertTrue(result)

		self.mock_table.delete_item.assert_called_once_with(
			Key={"PK": "ROUTING#PAGE#PAGE-101", "SK": "CONFIG"}
		)
		mock_redis.delete.assert_called_once_with("routing:page:PAGE-101")

	@patch("msuite.services.aws_routing_sync.get_routing_redis_client")
	@patch("msuite.services.aws_routing_sync.get_decrypted_password", return_value="secret_ig")
	def test_sync_instagram_route_active(self, mock_secret, mock_get_redis):
		mock_redis = MagicMock()
		mock_get_redis.return_value = mock_redis

		client_doc = MagicMock()
		client_doc.name = "CLI-IG-DOC"
		client_doc.client_code = "CLI-IG"
		client_doc.client_url = "https://ig.msuite.app/"
		client_doc.api_key = "ig_api_key"
		client_doc.status = "Active"

		result = aws_routing_sync.sync_instagram_route("IG-202", client_doc)
		self.assertTrue(result)

		self.mock_table.put_item.assert_called_once()
		call_args = self.mock_table.put_item.call_args[1]
		item = call_args["Item"]

		self.assertEqual(item["PK"], "ROUTING#INSTAGRAM#IG-202")
		self.assertEqual(item["SK"], "CONFIG")
		self.assertEqual(item["client_code"], "CLI-IG")
		self.assertEqual(item["client_url"], "https://ig.msuite.app")
		self.assertEqual(item["api_key"], "ig_api_key")
		self.assertEqual(item["api_secret"], "secret_ig")
		self.assertEqual(item["status"], "Active")
		self.assertIn("updated_at", item)

		mock_redis.set.assert_called_once()
		redis_args = mock_redis.set.call_args
		key = redis_args[0][0]
		val_str = redis_args[0][1]
		ex = redis_args[1].get("ex")

		self.assertEqual(key, "routing:instagram:IG-202")
		self.assertEqual(ex, aws_routing_sync.DEFAULT_ROUTING_TTL_SECONDS)
		self.assertIn('"client_code": "CLI-IG"', val_str)

	@patch("msuite.services.aws_routing_sync.get_decrypted_password", return_value="secret_ig")
	def test_sync_instagram_route_suspended(self, mock_secret):
		client_doc = MagicMock()
		client_doc.name = "CLI-IG-DOC"
		client_doc.client_code = "CLI-IG"
		client_doc.client_url = "https://ig.msuite.app"
		client_doc.api_key = "ig_api_key"
		client_doc.status = "Suspended"

		result = aws_routing_sync.sync_instagram_route("IG-203", client_doc)
		self.assertTrue(result)

		call_args = self.mock_table.put_item.call_args[1]
		item = call_args["Item"]
		self.assertEqual(item["PK"], "ROUTING#INSTAGRAM#IG-203")
		self.assertEqual(item["status"], "Suspended")

	@patch("msuite.services.aws_routing_sync.get_decrypted_password", return_value="secret_ig")
	def test_sync_instagram_route_client_suspended_precedence(self, mock_secret):
		client_doc = MagicMock()
		client_doc.name = "CLI-IG-DOC"
		client_doc.client_code = "CLI-IG"
		client_doc.client_url = "https://ig.msuite.app"
		client_doc.api_key = "ig_api_key"
		client_doc.status = "Suspended"

		# Connected account passes status="Active", but client is Suspended
		result = aws_routing_sync.sync_instagram_route("IG-203", client_doc, status="Active")
		self.assertTrue(result)

		call_args = self.mock_table.put_item.call_args[1]
		item = call_args["Item"]
		self.assertEqual(item["PK"], "ROUTING#INSTAGRAM#IG-203")
		self.assertEqual(item["status"], "Suspended")

	@patch("msuite.services.aws_routing_sync.get_routing_redis_client")
	def test_delete_instagram_route(self, mock_get_redis):
		mock_redis = MagicMock()
		mock_get_redis.return_value = mock_redis

		result = aws_routing_sync.delete_instagram_route("IG-202")
		self.assertTrue(result)

		self.mock_table.delete_item.assert_called_once_with(
			Key={"PK": "ROUTING#INSTAGRAM#IG-202", "SK": "CONFIG"}
		)
		mock_redis.delete.assert_called_once_with("routing:instagram:IG-202")

	@patch("msuite.services.aws_routing_sync.sync_page_route")
	def test_connected_account_on_update_facebook(self, mock_sync):
		from msuite.msuite_client.doctype.msuite_connected_account.msuite_connected_account import (
			MSuiteConnectedAccount,
		)

		doc = MSuiteConnectedAccount.__new__(MSuiteConnectedAccount)
		doc.platform = "Facebook"
		doc.account_id = "PAGE-12345"
		doc.client = "CLI-001"
		doc.status = "Active"

		doc.on_update()
		mock_sync.assert_called_once_with("PAGE-12345", "CLI-001", status="Active")

	@patch("msuite.services.aws_routing_sync.delete_page_route")
	def test_connected_account_on_trash_facebook(self, mock_delete):
		from msuite.msuite_client.doctype.msuite_connected_account.msuite_connected_account import (
			MSuiteConnectedAccount,
		)

		doc = MSuiteConnectedAccount.__new__(MSuiteConnectedAccount)
		doc.platform = "Facebook"
		doc.account_id = "PAGE-12345"

		doc.on_trash()
		mock_delete.assert_called_once_with("PAGE-12345")

	@patch("msuite.services.aws_routing_sync.sync_instagram_route")
	def test_connected_account_on_update_instagram(self, mock_sync):
		from msuite.msuite_client.doctype.msuite_connected_account.msuite_connected_account import (
			MSuiteConnectedAccount,
		)

		doc = MSuiteConnectedAccount.__new__(MSuiteConnectedAccount)
		doc.platform = "Instagram"
		doc.account_id = "IG-12345"
		doc.client = "CLI-001"
		doc.status = "Active"

		doc.on_update()
		mock_sync.assert_called_once_with("IG-12345", "CLI-001", status="Active")

	@patch("msuite.services.aws_routing_sync.delete_instagram_route")
	def test_connected_account_on_trash_instagram(self, mock_delete):
		from msuite.msuite_client.doctype.msuite_connected_account.msuite_connected_account import (
			MSuiteConnectedAccount,
		)

		doc = MSuiteConnectedAccount.__new__(MSuiteConnectedAccount)
		doc.platform = "Instagram"
		doc.account_id = "IG-12345"

		doc.on_trash()
		mock_delete.assert_called_once_with("IG-12345")

	@patch("msuite.services.aws_routing_sync.sync_waba_route")
	def test_connected_account_on_update_hook(self, mock_sync):
		from msuite.msuite_client.doctype.msuite_connected_account.msuite_connected_account import (
			MSuiteConnectedAccount,
		)

		doc = MSuiteConnectedAccount.__new__(MSuiteConnectedAccount)
		doc.platform = "WhatsApp"
		doc.account_id = "WABA-12345"
		doc.client = "CLI-001"
		doc.status = "Active"

		doc.on_update()
		mock_sync.assert_called_once_with("WABA-12345", "CLI-001", status="Active")

	@patch("msuite.services.aws_routing_sync.delete_waba_route")
	def test_connected_account_on_trash_hook(self, mock_delete):
		from msuite.msuite_client.doctype.msuite_connected_account.msuite_connected_account import (
			MSuiteConnectedAccount,
		)

		doc = MSuiteConnectedAccount.__new__(MSuiteConnectedAccount)
		doc.platform = "WhatsApp"
		doc.account_id = "WABA-12345"

		doc.on_trash()
		mock_delete.assert_called_once_with("WABA-12345")

	@patch("msuite.services.aws_routing_sync.sync_all_client_routes")
	def test_client_on_update_hook(self, mock_sync_all):
		from msuite.msuite_client.doctype.msuite_client.msuite_client import MSuiteClient

		doc = MSuiteClient.__new__(MSuiteClient)
		doc.name = "CLI-001-NAME"

		doc.on_update()
		mock_sync_all.assert_called_once_with("CLI-001-NAME")

	@patch("msuite.services.aws_routing_sync.delete_client_route")
	def test_client_on_trash_hook(self, mock_delete_client):
		from msuite.msuite_client.doctype.msuite_client.msuite_client import MSuiteClient

		doc = MSuiteClient.__new__(MSuiteClient)
		doc.client_code = "CLI-CODE-123"

		doc.on_trash()
		mock_delete_client.assert_called_once_with("CLI-CODE-123")

	@patch("msuite.services.aws_routing_sync.frappe.get_all")
	def test_get_redis_endpoint_url_from_child_table(self, mock_get_all):
		frappe.local.db.get_single_value.return_value = 1
		endpoint_row = MagicMock()
		endpoint_row.redis_url = "rediss://routing-redis.aws.com:6379/0"
		mock_get_all.return_value = [endpoint_row]

		url = aws_routing_sync.get_redis_endpoint_url("Tenant Routing")
		self.assertEqual(url, "rediss://routing-redis.aws.com:6379/0")
		mock_get_all.assert_called_once_with(
			"MSuite Redis Endpoint",
			filters={"parent": "MSuite AWS Settings", "purpose": "Tenant Routing", "enabled": 1},
			fields=["redis_url"],
			limit=1,
		)

	def test_get_redis_endpoint_url_from_conf(self):
		frappe.local.db.get_single_value.return_value = 0
		frappe.local.conf = frappe._dict({"tenant_routing_redis_url": "rediss://conf-redis:6379/0"})
		url = aws_routing_sync.get_redis_endpoint_url("Tenant Routing")
		self.assertEqual(url, "rediss://conf-redis:6379/0")

	@patch("msuite.services.aws_routing_sync.get_routing_redis_client")
	@patch("msuite.services.aws_routing_sync.get_decrypted_password", return_value="secret_xyz")
	def test_sync_waba_route_writes_to_redis(self, mock_secret, mock_get_redis):
		mock_redis = MagicMock()
		mock_get_redis.return_value = mock_redis

		client_doc = MagicMock()
		client_doc.name = "CLI-001-DOC"
		client_doc.client_code = "CLI-001"
		client_doc.client_url = "https://tenant1.msuite.app/"
		client_doc.api_key = "key_abc"
		client_doc.status = "Active"

		result = aws_routing_sync.sync_waba_route("104857291", client_doc)
		self.assertTrue(result)

		mock_redis.set.assert_called_once()
		call_args = mock_redis.set.call_args
		key = call_args[0][0]
		val_str = call_args[0][1]
		ex = call_args[1].get("ex")

		self.assertEqual(key, "routing:waba:104857291")
		self.assertEqual(ex, aws_routing_sync.DEFAULT_ROUTING_TTL_SECONDS)
		self.assertIn('"client_code": "CLI-001"', val_str)
		self.assertIn('"client_url": "https://tenant1.msuite.app"', val_str)

	@patch("msuite.services.aws_routing_sync.get_routing_redis_client")
	def test_delete_waba_route_evicts_from_redis(self, mock_get_redis):
		mock_redis = MagicMock()
		mock_get_redis.return_value = mock_redis

		result = aws_routing_sync.delete_waba_route("104857291")
		self.assertTrue(result)

		mock_redis.delete.assert_called_once_with("routing:waba:104857291")

	@patch("msuite.services.aws_routing_sync.get_routing_redis_client")
	@patch("msuite.services.aws_routing_sync.get_decrypted_password", return_value="secret_xyz")
	def test_sync_client_route_writes_to_redis(self, mock_secret, mock_get_redis):
		mock_redis = MagicMock()
		mock_get_redis.return_value = mock_redis

		client_doc = MagicMock()
		client_doc.name = "CLI-001-DOC"
		client_doc.client_code = "CLI-001"
		client_doc.client_url = "https://tenant1.msuite.app/"
		client_doc.api_key = "key_abc"
		client_doc.status = "Active"

		result = aws_routing_sync.sync_client_route(client_doc)
		self.assertTrue(result)

		mock_redis.set.assert_called_once()
		key = mock_redis.set.call_args[0][0]
		self.assertEqual(key, "routing:client:CLI-001")

	@patch("msuite.services.aws_routing_sync.get_routing_redis_client")
	def test_delete_client_route_evicts_from_redis(self, mock_get_redis):
		mock_redis = MagicMock()
		mock_get_redis.return_value = mock_redis

		result = aws_routing_sync.delete_client_route("CLI-001")
		self.assertTrue(result)

		mock_redis.delete.assert_called_once_with("routing:client:CLI-001")

	@patch("msuite.services.aws_routing_sync.get_routing_redis_client")
	@patch("msuite.services.aws_routing_sync.get_decrypted_password", return_value="secret_xyz")
	def test_redis_write_fail_soft(self, mock_secret, mock_get_redis):
		mock_redis = MagicMock()
		mock_redis.set.side_effect = Exception("Redis connection timed out")
		mock_get_redis.return_value = mock_redis

		client_doc = MagicMock()
		client_doc.name = "CLI-001-DOC"
		client_doc.client_code = "CLI-001"
		client_doc.client_url = "https://tenant1.msuite.app/"
		client_doc.api_key = "key_abc"
		client_doc.status = "Active"

		result = aws_routing_sync.sync_waba_route("104857291", client_doc)
		self.assertTrue(result)
		self.mock_table.put_item.assert_called_once()

	@patch("redis.Redis.from_url")
	@patch("msuite.services.aws_routing_sync.get_redis_endpoint_url")
	def test_get_routing_redis_client_reconnects_on_url_change(self, mock_get_url, mock_from_url):
		mock_get_url.return_value = "rediss://redis-cluster-a:6379/0"
		mock_client_a = MagicMock()
		mock_client_b = MagicMock()
		mock_from_url.side_effect = [mock_client_a, mock_client_b]

		# 1. First call initializes client A
		client1 = aws_routing_sync.get_routing_redis_client()
		self.assertEqual(client1, mock_client_a)
		self.assertEqual(mock_from_url.call_count, 1)

		# 2. Second call with same URL reuses client A without calling from_url again
		client2 = aws_routing_sync.get_routing_redis_client()
		self.assertEqual(client2, mock_client_a)
		self.assertEqual(mock_from_url.call_count, 1)

		# 3. URL changes -> closes client A and connects to client B
		mock_get_url.return_value = "rediss://redis-cluster-b:6379/0"
		client3 = aws_routing_sync.get_routing_redis_client()
		self.assertEqual(client3, mock_client_b)
		self.assertEqual(mock_from_url.call_count, 2)
		mock_client_a.close.assert_called_once()


if __name__ == "__main__":
	unittest.main()


