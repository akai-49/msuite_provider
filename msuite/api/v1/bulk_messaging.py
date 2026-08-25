"""
Bulk-messaging broker.

The client workspace has no AWS credentials — not a key, not an STS token,
not even the bucket name. It gets exactly two things from here:

  1. `request_upload_url`  — a short-lived presigned S3 PUT URL for one key
  2. `trigger_campaign`    — an SQS dispatch message, sent with OUR credentials

The manifest itself goes client → S3 directly over the presigned URL, so a
300 MB blast never crosses this site. Only the two small control calls do.

Why not hand the client a key at all: there is one AWS account behind every
client site. An AWS credential is therefore cross-tenant — a client holding
one could read another client's manifests or drain the shared queue. Same
reason Meta app secrets never leave this site while per-user tokens do.

Authenticated with `require_msuite_client_auth`, the same shared-secret header
pair `gmail_relay.py` uses. The client has one credential for every direction.
"""

import json
import re

import frappe
from frappe.utils import now_datetime

from msuite.msuite_client.doctype.msuite_aws_settings.msuite_aws_settings import (
	boto_session,
	get_settings,
	secret_id_for,
)
from msuite.utils.validators import (
	error_response,
	require_msuite_client_auth,
	success_response,
)

# Channels the execution plane has a queue for.
_CHANNELS = ("email", "sms", "whatsapp")

# Doc names come from the client, so they are treated as untrusted input for
# key construction. Frappe names are alphanumeric plus - _ / . in practice;
# anything else is stripped rather than rejected, so a rename convention on
# the client cannot break dispatch.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _payload(kwargs: dict) -> dict:
	data = kwargs
	if not data and frappe.request:
		data = frappe.request.get_json(silent=True) or {}
	return data or {}


def _manifest_key(client_code: str, channel: str, bulk_doctype: str, bulk_doc_name: str) -> str:
	"""`manifests/<client>/<channel>/<doctype>/<name>/<ts>.ndjson`.

	The client segment comes first and is derived from the AUTHENTICATED
	client, never from the request body. That is what makes the prefix check
	in `trigger_campaign` meaningful: a client cannot name a key outside its
	own namespace, and cannot enqueue one that is.
	"""
	parts = [
		_UNSAFE.sub("-", client_code),
		_UNSAFE.sub("-", channel),
		_UNSAFE.sub("-", frappe.scrub(bulk_doctype)),
		_UNSAFE.sub("-", bulk_doc_name),
		now_datetime().strftime("%Y%m%dT%H%M%S%f") + ".ndjson",
	]
	return "manifests/" + "/".join(parts)


def _client_prefix(client_code: str) -> str:
	return f"manifests/{_UNSAFE.sub('-', client_code)}/"


@frappe.whitelist(allow_guest=True)
def request_upload_url(**kwargs):
	"""Mint a presigned PUT URL for one manifest.

	Payload:
	  {client_name, channel, bulk_doctype, bulk_doc_name}

	Returns:
	  {url, key, expires_in, max_bytes}

	The URL is good for one key, one method, and a few minutes. It carries no
	identity the client can reuse for anything else.
	"""
	data = _payload(kwargs)

	client_name = data.get("client_name")
	channel = (data.get("channel") or "").strip().lower()
	bulk_doctype = data.get("bulk_doctype") or ""
	bulk_doc_name = data.get("bulk_doc_name") or ""

	if not client_name or not bulk_doc_name:
		return error_response("INVALID_REQUEST", "client_name and bulk_doc_name are required")
	if channel not in _CHANNELS:
		return error_response("INVALID_REQUEST", f"channel must be one of {', '.join(_CHANNELS)}")

	try:
		client_doc = require_msuite_client_auth(client_name)
	except Exception as e:
		return error_response("AUTH_FAILED", str(e))

	try:
		settings = get_settings()
	except Exception as e:
		return error_response("NOT_ENABLED", str(e))

	client_code = client_doc.client_code or client_doc.name
	key = _manifest_key(client_code, channel, bulk_doctype, bulk_doc_name)
	expiry = int(settings.presign_expiry_seconds or 900)

	try:
		url = boto_session().client("s3").generate_presigned_url(
			"put_object",
			Params={
				"Bucket": settings.manifest_bucket,
				"Key": key,
				"ContentType": "application/x-ndjson",
			},
			ExpiresIn=expiry,
		)
	except Exception as e:
		frappe.log_error(title="AWS presign failed", message=frappe.get_traceback())
		return error_response("PRESIGN_FAILED", str(e)[:300])

	return success_response(
		{
			"url": url,
			"key": key,
			"expires_in": expiry,
			"max_bytes": int(settings.max_manifest_bytes or 0),
		}
	)


@frappe.whitelist(allow_guest=True)
def trigger_campaign(**kwargs):
	"""Enqueue one uploaded manifest onto the main router queue.

	Payload:
	  {client_name, key, channel, bulk_doctype, bulk_doc_name, campaign_id,
	   recipient_count}

	Returns:
	  {message_id, recipient_count}

	Three gates, in order — plan, ownership, existence:

	  * the client's plan must actually include bulk messaging, and the blast
	    must fit whatever volume the plan allows;
	  * the key must sit inside this client's own prefix, so an authenticated
	    client cannot dispatch another client's manifest;
	  * the object must exist in S3, so a failed or abandoned upload cannot
	    put a message on the queue pointing at nothing.
	"""
	data = _payload(kwargs)

	client_name = data.get("client_name")
	key = data.get("key") or ""
	channel = (data.get("channel") or "").strip().lower()

	if not client_name or not key:
		return error_response("INVALID_REQUEST", "client_name and key are required")
	if channel not in _CHANNELS:
		return error_response("INVALID_REQUEST", f"channel must be one of {', '.join(_CHANNELS)}")

	try:
		client_doc = require_msuite_client_auth(client_name)
	except Exception as e:
		return error_response("AUTH_FAILED", str(e))

	try:
		settings = get_settings()
	except Exception as e:
		return error_response("NOT_ENABLED", str(e))

	client_code = client_doc.client_code or client_doc.name
	recipient_count = int(data.get("recipient_count") or 0)

	allowed, reason = _check_plan(client_doc, recipient_count)
	if not allowed:
		return error_response("PLAN_DENIED", reason)

	# Ownership. The presigned URL already confined the client to this prefix,
	# but the key arrives back as plain request input — re-derive, never trust.
	if not key.startswith(_client_prefix(client_code)):
		frappe.log_error(
			title="AWS trigger rejected: key outside client prefix",
			message=f"client={client_doc.name} key={key}",
		)
		return error_response("FORBIDDEN", "Manifest key does not belong to this client")

	session = boto_session()

	try:
		session.client("s3").head_object(Bucket=settings.manifest_bucket, Key=key)
	except Exception:
		return error_response("NOT_UPLOADED", "No manifest at that key — upload it before triggering")

	body = {
		"bucket": settings.manifest_bucket,
		"s3_key": key,
		"channel": channel,
		"bulk_doctype": data.get("bulk_doctype") or "",
		"bulk_doc_name": data.get("bulk_doc_name") or "",
		"campaign_id": data.get("campaign_id") or data.get("bulk_doc_name") or "",
		"client": client_code,
		# The gateway credentials the sender Lambda will need. Named here, by
		# us, from our own convention — the client never learns the name of a
		# secret it could not read anyway.
		"credentials_secret_id": secret_id_for(client_code, channel),
	}

	try:
		message_id = session.client("sqs").send_message(
			QueueUrl=settings.main_router_queue_url, MessageBody=json.dumps(body)
		)["MessageId"]
	except Exception as e:
		frappe.log_error(title="AWS trigger failed", message=frappe.get_traceback())
		return error_response("ENQUEUE_FAILED", str(e)[:300])

	frappe.logger().info(
		f"aws broker: {client_doc.name} dispatched {channel} manifest {key} (sqs {message_id})"
	)
	return success_response({"message_id": message_id, "recipient_count": recipient_count})


def _check_plan(client_doc, recipient_count: int) -> tuple[bool, str]:
	"""Gate the dispatch on the client's subscription.

	This is the point the client cannot route around. The client site runs its
	own `plan_enforcer` check before exporting, but that check lives on the
	customer's own bench — the enforceable one is here, where the plan data
	and the AWS credentials both live.
	"""
	if not client_doc.customer:
		return False, "Client is not linked to a customer"

	try:
		from msuite.services.entitlement_service import get_customer_entitlements

		entitlements = get_customer_entitlements(client_doc.customer) or {}
	except Exception:
		# An entitlement lookup that errors must not silently grant access.
		frappe.log_error(title="AWS trigger: entitlement lookup failed", message=frappe.get_traceback())
		return False, "Could not verify plan entitlements"

	features = entitlements.get("features") or {}
	feature = features.get("bulk_messaging") or {}
	if not feature.get("enabled"):
		return False, "Bulk messaging is not enabled on this plan"

	# `limit` is the plan's per-blast recipient ceiling. None/0 means unlimited,
	# matching how the rest of the entitlement map reads limits.
	limit = int(feature.get("limit") or 0)
	if limit and recipient_count > limit:
		return False, f"Blast of {recipient_count} exceeds the plan limit of {limit}"

	return True, ""


@frappe.whitelist(allow_guest=True)
def sync_channel_secret(**kwargs):
	"""Create or update a client channel credentials secret in AWS Secrets Manager.

	Payload:
	  {client_name, channel, credentials}

	Returns:
	  {ok: True, secret_name: ...}
	"""
	data = _payload(kwargs)

	client_name = data.get("client_name")
	channel = (data.get("channel") or "").strip().lower()
	credentials = data.get("credentials") or {}

	if not client_name or not channel:
		return error_response("INVALID_REQUEST", "client_name and channel are required")
	if channel not in _CHANNELS:
		return error_response("INVALID_REQUEST", f"channel must be one of {', '.join(_CHANNELS)}")
	if not credentials or not isinstance(credentials, dict):
		return error_response("INVALID_REQUEST", "credentials dict is required")

	try:
		client_doc = require_msuite_client_auth(client_name)
	except Exception as e:
		return error_response("AUTH_FAILED", str(e))

	try:
		get_settings()
	except Exception as e:
		return error_response("NOT_ENABLED", str(e))

	client_code = client_doc.client_code or client_doc.name
	secret_name = secret_id_for(client_code, channel)

	session = boto_session()
	sm = session.client("secretsmanager")
	secret_str = json.dumps(credentials)

	try:
		try:
			sm.put_secret_value(SecretId=secret_name, SecretString=secret_str)
			frappe.logger().info(f"aws secretsmanager: updated secret {secret_name} for client {client_doc.name}")
		except sm.exceptions.ResourceNotFoundException:
			sm.create_secret(
				Name=secret_name,
				Description=f"MSuite gateway credentials for client {client_doc.name} ({channel})",
				SecretString=secret_str,
			)
			frappe.logger().info(f"aws secretsmanager: created secret {secret_name} for client {client_doc.name}")
	except Exception as e:
		frappe.log_error(title=f"AWS Secrets Manager sync failed for {secret_name}", message=frappe.get_traceback())
		return error_response("SECRET_SYNC_FAILED", str(e)[:300])

	return success_response({"ok": True, "secret_name": secret_name})

