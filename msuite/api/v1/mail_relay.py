"""
Provider-side mail relay — one endpoint pair for every OAuth mailbox.

The client used to hardcode `msuite.api.v1.gmail_relay.*` in four places.
Rather than add a fifth string per provider, this module owns the shared
preamble (auth → resolve Connected Account → refresh a token that's about
to expire) and dispatches the transport on the account's platform.

The client therefore sends ONE endpoint regardless of provider; which
backend runs is decided here, server-side, from the account it resolves.

`gmail_relay.send_email` / `poll_new_messages` remain live and unchanged.
The two benches deploy independently, so a client still on the old build
keeps working against the old endpoints; delete them one release after
both sides ship.
"""
import frappe
from frappe.utils import add_to_date, now_datetime

from msuite.constants import Platform
from msuite.services.mail import graph
from msuite.utils.validators import (
    error_response,
    require_msuite_client_auth,
    success_response,
)

# Connected Account platforms this relay serves, mapped to their token
# refresher. Keys MUST match `MSuite Connected Account.platform`.
_MAIL_PLATFORMS = (Platform.GMAIL, Platform.OUTLOOK)


def _refresher(platform: str):
    if platform == Platform.OUTLOOK:
        from msuite.services.oauth.microsoft import refresh_token_fn
    else:
        from msuite.services.oauth.google import refresh_token_fn
    return refresh_token_fn


def _payload(kwargs: dict) -> dict:
    data = kwargs
    if not data and frappe.request:
        data = frappe.request.get_json(silent=True) or {}
    return data or {}


def _resolve_account(data: dict):
    """Shared preamble. Returns (connected_account_doc, access_token) or
    (None, error_response) — callers must check the first element."""
    client_name = data.get("client_name")
    # `mailbox` is the provider-neutral name; `gmail_address` is accepted so
    # a client mid-upgrade can keep sending the old key.
    mailbox = data.get("mailbox") or data.get("gmail_address")
    if not client_name or not mailbox:
        return None, error_response("INVALID_REQUEST", "client_name and mailbox are required")

    try:
        client_doc = require_msuite_client_auth(client_name)
    except Exception as e:
        return None, error_response("AUTH_FAILED", str(e))

    ca_name = frappe.db.get_value(
        "MSuite Connected Account",
        {
            "client": client_doc.name,
            "platform": ["in", list(_MAIL_PLATFORMS)],
            "account_id": mailbox,
        },
        "name",
    )
    if not ca_name:
        return None, error_response(
            "NOT_FOUND", f"Mailbox {mailbox} not connected on provider for client {client_name}"
        )

    ca = frappe.get_doc("MSuite Connected Account", ca_name)

    # Refresh when missing or expiring within the next 60s — a token that
    # expires mid-request fails the upstream call anyway.
    if not ca.access_token or not ca.token_expiry or ca.token_expiry <= add_to_date(
        now_datetime(), seconds=60
    ):
        try:
            _refresher(ca.platform)(ca.name)
            ca = frappe.get_doc("MSuite Connected Account", ca_name)
        except Exception as e:
            return None, error_response(
                "TOKEN_REFRESH_FAILED", f"Could not refresh {ca.platform} token: {e}"
            )

    return ca, ca.get_password("access_token")


@frappe.whitelist(allow_guest=True)
def send_email(**kwargs):
    """Send an outbound email from a connected mailbox.

    Payload: client_name, mailbox (or gmail_address), to, subject,
    body_html, body_text, cc, bcc, attachments, thread_id,
    in_reply_to (Gmail: RFC Message-ID) or reply_to_message_id
    (Outlook: the parent's Graph id — see graph.send_message).
    """
    data = _payload(kwargs)
    ca, token_or_error = _resolve_account(data)
    if ca is None:
        return token_or_error

    if ca.platform == Platform.OUTLOOK:
        try:
            return success_response(graph.send_message(token_or_error, data))
        except graph.GraphThrottled as e:
            return error_response("GRAPH_THROTTLED", str(e))
        except graph.GraphError as e:
            return error_response("GRAPH_API_ERROR", str(e))
        except Exception as e:
            return error_response("OUTLOOK_SEND_FAILED", f"HTTP request failed: {e}")

    from msuite.api.v1.gmail_relay import send_via_gmail

    return send_via_gmail(token_or_error, ca.account_id, data)


@frappe.whitelist(allow_guest=True)
def fetch_attachment(**kwargs):
    """Download one attachment's bytes for a connected mailbox.

    Payload: client_name, mailbox, message_id, attachment_id.
    Returns {content: <standard base64>, filename, mime_type, size}.

    Closes G8 for BOTH providers — the Gmail OAuth path returned attachment
    metadata with no way to get the bytes, so the inbox showed filenames
    that couldn't be opened.
    """
    data = _payload(kwargs)
    ca, token_or_error = _resolve_account(data)
    if ca is None:
        return token_or_error

    message_id = data.get("message_id")
    attachment_id = data.get("attachment_id")
    if not message_id or not attachment_id:
        return error_response("INVALID_REQUEST", "message_id and attachment_id are required")

    try:
        if ca.platform == Platform.OUTLOOK:
            return success_response(graph.fetch_attachment(token_or_error, message_id, attachment_id))

        from msuite.api.v1.gmail_relay import fetch_gmail_attachment

        return success_response(fetch_gmail_attachment(token_or_error, message_id, attachment_id))
    except graph.GraphThrottled as e:
        return error_response("GRAPH_THROTTLED", str(e))
    except Exception as e:
        return error_response("ATTACHMENT_FETCH_FAILED", str(e))


@frappe.whitelist(allow_guest=True)
def poll_new_messages(**kwargs):
    """Poll a connected mailbox for new messages.

    Payload: client_name, mailbox (or gmail_address), cursor (Gmail:
    history_id; Outlook: the delta_link JSON blob). `history_id` is
    accepted as an alias so an old client keeps working.

    Returns {messages: [...], new_cursor: str, new_history_id: str}.
    `new_history_id` is echoed for the old client; new clients read
    `new_cursor` for both providers.
    """
    data = _payload(kwargs)
    ca, token_or_error = _resolve_account(data)
    if ca is None:
        return token_or_error

    cursor = data.get("cursor")
    if cursor is None:
        cursor = data.get("history_id")

    if ca.platform == Platform.OUTLOOK:
        try:
            messages, new_cursor = graph.poll_messages(token_or_error, cursor)
        except graph.GraphThrottled as e:
            return error_response("GRAPH_THROTTLED", str(e))
        except graph.GraphError as e:
            return error_response("GRAPH_API_ERROR", str(e))
        except Exception as e:
            return error_response("POLL_FAILED", f"Failed to poll Outlook: {e}")
        return success_response(
            {"messages": messages, "new_cursor": new_cursor, "new_history_id": None}
        )

    from msuite.api.v1.gmail_relay import poll_gmail

    result = poll_gmail(token_or_error, cursor)
    # Gmail's poller speaks history_id; surface it under the neutral name
    # too so the client reads one key for both providers.
    payload = (result or {}).get("data")
    if isinstance(payload, dict) and "new_cursor" not in payload:
        payload["new_cursor"] = payload.get("new_history_id")
    return result
