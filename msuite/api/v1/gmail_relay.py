"""
Gmail Relay API on Provider.

Allows authenticated clients to send emails and poll for new messages using Gmail REST API.
Authenticates client requests via require_msuite_client_auth.
"""
import base64
import requests
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

import frappe
from frappe import _
from frappe.utils import now_datetime, add_to_date

from msuite.utils.validators import (
    require_msuite_client_auth,
    success_response,
    error_response,
)
from msuite.constants import Platform
from msuite.services.oauth.google import refresh_token_fn

@frappe.whitelist(allow_guest=True)
def send_email(**kwargs):
    """
    Client calls this to send an outbound email via Gmail API.
    Payload: {
        client_name: str
        gmail_address: str
        to: str/list
        subject: str
        body_html: str
        body_text: str
        cc: str/list (optional)
        bcc: str/list (optional)
        attachments: list of dicts (optional) [{"filename": "...", "content": "base64", "mime_type": "..."}]
        thread_id: str (optional)
        in_reply_to: str (optional)
    }
    """
    # 1. Parse & validate request payload
    data = kwargs
    if not data and frappe.request:
        data = frappe.request.get_json(silent=True) or {}

    client_name = data.get("client_name")
    gmail_address = data.get("gmail_address")
    if not client_name or not gmail_address:
        return error_response("INVALID_REQUEST", "client_name and gmail_address are required")

    # 2. Authenticate the Client
    try:
        client_doc = require_msuite_client_auth(client_name)
    except Exception as e:
        return error_response("AUTH_FAILED", str(e))

    # 3. Find MSuite Connected Account
    ca_name = frappe.db.get_value(
        "MSuite Connected Account",
        {"client": client_doc.name, "platform": Platform.GMAIL, "account_id": gmail_address},
        "name"
    )
    if not ca_name:
        return error_response("NOT_FOUND", f"Gmail account {gmail_address} not connected on provider for client {client_name}")

    ca = frappe.get_doc("MSuite Connected Account", ca_name)

    # 4. Check & refresh token if expired
    # Refresh when missing or expiring within the next 60s — a token that
    # expires mid-request fails the Gmail call anyway
    if not ca.access_token or not ca.token_expiry or ca.token_expiry <= add_to_date(now_datetime(), seconds=60):
        try:
            refresh_token_fn(ca.name)
            ca = frappe.get_doc("MSuite Connected Account", ca_name) # reload
        except Exception as e:
            return error_response("TOKEN_REFRESH_FAILED", f"Could not refresh Gmail token: {str(e)}")

    access_token = ca.get_password("access_token")
    return send_via_gmail(access_token, gmail_address, data)


def send_via_gmail(access_token: str, gmail_address: str, data: dict):
    """MIME construction + Gmail send. Shared with `api/v1/mail_relay.py`,
    which owns the generic auth/account/refresh preamble for both providers.
    Returns an already-wrapped success_response/error_response."""
    # 5. Construct MIME message
    try:
        thread_id = data.get("thread_id")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = data.get("subject", "")
        msg["From"] = f"{data.get('from_name') or gmail_address} <{gmail_address}>"

        to_list = data.get("to")
        if isinstance(to_list, list):
            msg["To"] = ", ".join(to_list)
        else:
            msg["To"] = to_list or ""

        cc_list = data.get("cc")
        if cc_list:
            if isinstance(cc_list, list):
                msg["Cc"] = ", ".join(cc_list)
            else:
                msg["Cc"] = cc_list

        bcc_list = data.get("bcc")
        if bcc_list:
            if isinstance(bcc_list, list):
                msg["Bcc"] = ", ".join(bcc_list)
            else:
                msg["Bcc"] = bcc_list

        in_reply_to = data.get("in_reply_to")
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            # References header should include the in_reply_to ID
            msg["References"] = in_reply_to

        # Attach text and html parts
        body_text = data.get("body_text", "")
        body_html = data.get("body_html", "")

        if body_text:
            msg.attach(MIMEText(body_text, "plain", "utf-8"))
        if body_html:
            msg.attach(MIMEText(body_html, "html", "utf-8"))

        # Attachments
        attachments = data.get("attachments") or []
        if attachments:
            outer = MIMEMultipart("mixed")
            # Move the top-level headers onto the new outermost message —
            # otherwise the sent email has no Subject/From/To/threading
            # headers at all (they'd be stuck on the inner alternative part).
            for header in ("Subject", "From", "To", "Cc", "Bcc", "In-Reply-To", "References"):
                if msg.get(header):
                    outer[header] = msg[header]
                    del msg[header]
            outer.attach(msg)
            for att in attachments:
                filename = att.get("filename")
                content = att.get("content")
                mime_type = att.get("mime_type", "application/octet-stream")
                if not filename or not content:
                    continue
                try:
                    raw_data = base64.b64decode(content)
                    part = MIMEApplication(raw_data)
                    part.add_header("Content-Disposition", "attachment", filename=filename)
                    if mime_type:
                        part.add_header("Content-Type", mime_type)
                    outer.attach(part)
                except Exception:
                    pass
            msg = outer

        # Base64url encode the raw message
        raw_bytes = msg.as_bytes()
        raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("utf-8")
    except Exception as e:
        return error_response("MIME_CONSTRUCTION_FAILED", f"Failed to build email body: {str(e)}")

    # 6. Call Gmail API to send
    gmail_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {"raw": raw_b64}
    if thread_id:
        payload["threadId"] = thread_id

    try:
        resp = requests.post(gmail_url, headers=headers, json=payload, timeout=30)
        resp_json = resp.json()
        if resp.status_code != 200:
            return error_response("GMAIL_API_ERROR", f"Gmail API error {resp.status_code}: {resp.text}")
        
        # Return success with message ID and thread ID
        return success_response({
            "message_id": resp_json.get("id"),
            "thread_id": resp_json.get("threadId"),
        })
    except Exception as e:
        return error_response("GMAIL_SEND_FAILED", f"HTTP request failed: {str(e)}")


@frappe.whitelist(allow_guest=True)
def poll_new_messages(**kwargs):
    """
    Client calls this to poll for new messages (fallback or standard ingestion).
    Payload: {
        client_name: str
        gmail_address: str
        history_id: str/int (optional, if provided returns history since this ID)
    }
    """
    data = kwargs
    if not data and frappe.request:
        data = frappe.request.get_json(silent=True) or {}

    client_name = data.get("client_name")
    gmail_address = data.get("gmail_address")
    history_id = data.get("history_id")

    if not client_name or not gmail_address:
        return error_response("INVALID_REQUEST", "client_name and gmail_address are required")

    try:
        client_doc = require_msuite_client_auth(client_name)
    except Exception as e:
        return error_response("AUTH_FAILED", str(e))

    ca_name = frappe.db.get_value(
        "MSuite Connected Account",
        {"client": client_doc.name, "platform": Platform.GMAIL, "account_id": gmail_address},
        "name"
    )
    if not ca_name:
        return error_response("NOT_FOUND", f"Gmail account {gmail_address} not connected")

    ca = frappe.get_doc("MSuite Connected Account", ca_name)

    # Refresh when missing or expiring within the next 60s — a token that
    # expires mid-request fails the Gmail call anyway
    if not ca.access_token or not ca.token_expiry or ca.token_expiry <= add_to_date(now_datetime(), seconds=60):
        try:
            refresh_token_fn(ca.name)
            ca = frappe.get_doc("MSuite Connected Account", ca_name)
        except Exception as e:
            return error_response("TOKEN_REFRESH_FAILED", f"Could not refresh Gmail token: {str(e)}")

    access_token = ca.get_password("access_token")
    return poll_gmail(access_token, history_id)


def poll_gmail(access_token: str, history_id=None):
    """Gmail history/list polling. Shared with `api/v1/mail_relay.py`.
    Returns an already-wrapped success_response/error_response."""
    headers = {"Authorization": f"Bearer {access_token}"}

    # Fetch messages. If history_id is provided, use the Gmail History list API,
    # otherwise list current unread messages or the latest 20 messages.
    messages_to_return = []
    new_history_id = history_id

    try:
        if history_id:
            # Call Gmail history API
            history_url = f"https://gmail.googleapis.com/gmail/v1/users/me/history"
            params = {"startHistoryId": str(history_id), "maxResults": 100}
            resp = requests.get(history_url, headers=headers, params=params, timeout=30)
            if resp.status_code == 200:
                history_data = resp.json()
                new_history_id = history_data.get("historyId", history_id)
                histories = history_data.get("history", [])
                message_ids = set()
                for h in histories:
                    # Collect messageAdded/messages
                    for ma in h.get("messagesAdded", []):
                        msg_obj = ma.get("message", {})
                        if msg_obj.get("id"):
                            message_ids.add(msg_obj["id"])
                
                # Fetch full content for each unique message ID
                for mid in message_ids:
                    msg_detail = _fetch_message_details(mid, headers)
                    if msg_detail:
                        messages_to_return.append(msg_detail)
            else:
                # If history ID is too old (HTTP 404/410), fallback to general listing
                history_id = None

        if not history_id:
            # Fetch latest 20 messages
            list_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
            params = {"maxResults": 20}
            resp = requests.get(list_url, headers=headers, params=params, timeout=30)
            if resp.status_code == 200:
                list_data = resp.json()
                # Get the current history ID of the profile for future incremental checks
                profile_url = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
                p_resp = requests.get(profile_url, headers=headers, timeout=10)
                if p_resp.status_code == 200:
                    new_history_id = p_resp.json().get("historyId")

                for msg_ref in list_data.get("messages", []):
                    mid = msg_ref.get("id")
                    msg_detail = _fetch_message_details(mid, headers)
                    if msg_detail:
                        messages_to_return.append(msg_detail)

        return success_response({
            "messages": messages_to_return,
            "new_history_id": new_history_id
        })
    except Exception as e:
        return error_response("POLL_FAILED", f"Failed to poll Gmail: {str(e)}")


# Never forward these to the client inbox: drafts aren't messages yet,
# and spam/trash would open junk conversations for every phishing email.
_SKIPPED_LABELS = {"DRAFT", "SPAM", "TRASH"}


def _fetch_message_details(message_id: str, headers: dict) -> dict | None:
    """Helper to fetch and normalize Gmail message content."""
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"
    resp = requests.get(url, headers=headers, timeout=20)
    if resp.status_code != 200:
        return None

    data = resp.json()
    if _SKIPPED_LABELS & set(data.get("labelIds") or []):
        return None
    payload = data.get("payload", {})
    headers_list = payload.get("headers", [])

    # Extract headers
    msg_headers = {}
    for h in headers_list:
        name = h.get("name", "").lower()
        msg_headers[name] = h.get("value", "")

    # Parse body parts
    body_text = ""
    body_html = ""
    attachments = []

    def parse_parts(parts):
        nonlocal body_text, body_html, attachments
        for part in parts:
            mime_type = part.get("mimeType", "")
            body_data = part.get("body", {}).get("data", "")
            filename = part.get("filename", "")

            if filename:
                # This is an attachment
                attachments.append({
                    "filename": filename,
                    "mime_type": mime_type,
                    "attachment_id": part.get("body", {}).get("attachmentId", ""),
                    "size": part.get("body", {}).get("size", 0),
                })
            elif mime_type == "text/plain" and body_data:
                body_text += base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
            elif mime_type == "text/html" and body_data:
                body_html += base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
            elif part.get("parts"):
                parse_parts(part["parts"])

    if payload.get("parts"):
        parse_parts(payload["parts"])
    else:
        # Single part body
        body_data = payload.get("body", {}).get("data", "")
        mime_type = payload.get("mimeType", "")
        if mime_type == "text/plain" and body_data:
            body_text = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
        elif mime_type == "text/html" and body_data:
            body_html = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")

    # Normalize response fields for client side ingestion
    return {
        "message_id": message_id,
        "thread_id": data.get("threadId"),
        "history_id": data.get("historyId"),
        "internal_date": data.get("internalDate"),
        "from": msg_headers.get("from", ""),
        "to": msg_headers.get("to", ""),
        "cc": msg_headers.get("cc", ""),
        "bcc": msg_headers.get("bcc", ""),
        "subject": msg_headers.get("subject", ""),
        "in_reply_to": msg_headers.get("in-reply-to", ""),
        "message_id_header": msg_headers.get("message-id", ""),
        "body_text": body_text,
        "body_html": body_html,
        "attachments": attachments,
        "label_ids": data.get("labelIds", []),
    }


def fetch_gmail_attachment(access_token: str, message_id: str, attachment_id: str) -> dict:
    """Download one Gmail attachment's bytes.

    Returns {content: <standard base64>, ...} — Gmail encodes with the
    URL-safe alphabet, so it is re-encoded here to match the Graph backend
    and give the client one contract (see G8 in the connector plan).
    """
    resp = requests.get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/attachments/{attachment_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Gmail attachment fetch failed: HTTP {resp.status_code} {resp.text[:300]}")

    data = resp.json()
    raw = base64.urlsafe_b64decode(data.get("data") or "")
    return {
        "content": base64.b64encode(raw).decode("ascii"),
        "filename": "",  # Gmail returns bytes only; the name came with the message metadata
        "mime_type": "",
        "size": data.get("size") or len(raw),
    }


# ── Gmail push (users.watch) registration ────────────────────────────────
#
# Google Pub/Sub push only fires for mailboxes that have an active
# `users.watch` registration, and registrations expire after 7 days.
# Without these two functions the `receive_gmail_push` webhook never
# receives anything and ingestion silently degrades to the client's
# cron polling.
#
# The Pub/Sub topic is site configuration, not schema:
#   bench --site <provider-site> set-config gmail_pubsub_topic \
#       "projects/<gcp-project>/topics/<topic>"
# When unset, watch registration is skipped and polling remains the
# only ingestion path (which still works).


def register_gmail_watch(connected_account_name: str) -> dict | None:
    """Register (or re-register) a Gmail watch for one connected account.

    Safe to call repeatedly — Gmail treats it as an upsert. Returns the
    watch response ({historyId, expiration}) or None when skipped/failed.
    """
    topic = frappe.conf.get("gmail_pubsub_topic")
    if not topic:
        return None

    ca = frappe.get_doc("MSuite Connected Account", connected_account_name)
    if ca.platform != Platform.GMAIL:
        return None

    if not ca.access_token or not ca.token_expiry or ca.token_expiry <= add_to_date(now_datetime(), seconds=60):
        refresh_token_fn(ca.name)
        ca = frappe.get_doc("MSuite Connected Account", ca.name)

    access_token = ca.get_password("access_token")
    resp = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/watch",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"topicName": topic, "labelIds": ["INBOX"], "labelFilterBehavior": "INCLUDE"},
        timeout=30,
    )
    if resp.status_code != 200:
        frappe.log_error(
            title=f"Gmail watch registration failed ({ca.account_id})",
            message=f"HTTP {resp.status_code}: {resp.text[:500]}",
        )
        return None

    # The historyId in the watch response is deliberately dropped: MSuite
    # Connected Account has no cursor field, and the client keeps its own
    # (MSuite Email Account.history_id) which is the one poll_new_messages reads.
    return resp.json()


def renew_gmail_watches() -> None:
    """Daily cron: re-register the watch for every active Gmail account.

    Watches expire after 7 days; renewing daily keeps a 6-day safety
    margin over worker downtime. No-op when gmail_pubsub_topic is unset.
    """
    if not frappe.conf.get("gmail_pubsub_topic"):
        return

    accounts = frappe.get_all(
        "MSuite Connected Account",
        filters={"platform": Platform.GMAIL, "status": "Active"},
        pluck="name",
    )
    for name in accounts:
        try:
            register_gmail_watch(name)
        except Exception:
            frappe.log_error(
                title=f"Gmail watch renewal failed ({name})",
                message=frappe.get_traceback(),
            )
