# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
AWS Tenant Routing Sync Service (Control Plane).

Synchronizes tenant routing metadata (WABA ID -> Client URL & credentials)
from the Provider to AWS DynamoDB (msuite-tenant-routing-${Stage}).
Enables direct-to-client webhook dispatch on the AWS execution plane.
"""

from __future__ import annotations

import os
import frappe
from frappe import _
from frappe.utils.password import get_decrypted_password

from msuite.constants import MSUITE_LOGGER_NAME

logger = frappe.logger(MSUITE_LOGGER_NAME)


def get_routing_table():
	"""Connect to DynamoDB tenant routing table using MSuite AWS Settings."""
	try:
		enabled = frappe.db.get_single_value("MSuite AWS Settings", "enabled")
		if not enabled:
			return None

		from msuite.msuite_client.doctype.msuite_aws_settings.msuite_aws_settings import boto_session

		session = boto_session()
		dynamodb = session.resource("dynamodb")

		table_name = (
			frappe.conf.get("msuite_tenant_routing_table")
			or os.environ.get("ROUTING_TABLE")
			or frappe.db.get_single_value("MSuite AWS Settings", "routing_table_name")
			or f"msuite-tenant-routing-{frappe.conf.get('stage', 'dev')}"
		)
		return dynamodb.Table(table_name)
	except Exception as e:
		logger.warning(f"Failed to initialize AWS DynamoDB routing table: {e}")
		return None


def sync_waba_route(waba_id: str, client_doc_or_name, status: str | None = None) -> bool:
	"""Persist or update tenant routing for a WhatsApp Business Account in DynamoDB."""
	if not waba_id:
		return False

	table = get_routing_table()
	if table is None:
		return False

	try:
		if isinstance(client_doc_or_name, str):
			client_doc = frappe.get_doc("MSuite Client", client_doc_or_name)
		else:
			client_doc = client_doc_or_name

		api_secret = get_decrypted_password("MSuite Client", client_doc.name, "api_secret") or ""
		effective_status = status or ("Active" if client_doc.status == "Active" else "Suspended")

		item = {
			"PK": f"ROUTING#WABA#{waba_id}",
			"SK": "CONFIG",
			"client_code": client_doc.client_code or "",
			"client_url": (client_doc.client_url or "").rstrip("/"),
			"api_key": client_doc.api_key or "",
			"api_secret": api_secret,
			"status": effective_status,
			"updated_at": frappe.utils.now_datetime().isoformat(),
		}
		table.put_item(Item=item)
		logger.info(f"Synchronized DynamoDB route for WABA {waba_id} -> {client_doc.client_code} ({effective_status})")
		return True
	except Exception as e:
		logger.error(f"Failed to sync WABA {waba_id} to DynamoDB: {e}")
		return False


def delete_waba_route(waba_id: str) -> bool:
	"""Remove a WABA routing entry from DynamoDB."""
	if not waba_id:
		return False

	table = get_routing_table()
	if table is None:
		return False

	try:
		table.delete_item(Key={"PK": f"ROUTING#WABA#{waba_id}", "SK": "CONFIG"})
		logger.info(f"Deleted DynamoDB route for WABA {waba_id}")
		return True
	except Exception as e:
		logger.error(f"Failed to delete WABA {waba_id} from DynamoDB: {e}")
		return False


def sync_client_route(client_doc_or_name, status: str | None = None) -> bool:
	"""Persist or update tenant routing for a Client (used by SMS & Email) in DynamoDB."""
	table = get_routing_table()
	if table is None:
		return False

	try:
		if isinstance(client_doc_or_name, str):
			client_doc = frappe.get_doc("MSuite Client", client_doc_or_name)
		else:
			client_doc = client_doc_or_name

		if not client_doc.client_code:
			return False

		api_secret = get_decrypted_password("MSuite Client", client_doc.name, "api_secret") or ""
		effective_status = status or ("Active" if client_doc.status == "Active" else "Suspended")

		item = {
			"PK": f"ROUTING#CLIENT#{client_doc.client_code}",
			"SK": "CONFIG",
			"client_code": client_doc.client_code,
			"client_url": (client_doc.client_url or "").rstrip("/"),
			"api_key": client_doc.api_key or "",
			"api_secret": api_secret,
			"status": effective_status,
			"updated_at": frappe.utils.now_datetime().isoformat(),
		}
		table.put_item(Item=item)
		logger.info(f"Synchronized DynamoDB route for Client {client_doc.client_code} ({effective_status})")
		return True
	except Exception as e:
		logger.error(f"Failed to sync Client route to DynamoDB: {e}")
		return False


def delete_client_route(client_code: str) -> bool:
	"""Remove a Client routing entry from DynamoDB."""
	if not client_code:
		return False

	table = get_routing_table()
	if table is None:
		return False

	try:
		table.delete_item(Key={"PK": f"ROUTING#CLIENT#{client_code}", "SK": "CONFIG"})
		logger.info(f"Deleted DynamoDB route for Client {client_code}")
		return True
	except Exception as e:
		logger.error(f"Failed to delete Client {client_code} from DynamoDB: {e}")
		return False


def sync_all_client_routes(client_name: str) -> None:
	"""Sync all client routes (SMS, Email, and WhatsApp) for a client."""
	if not client_name:
		return

	try:
		client_doc = frappe.get_doc("MSuite Client", client_name)
		# 1. Sync Client Route (for SMS and Email)
		sync_client_route(client_doc)

		# 2. Sync WhatsApp WABA Routes
		accounts = frappe.get_all(
			"MSuite Connected Account",
			filters={"client": client_name, "platform": "WhatsApp"},
			fields=["name", "account_id", "status"],
		)
		for acc in accounts:
			if acc.account_id:
				status = "Active" if (client_doc.status == "Active" and acc.status == "Active") else "Suspended"
				sync_waba_route(acc.account_id, client_doc, status=status)
	except Exception as e:
		logger.error(f"Failed to sync all routes for client {client_name}: {e}")
