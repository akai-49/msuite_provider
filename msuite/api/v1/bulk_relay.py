"""
Webhook fan-out and analytics reads for the AWS execution plane.

Two directions, both brokered here because the client holds no AWS credential
and the execution plane holds no client registry:

  * `relay_webhook`    AWS -> here -> the right client, **byte for byte**
  * `campaign_analytics`  client -> here -> the analytics API

### Why the relay must not touch the body

Gateway webhooks are configured per gateway account, not per campaign, so
repointing them at API Gateway affects every campaign on that account —
including Standard-routed blasts that know nothing about AWS. Those still need
their MariaDB status tracking, and the client's existing handlers authenticate
by recomputing an HMAC over the raw request body.

So this endpoint forwards `data=<original bytes>` with the gateway's own
signature header intact. It does **not** use `_post_to_client`, which sends
`json=payload` — re-serialising a dict changes key order and whitespace, the
recomputed HMAC stops matching, and every relayed receipt would 401.
"""

import json

import frappe
import requests

from msuite.msuite_client.doctype.msuite_aws_settings.msuite_aws_settings import get_settings
from msuite.utils.cache import (
	get_relay_route_cache,
	invalidate_relay_route_cache,
	set_relay_route_cache,
)
from msuite.utils.validators import (
	error_response,
	require_msuite_client_auth,
	success_response,
)

RELAY_TOKEN_HEADER = "X-MSuite-Relay-Token"

# Where a payload lands on the client, keyed `(provider, kind)`.
#
# The `kind` half is load-bearing for SMS: an inbound reply and a delivery
# receipt arrive at the same gateway URL but are two different client
# functions, with different parsing and different side effects (a reply can
# carry STOP and opt the number out). Routing both to `receive_dlr` would drop
# every inbound SMS silently.
#
# Meta needs no split — the client's `feed_whatsapp_payload` already branches
# on `value.messages[]` vs `value.statuses[]` internally, and one payload can
# legitimately carry both.
_CLIENT_ENDPOINT = {
	("msg91", "dlr"): "msuite_workspace.sms.api.v1.webhook.receive_dlr",
	("msg91", "chat"): "msuite_workspace.sms.api.v1.webhook.receive_inbound",
	("twilio", "dlr"): "msuite_workspace.sms.api.v1.webhook.receive_dlr",
	("twilio", "chat"): "msuite_workspace.sms.api.v1.webhook.receive_inbound",
	# Legacy URL, kept stable on purpose (see that module's docstring). It
	# authenticates with PROVIDER auth, not Meta's signature — which the relay
	# supplies anyway via make_auth_headers.
	("meta", "dlr"): "msuite_workspace.api.v1.whatsapp.webhook.webhook",
	("meta", "chat"): "msuite_workspace.api.v1.whatsapp.webhook.webhook",
	("ses", "dlr"): "msuite_workspace.msuite_email.api.email_tracking.receive_ses_notification",
}


@frappe.whitelist(allow_guest=True)
def relay_webhook(**kwargs):
	"""Forward one gateway receipt to the client that owns the account.

	Authenticated by a shared token the execution plane holds — not by client
	credentials, which the Lambda deliberately does not have. The receipt's own
	gateway signature is what the client then verifies.

	Routing comes from `X-MSuite-Relay-Route`, an opaque id baked into the
	webhook URL registered with the gateway. It is not a credential: a wrong
	route just misroutes to a client whose signature check then rejects it.
	"""
	request = frappe.request
	if not request:
		return error_response("INVALID_REQUEST", "No request context")

	headers = {k.lower(): v for k, v in dict(request.headers or {}).items()}

	if not _relay_authorised(headers):
		return error_response("AUTH_FAILED", "Invalid relay token")

	provider = (headers.get("x-msuite-relay-provider") or "").strip().lower()
	route = (headers.get("x-msuite-relay-route") or "").strip()
	kind = (headers.get("x-msuite-relay-kind") or "dlr").strip().lower()

	endpoint = _CLIENT_ENDPOINT.get((provider, kind))
	if not endpoint:
		return error_response(
			"UNKNOWN_PROVIDER", f"No client endpoint for provider={provider!r} kind={kind!r}"
		)

	client_doc, account = _resolve_route(route)
	if not client_doc:
		# Not an error worth retrying — an unknown route is a stale webhook URL,
		# and answering 200 stops AWS redriving it forever.
		frappe.logger().warning(f"bulk_relay: unknown route {route!r} for {provider}")
		return success_response({"forwarded": False, "reason": "unknown route"})

	# The bytes, exactly as the gateway sent them. `client_service` owns every
	# provider -> client call; this one just supplies the payload.
	from msuite.services.client_service import post_raw_to_client

	ok, error = post_raw_to_client(
		client_doc,
		endpoint,
		request.get_data(),
		dict(request.headers or {}),
		query=f"account={account}" if account else "",
	)
	if not ok:
		return error_response("FORWARD_FAILED", error)

	return success_response({"forwarded": True, "client": client_doc.name, "kind": kind})


def _relay_authorised(headers: dict) -> bool:
	"""Constant-time compare against the token in AWS settings. Fails closed."""
	import hmac

	settings = frappe.get_single("MSuite AWS Settings")
	expected = settings.get_password("relay_token", raise_exception=False) or ""
	presented = (headers.get(RELAY_TOKEN_HEADER.lower()) or "").strip()
	if not (expected and presented):
		return False
	return hmac.compare_digest(expected, presented)


def _resolve_route(route: str):
	"""`(client_doc, account_name)` for a routing id, or `(None, None)`.

	The id is `<client_code>:<account>` — enough to route, and carrying nothing
	that is worth protecting. The account segment is passed on as the client's
	own `?account=` query param, which its webhook already expects.
	"""
	if not route or ":" not in route:
		return None, None

	client_code, _, account = route.partition(":")
	name = _client_name_for(client_code)
	if not name:
		return None, None

	client_doc = frappe.get_doc("MSuite Client", name)
	if client_doc.status != "Active":
		# Deactivated since the lookup was cached. Drop the entry so the next
		# receipt re-reads rather than waiting out the TTL.
		invalidate_relay_route_cache(client_code)
		return None, None
	return client_doc, account


def _client_name_for(client_code: str) -> str | None:
	"""`client_code` -> MSuite Client name, cached in Redis.

	This sits in the hot path of every delivery receipt from every gateway —
	one lookup per receipt, and a large blast produces one receipt per
	recipient. Left uncached it is a `client_code` query per webhook.

	Misses are cached too. The relay endpoint is public, so a stale webhook URL
	or a probe would otherwise reach the database on every request.
	"""
	if not client_code:
		return None

	cached = get_relay_route_cache(client_code)
	if cached is not None:
		return cached or None

	name = frappe.db.get_value("MSuite Client", {"client_code": client_code}, "name") or ""
	set_relay_route_cache(client_code, name)
	return name or None


@frappe.whitelist(allow_guest=True)
def campaign_analytics(**kwargs):
	"""Read one campaign's counters on the client's behalf.

	The client has no API Gateway URL and no analytics key — both live in
	`MSuite AWS Settings`. Same brokering shape as dispatch, in the opposite
	direction.
	"""
	data = kwargs
	if not data and frappe.request:
		data = frappe.request.get_json(silent=True) or {}

	client_name = data.get("client_name")
	campaign_id = data.get("campaign_id")
	if not client_name or not campaign_id:
		return error_response("INVALID_REQUEST", "client_name and campaign_id are required")

	try:
		require_msuite_client_auth(client_name)
	except Exception as e:
		return error_response("AUTH_FAILED", str(e))

	try:
		settings = get_settings()
	except Exception as e:
		return error_response("NOT_ENABLED", str(e))

	base = (settings.api_gateway_base_url or "").rstrip("/")
	if not base:
		return error_response("NOT_CONFIGURED", "No API Gateway base URL configured")

	api_key = settings.get_password("analytics_api_key", raise_exception=False) or ""

	try:
		resp = requests.get(
			f"{base}/analytics/campaign/{campaign_id}",
			headers={"x-api-key": api_key},
			timeout=15,
		)
	except Exception as e:
		return error_response("UPSTREAM_FAILED", str(e)[:300])

	if resp.status_code != 200:
		return error_response("UPSTREAM_FAILED", f"HTTP {resp.status_code}: {resp.text[:200]}")

	try:
		return success_response(resp.json())
	except json.JSONDecodeError:
		return error_response("UPSTREAM_FAILED", "Analytics API returned a non-JSON response")
