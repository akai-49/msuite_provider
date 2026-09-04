# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
AWS Tenant Routing Sync Service (Control Plane).

Synchronizes tenant routing metadata (WABA ID -> Client URL & credentials)
from the Provider to AWS DynamoDB (msuite-tenant-routing-${Stage}).
Enables direct-to-client webhook dispatch on the AWS execution plane.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import frappe
from frappe import _
from frappe.utils.password import get_decrypted_password

from msuite.constants import MSUITE_LOGGER_NAME

logger = frappe.logger(MSUITE_LOGGER_NAME)

DEFAULT_ROUTING_TTL_SECONDS = 86400  # 24 hours

_routing_redis_client = None
_routing_redis_cached_url: str | None = None


def get_redis_endpoint_url(purpose: str = "Tenant Routing") -> str | None:
	"""Resolve the active Redis URL for a specified purpose.

	Order of resolution:
	1. Enabled row in MSuite AWS Settings -> redis_endpoints matching purpose.
	2. frappe.conf override (e.g. routing_redis_url or analytics_redis_url).
	3. Ambient environment variable (e.g. ROUTING_REDIS_URL or ANALYTICS_REDIS_URL).
	"""
	slug = purpose.lower().replace(" ", "_")
	try:
		if frappe.db.get_single_value("MSuite AWS Settings", "enabled"):
			endpoints = frappe.get_all(
				"MSuite Redis Endpoint",
				filters={"parent": "MSuite AWS Settings", "purpose": purpose, "enabled": 1},
				fields=["redis_url"],
				limit=1,
			)
			if endpoints and endpoints[0].redis_url:
				return endpoints[0].redis_url.strip()
	except Exception as e:
		logger.debug(f"Could not read MSuite Redis Endpoint table: {e}")

	# 2. frappe.conf fallback
	conf_url = frappe.conf.get(f"{slug}_redis_url") or frappe.conf.get("routing_redis_url")
	if conf_url:
		return conf_url.strip()

	# 3. Environment variable fallback
	env_key = f"{slug.upper()}_REDIS_URL"
	env_url = os.environ.get(env_key) or os.environ.get("ROUTING_REDIS_URL")
	if env_url:
		return env_url.strip()

	return None


def get_routing_redis_client():
	"""Connect to Tenant Routing Redis with pooling and 0.5s timeout. Fails soft.

	Reuses existing connection if URL hasn't changed; reconnects if URL is updated.
	"""
	global _routing_redis_client, _routing_redis_cached_url

	url = get_redis_endpoint_url("Tenant Routing")
	if not url:
		if _routing_redis_client is not None:
			try:
				_routing_redis_client.close()
			except Exception:
				pass
			_routing_redis_client = None
			_routing_redis_cached_url = None
		return None

	if _routing_redis_client is not None and _routing_redis_cached_url == url:
		return _routing_redis_client

	# Close previous client if URL changed
	if _routing_redis_client is not None:
		try:
			_routing_redis_client.close()
		except Exception:
			pass
		_routing_redis_client = None
		_routing_redis_cached_url = None

	try:
		import redis

		normalized_url = url
		if not (normalized_url.startswith("redis://") or normalized_url.startswith("rediss://") or normalized_url.startswith("unix://")):
			scheme = "rediss://" if ("serverless" in normalized_url or "cache.amazonaws.com" in normalized_url) else "redis://"
			normalized_url = f"{scheme}{normalized_url}"
			if not normalized_url.endswith(("/0", "/1", "/2")):
				normalized_url = f"{normalized_url}/0"

		client = redis.Redis.from_url(
			normalized_url,
			socket_connect_timeout=0.5,
			socket_timeout=0.5,
			decode_responses=True,
		)
		_routing_redis_client = client
		_routing_redis_cached_url = url
		return client
	except Exception as e:
		logger.warning(f"Failed to initialize Tenant Routing Redis client from Provider: {e}")
		return None


def get_routing_table():
	"""Connect to DynamoDB tenant routing table using MSuite AWS Settings."""
	try:
		enabled = frappe.db.get_single_value("MSuite AWS Settings", "enabled")
		if not enabled:
			return None

		from msuite.msuite_client.doctype.msuite_aws_settings.msuite_aws_settings import boto_session

		session = boto_session()
		dynamodb = session.resource("dynamodb")

		stage = frappe.conf.get("stage") or "dev"
		table_name = (
			frappe.conf.get("msuite_tenant_routing_table")
			or os.environ.get("ROUTING_TABLE")
			or f"msuite-tenant-routing-{stage}"
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
			"updated_at": datetime.now(timezone.utc).isoformat(),
		}
		table.put_item(Item=item)
		logger.info(f"Synchronized DynamoDB route for WABA {waba_id} -> {client_doc.client_code} ({effective_status})")

		# Redis write-through
		try:
			r = get_routing_redis_client()
			if r is not None:
				redis_payload = {
					"client_code": client_doc.client_code or "",
					"client_url": (client_doc.client_url or "").rstrip("/"),
					"api_key": client_doc.api_key or "",
					"api_secret": api_secret,
					"status": effective_status,
					"updated_at": item["updated_at"],
				}
				r.set(f"routing:waba:{waba_id}", json.dumps(redis_payload), ex=DEFAULT_ROUTING_TTL_SECONDS)
				logger.info(f"Synchronized Redis route for WABA {waba_id} -> {client_doc.client_code}")
		except Exception as re:
			logger.warning(f"Failed to write WABA {waba_id} to Routing Redis: {re}")

		return True
	except Exception as e:
		logger.error(f"Failed to sync WABA {waba_id} to DynamoDB: {e}")
		return False


def delete_waba_route(waba_id: str) -> bool:
	"""Remove a WABA routing entry from DynamoDB and evict from Routing Redis."""
	if not waba_id:
		return False

	table = get_routing_table()
	if table is None:
		return False

	try:
		table.delete_item(Key={"PK": f"ROUTING#WABA#{waba_id}", "SK": "CONFIG"})
		logger.info(f"Deleted DynamoDB route for WABA {waba_id}")

		try:
			r = get_routing_redis_client()
			if r is not None:
				r.delete(f"routing:waba:{waba_id}")
				logger.info(f"Deleted Redis route for WABA {waba_id}")
		except Exception as re:
			logger.warning(f"Failed to delete WABA {waba_id} from Routing Redis: {re}")

		return True
	except Exception as e:
		logger.error(f"Failed to delete WABA {waba_id} from DynamoDB: {e}")
		return False


def sync_client_route(client_doc_or_name, status: str | None = None) -> bool:
	"""Persist or update tenant routing for a Client (used by SMS & Email) in DynamoDB and Redis."""
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
			"updated_at": datetime.now(timezone.utc).isoformat(),
		}
		table.put_item(Item=item)
		logger.info(f"Synchronized DynamoDB route for Client {client_doc.client_code} ({effective_status})")

		# Redis write-through
		try:
			r = get_routing_redis_client()
			if r is not None:
				redis_payload = {
					"client_code": client_doc.client_code,
					"client_url": (client_doc.client_url or "").rstrip("/"),
					"api_key": client_doc.api_key or "",
					"api_secret": api_secret,
					"status": effective_status,
					"updated_at": item["updated_at"],
				}
				r.set(f"routing:client:{client_doc.client_code}", json.dumps(redis_payload), ex=DEFAULT_ROUTING_TTL_SECONDS)
				logger.info(f"Synchronized Redis route for Client {client_doc.client_code}")
		except Exception as re:
			logger.warning(f"Failed to write Client route to Routing Redis: {re}")

		return True
	except Exception as e:
		logger.error(f"Failed to sync Client route to DynamoDB: {e}")
		return False


def delete_client_route(client_code: str) -> bool:
	"""Remove a Client routing entry from DynamoDB and evict from Routing Redis."""
	if not client_code:
		return False

	table = get_routing_table()
	if table is None:
		return False

	try:
		table.delete_item(Key={"PK": f"ROUTING#CLIENT#{client_code}", "SK": "CONFIG"})
		logger.info(f"Deleted DynamoDB route for Client {client_code}")

		try:
			r = get_routing_redis_client()
			if r is not None:
				r.delete(f"routing:client:{client_code}")
				logger.info(f"Deleted Redis route for Client {client_code}")
		except Exception as re:
			logger.warning(f"Failed to delete Client {client_code} from Routing Redis: {re}")

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


def sync_all_tenants() -> dict:
	"""Bulk-sync all active MSuite Clients and their connected WhatsApp WABAs to DynamoDB.

	Used for initial system migration / backfill or after AWS table recreation.
	"""
	clients = frappe.get_all(
		"MSuite Client",
		fields=["name", "client_code", "status"],
	)

	synced_clients = 0
	synced_wabas = 0
	errors = []

	for client in clients:
		try:
			client_doc = frappe.get_doc("MSuite Client", client.name)
			if sync_client_route(client_doc):
				synced_clients += 1

			accounts = frappe.get_all(
				"MSuite Connected Account",
				filters={"client": client.name, "platform": "WhatsApp"},
				fields=["name", "account_id", "status"],
			)
			for acc in accounts:
				if acc.account_id:
					status = "Active" if (client_doc.status == "Active" and acc.status == "Active") else "Suspended"
					if sync_waba_route(acc.account_id, client_doc, status=status):
						synced_wabas += 1
		except Exception as e:
			err_msg = f"Failed to sync client {client.name} ({client.client_code}): {e}"
			logger.error(err_msg)
			errors.append(err_msg)

	logger.info(f"Bulk DynamoDB routing sync complete: {synced_clients} clients, {synced_wabas} WABAs synced, {len(errors)} errors")
	return {
		"status": "success" if not errors else "partial",
		"synced_clients": synced_clients,
		"synced_wabas": synced_wabas,
		"errors": errors,
	}


@frappe.whitelist()
def sync_all_routes_rpc() -> dict:
	"""Whitelisted API method to trigger bulk tenant routing synchronization."""
	from msuite.utils.validators import require_system_manager_or_msuite_manager

	require_system_manager_or_msuite_manager()
	return sync_all_tenants()

