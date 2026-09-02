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
		self.mock_table = MagicMock()
		self.table_patch = patch.object(
			aws_routing_sync, "get_routing_table", return_value=self.mock_table
		)
		self.table_patch.start()

	def tearDown(self):
		self.table_patch.stop()

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

	@patch("msuite.services.aws_routing_sync.sync_client_route")
	@patch("msuite.services.aws_routing_sync.sync_waba_route")
	@patch("frappe.get_doc")
	@patch("frappe.get_all")
	def test_sync_all_client_routes(self, mock_get_all, mock_get_doc, mock_sync_waba, mock_sync_client):
		client_doc = MagicMock()
		client_doc.name = "CLI-001"
		client_doc.status = "Active"
		mock_get_doc.return_value = client_doc

		mock_get_all.return_value = [
			frappe._dict({"name": "ACC-1", "account_id": "WABA-1", "status": "Active"}),
			frappe._dict({"name": "ACC-2", "account_id": "WABA-2", "status": "Suspended"}),
		]

		aws_routing_sync.sync_all_client_routes("CLI-001")

		mock_sync_client.assert_called_once_with(client_doc)
		self.assertEqual(mock_sync_waba.call_count, 2)
		mock_sync_waba.assert_any_call("WABA-1", client_doc, status="Active")
		mock_sync_waba.assert_any_call("WABA-2", client_doc, status="Suspended")

	@patch("msuite.services.aws_routing_sync.sync_client_route", return_value=True)
	@patch("msuite.services.aws_routing_sync.sync_waba_route", return_value=True)
	@patch("frappe.get_doc")
	@patch("frappe.get_all")
	def test_sync_all_tenants_bulk(self, mock_get_all, mock_get_doc, mock_sync_waba, mock_sync_client):
		mock_get_all.side_effect = [
			# 1. Clients
			[
				frappe._dict({"name": "CLI-1", "client_code": "code-1", "status": "Active"}),
				frappe._dict({"name": "CLI-2", "client_code": "code-2", "status": "Active"}),
			],
			# 2. WhatsApp accounts for CLI-1
			[
				frappe._dict({"name": "ACC-1", "account_id": "WABA-101", "status": "Active"}),
			],
			# 3. WhatsApp accounts for CLI-2
			[
				frappe._dict({"name": "ACC-2", "account_id": "WABA-201", "status": "Active"}),
				frappe._dict({"name": "ACC-3", "account_id": "WABA-202", "status": "Active"}),
			],
		]

		client_doc_1 = MagicMock(name="CLI-1", status="Active", client_code="code-1")
		client_doc_2 = MagicMock(name="CLI-2", status="Active", client_code="code-2")
		mock_get_doc.side_effect = [client_doc_1, client_doc_2]

		summary = aws_routing_sync.sync_all_tenants()

		self.assertEqual(summary["status"], "success")
		self.assertEqual(summary["synced_clients"], 2)
		self.assertEqual(summary["synced_wabas"], 3)
		self.assertEqual(summary["errors"], [])

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


if __name__ == "__main__":
	unittest.main()
