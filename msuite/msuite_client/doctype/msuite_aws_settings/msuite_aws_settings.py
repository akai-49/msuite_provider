# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
The only place AWS credentials exist in the MSuite platform.

Client workspaces never hold an AWS key, an STS token, a bucket name or a
queue URL. They ask this site for a presigned URL, upload to it, and ask this
site to enqueue — see `api/v1/bulk_messaging.py`.

Why this is not the client's business: there is ONE AWS account behind every
client site, so an AWS key is a cross-tenant credential. A client holding it
could read another client's manifests, write into their prefix, or drain the
queue. That is the same reason Meta app secrets never leave this site while
per-user tokens do.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

SETTINGS_DOCTYPE = "MSuite AWS Settings"


class MSuiteAWSSettings(Document):
	def validate(self):
		if self.presign_expiry_seconds and not (60 <= self.presign_expiry_seconds <= 3600):
			frappe.throw(_("Presign Expiry must be between 60 and 3600 seconds."))
		if self.min_recipient_threshold is not None and self.min_recipient_threshold < 1:
			frappe.throw(_("Min Recipient Threshold must be at least 1."))

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Prove these credentials can do the two things the broker does."""
		session = boto_session()
		identity = session.client("sts").get_caller_identity()
		session.client("s3").head_bucket(Bucket=self.manifest_bucket)
		session.client("sqs").get_queue_attributes(
			QueueUrl=self.main_router_queue_url, AttributeNames=["QueueArn"]
		)
		return {"ok": True, "account": identity.get("Account"), "arn": identity.get("Arn")}


def get_settings():
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	if not settings.enabled:
		frappe.throw(_("AWS bulk messaging is not enabled on this provider."))
	return settings


def boto_session():
	"""A boto3 Session from the settings, falling back to ambient credentials.

	boto3 is imported here rather than at module scope: a provider bench that
	never brokers AWS traffic must not fail to import this module.
	"""
	try:
		import boto3
	except ImportError:
		frappe.throw(_('boto3 is not installed. Run: pip install -e "apps/msuite[aws]"'))

	settings = frappe.get_single(SETTINGS_DOCTYPE)
	if settings.access_key_id:
		return boto3.Session(
			aws_access_key_id=settings.access_key_id,
			aws_secret_access_key=settings.get_password("secret_access_key", raise_exception=False),
			region_name=settings.aws_region,
		)
	return boto3.Session(region_name=settings.aws_region)


def secret_id_for(client_code: str, channel: str) -> str:
	"""The Secrets Manager name holding a client's gateway credentials.

	By convention, not configuration: `<prefix>/<channel>/<client_code>`. The
	provider computes it and injects it into the SQS trigger, so the client
	never learns the name of a secret it is not allowed to read anyway.
	"""
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	prefix = (settings.secret_prefix or "msuite").strip("/")
	return f"{prefix}/{channel}/{client_code}"
