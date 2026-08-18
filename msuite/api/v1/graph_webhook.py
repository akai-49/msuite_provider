"""
Microsoft Graph change-notification receiver.

Two public endpoints, both unauthenticated by necessity — Graph cannot
present our credentials. `clientState` is what separates a real callback
from anyone who guesses the URL, and it is verified against the mailbox
the notification claims to be about.

A notification is treated purely as a *hint*. We never read message data
out of it; we enqueue the ordinary delta sync for that mailbox and let the
existing, idempotent path do the work. A forged or replayed notification
therefore costs at most one wasted delta call, never bad data.
"""
import json

import frappe
from werkzeug.wrappers import Response

from msuite.constants import MSUITE_LOGGER_NAME, Platform
from msuite.services.mail.graph_subscriptions import verify_client_state

logger = frappe.logger(MSUITE_LOGGER_NAME)


def _validation_echo() -> Response | None:
    """Graph's subscription handshake.

    On create/renew, Graph POSTs with `?validationToken=...` and expects the
    raw token back as text/plain, HTTP 200, within 10 seconds. Anything else
    — JSON wrapping included — and the subscription is refused.

    Frappe returns JSON by default, so this hands back a werkzeug Response;
    `frappe.handler` passes those through untouched.
    """
    token = frappe.form_dict.get("validationToken")
    if token is None:
        return None
    return Response(token, status=200, mimetype="text/plain")


def _raw_notifications() -> list[dict]:
    """Graph posts `{"value": [ ... ]}` as a JSON body."""
    try:
        data = frappe.request.get_json(silent=True) if frappe.request else None
        if not data:
            raw = frappe.request.get_data(as_text=True) if frappe.request else ""
            data = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return []
    value = (data or {}).get("value")
    return value if isinstance(value, list) else []


def _account_for(subscription_id: str):
    if not subscription_id:
        return None
    name = frappe.db.get_value(
        "MSuite Connected Account",
        {"graph_subscription_id": subscription_id, "platform": Platform.OUTLOOK},
        "name",
    )
    return name


@frappe.whitelist(allow_guest=True)
def receive_graph_notification(**kwargs):
    """Mailbox change notifications.

    Always returns 202 quickly — Graph retries on non-2xx and will drop a
    subscription that keeps failing, so a bad payload must not produce a 500.
    """
    validation = _validation_echo()
    if validation is not None:
        return validation

    enqueued = set()
    for note in _raw_notifications():
        subscription_id = note.get("subscriptionId")
        ca_name = _account_for(subscription_id)
        if not ca_name:
            logger.info(f"Graph notification for unknown subscription {subscription_id} — dropping")
            continue
        if not verify_client_state(ca_name, note.get("clientState")):
            # Wrong secret: either a forgery or a stale subscription from a
            # previous encryption key. Never act on it.
            logger.warning(f"Graph notification clientState mismatch for {ca_name} — dropping")
            continue
        if ca_name in enqueued:
            continue  # one delta run covers every change in this batch
        enqueued.add(ca_name)
        _enqueue_sync(ca_name)

    return Response(status=202)


@frappe.whitelist(allow_guest=True)
def receive_graph_lifecycle(**kwargs):
    """Lifecycle events — Graph telling us the subscription itself is unwell.

      reauthorizationRequired : token needs refreshing; renewing re-authorizes
      subscriptionRemoved     : gone, recreate it
      missed                  : Graph dropped notifications; delta backfills

    All three resolve to "re-up the subscription and run a delta", because
    delta is authoritative and catches anything the gap swallowed.
    """
    validation = _validation_echo()
    if validation is not None:
        return validation

    from msuite.services.mail.graph_subscriptions import renew_subscription

    for note in _raw_notifications():
        ca_name = _account_for(note.get("subscriptionId"))
        if not ca_name or not verify_client_state(ca_name, note.get("clientState")):
            continue

        event = note.get("lifecycleEvent")
        logger.info(f"Graph lifecycle '{event}' for {ca_name}")
        try:
            renew_subscription(ca_name)
        except Exception:
            frappe.log_error(
                title=f"Graph lifecycle renewal failed ({ca_name})",
                message=frappe.get_traceback(),
            )
        # `missed` means notifications were dropped on Graph's side, so the
        # delta sweep is the only way to recover them.
        if event == "missed":
            _enqueue_sync(ca_name)

    return Response(status=202)


def _enqueue_sync(connected_account_name: str) -> None:
    """Forward to the client's ingest endpoint, exactly as the Gmail push
    path does — same durable forwarder, same client-side entry point."""
    ca = frappe.db.get_value(
        "MSuite Connected Account", connected_account_name, ["client", "account_id"], as_dict=True
    )
    if not ca:
        return

    client_status = frappe.db.get_value("MSuite Client", ca.client, "status")
    if client_status != "Active":
        return

    frappe.db.set_value(
        "MSuite Connected Account",
        connected_account_name,
        "last_inbound_event_at",
        frappe.utils.now_datetime(),
        update_modified=False,
    )

    frappe.enqueue(
        "msuite.api.v1.webhook.forward_webhook_job",
        queue="short",
        client_name=ca.client,
        endpoint="msuite_workspace.msuite_email.api.email_ingest.receive_inbound_push",
        payload={"mailbox": ca.account_id},
    )
