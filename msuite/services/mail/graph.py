"""
Microsoft Graph mail backend for the provider's mail relay.

Emits the SAME normalized dict shape as
`gmail_relay._fetch_message_details`, which is the contract that lets the
client's `inbox_channel.parse()`, dispatcher, dedup and threading stay
completely unchanged between Gmail and Outlook. Anything added here must
be added to both or to neither — `test_graph_relay` asserts the key sets
are identical.

Inbound is delta query, not change-notification subscriptions: no public
validation endpoint, no ~3-day renewal cron, no lifecycle notifications,
and it reuses the client's existing poll cron. Subscriptions only buy
latency and are deferred.

Two folders are polled, not one. Gmail's `messages.list` is label-agnostic
so the client sees its own outbound copies for free; Graph's delta is
per-folder, so Sent Items must be polled explicitly or the pair-handle and
outbound-echo logic in `parse()` never sees agent-sent mail.
"""
import json
from datetime import datetime, timedelta, timezone

import requests

GRAPH = "https://graph.microsoft.com/v1.0"

# Well-known folder ids. Sent Items matters — see module docstring.
_FOLDERS = ("inbox", "sentitems")

_DELTA_SELECT = (
    "id,conversationId,internetMessageId,subject,from,toRecipients,"
    "ccRecipients,bccRecipients,receivedDateTime,body,hasAttachments,isDraft"
)

# On a cursor reset we pull a bounded window rather than the whole mailbox.
# Delta supports only `receivedDateTime ge|gt` as a $filter.
_INITIAL_SYNC_DAYS = 7

# Hard stop on delta pagination. A malformed nextLink that returns itself
# would otherwise spin forever inside one HTTP request.
_MAX_PAGES = 50

_PAGE_SIZE = 50


class GraphError(RuntimeError):
    """Non-retryable Graph failure. Carries the HTTP status for the caller."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class GraphThrottled(GraphError):
    """429 that survived the single bounded retry."""


class _DeltaExpired(Exception):
    """410 Gone — the delta token is too old. Internal; never escapes poll_messages()."""


def _request(method: str, url: str, access_token: str, **kwargs) -> requests.Response:
    """One Graph call with a single bounded retry on 429.

    ponytail: one retry honouring Retry-After, no token bucket. Graph's
    per-mailbox limits are generous relative to one poll every few minutes;
    add per-mailbox pacing only if throughput actually becomes a problem.
    """
    headers = kwargs.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {access_token}"
    kwargs.setdefault("timeout", 30)

    resp = requests.request(method, url, headers=headers, **kwargs)
    if resp.status_code != 429:
        return resp

    try:
        delay = int(resp.headers.get("Retry-After", "2"))
    except (TypeError, ValueError):
        delay = 2
    # Cap the wait: this runs inside a client HTTP request, so a long
    # Retry-After must fail fast rather than hold the connection open.
    if delay > 10:
        raise GraphThrottled(f"Graph throttled, Retry-After={delay}s", 429)

    import time

    time.sleep(delay)
    resp = requests.request(method, url, headers=headers, **kwargs)
    if resp.status_code == 429:
        raise GraphThrottled("Graph throttled after one retry", 429)
    return resp


# ── Inbound ──────────────────────────────────────────────────────────────


def poll_messages(access_token: str, cursor: str | None) -> tuple[list[dict], str]:
    """Drain both folders' delta streams.

    `cursor` is the JSON blob stored in `MSuite Email Account.delta_link`:
    {"inbox": "<deltaLink>", "sentitems": "<deltaLink>"}. Returns
    (normalized messages, new cursor JSON).
    """
    cursors = _parse_cursor(cursor)
    messages: list[dict] = []
    new_cursors: dict[str, str] = {}

    for folder in _FOLDERS:
        link = cursors.get(folder)
        try:
            folder_messages, delta_link = _drain_delta(access_token, folder, link)
        except _DeltaExpired:
            # Token too old. Bounded resync, NOT a full-mailbox pull.
            folder_messages, delta_link = _drain_delta(access_token, folder, None)
        messages.extend(folder_messages)
        # Keep the old link when a round ends without one, so a partial
        # drain can't rewind the cursor to the start of the window.
        new_cursors[folder] = delta_link or (link or "")

    # Delta order isn't guaranteed, and the client persists conversation
    # state per message as it goes — same reason the Gmail path sorts.
    messages.sort(key=lambda m: int(m.get("internal_date") or 0))
    return messages, json.dumps(new_cursors)


def _parse_cursor(cursor: str | None) -> dict:
    if not cursor:
        return {}
    try:
        parsed = json.loads(cursor)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        # A corrupt cursor must degrade to a bounded resync, not an exception.
        return {}


def _drain_delta(access_token: str, folder: str, delta_link: str | None) -> tuple[list[dict], str]:
    if delta_link:
        url, params = delta_link, None
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=_INITIAL_SYNC_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        url = f"{GRAPH}/me/mailFolders/{folder}/messages/delta"
        params = {
            "$select": _DELTA_SELECT,
            "$filter": f"receivedDateTime ge {since}",
            "$top": _PAGE_SIZE,
        }

    out: list[dict] = []
    for _ in range(_MAX_PAGES):
        resp = _request(
            "GET", url, access_token, params=params, headers={"Prefer": f"odata.maxpagesize={_PAGE_SIZE}"}
        )
        if resp.status_code == 410:
            raise _DeltaExpired
        if resp.status_code != 200:
            raise GraphError(f"Graph delta {folder} failed: HTTP {resp.status_code} {resp.text[:300]}", resp.status_code)

        data = resp.json()
        for raw in data.get("value") or []:
            normalized = _normalize(raw, access_token)
            if normalized:
                out.append(normalized)

        next_link = data.get("@odata.nextLink")
        if next_link:
            url, params = next_link, None
            continue
        return out, data.get("@odata.deltaLink") or ""

    raise GraphError(f"Graph delta {folder} exceeded {_MAX_PAGES} pages", 0)


def _normalize(raw: dict, access_token: str) -> dict | None:
    """Map one Graph message onto the Gmail-shaped normalized dict."""
    # Delta is a COLLECTION-level stream: it also emits tombstones for
    # messages deleted or moved out of the folder. Those carry only an id —
    # no `from` — so feeding one to the client's parse() would mint a
    # Channel Handle with an empty address. Drop them.
    if "@removed" in raw:
        return None
    # Drafts aren't messages yet (Gmail drops the DRAFT label for the same
    # reason), and our own unsent reply drafts live in this stream too.
    if raw.get("isDraft"):
        return None

    message_id = raw.get("id") or ""
    if not message_id:
        return None

    body = raw.get("body") or {}
    is_html = (body.get("contentType") or "").lower() == "html"
    content = body.get("content") or ""

    attachments = []
    if raw.get("hasAttachments"):
        attachments = _fetch_attachment_metadata(message_id, access_token)

    return {
        "message_id": message_id,
        "thread_id": raw.get("conversationId") or "",
        # Gmail-only cursor field. Kept so the key set matches exactly.
        "history_id": None,
        "internal_date": _to_epoch_ms(raw.get("receivedDateTime")),
        "from": _format_recipient(raw.get("from")),
        "to": _format_recipients(raw.get("toRecipients")),
        "cc": _format_recipients(raw.get("ccRecipients")),
        "bcc": _format_recipients(raw.get("bccRecipients")),
        "subject": raw.get("subject") or "",
        # ponytail: delta doesn't return internetMessageHeaders, and Graph
        # threads replies by conversationId (which parse() uses as thread_id)
        # so In-Reply-To is not load-bearing here. Populate it with a
        # per-message $select follow-up only if a real threading gap shows up.
        "in_reply_to": "",
        "message_id_header": raw.get("internetMessageId") or "",
        "body_text": "" if is_html else content,
        "body_html": content if is_html else "",
        "attachments": attachments,
        # Gmail label vocabulary has no Graph equivalent; empty, not absent.
        "label_ids": [],
    }


def _fetch_attachment_metadata(message_id: str, access_token: str) -> list[dict]:
    """Attachment metadata only — same parity as the Gmail OAuth path,
    which also returns names/sizes without downloading content."""
    try:
        resp = _request(
            "GET",
            f"{GRAPH}/messages/{message_id}/attachments",
            access_token,
            params={"$select": "id,name,contentType,size"},
        )
        if resp.status_code != 200:
            return []
        return [
            {
                "filename": a.get("name") or "attachment",
                "mime_type": a.get("contentType") or "",
                "attachment_id": a.get("id") or "",
                "size": a.get("size") or 0,
            }
            for a in resp.json().get("value") or []
        ]
    except Exception:
        # Attachment metadata is cosmetic — never lose the message over it.
        return []


def fetch_attachment(access_token: str, message_id: str, attachment_id: str) -> dict:
    """Download one attachment's bytes.

    Returns {content: <standard base64>, filename, mime_type, size}. Graph
    hands back `contentBytes` already base64-encoded on the fileAttachment
    resource, so no re-encoding is needed — unlike Gmail, which uses the
    URL-safe alphabet.
    """
    resp = _request(
        "GET", f"{GRAPH}/me/messages/{message_id}/attachments/{attachment_id}", access_token
    )
    if resp.status_code != 200:
        raise GraphError(
            f"Attachment fetch failed: HTTP {resp.status_code} {resp.text[:300]}", resp.status_code
        )
    data = resp.json()
    # itemAttachment / referenceAttachment carry no contentBytes — only
    # fileAttachment does. Treat the others as "no content available"
    # rather than exploding the whole ingest.
    content = data.get("contentBytes")
    if not content:
        raise GraphError(
            f"Attachment {attachment_id} has no downloadable content "
            f"(@odata.type={data.get('@odata.type')})",
            0,
        )
    return {
        "content": content,
        "filename": data.get("name") or "attachment",
        "mime_type": data.get("contentType") or "",
        "size": data.get("size") or 0,
    }


def _to_epoch_ms(iso_value: str | None) -> str:
    """Graph gives ISO 8601; Gmail gives ms-epoch and the client's
    `parse_unix_timestamp` expects that."""
    if not iso_value:
        return ""
    try:
        cleaned = iso_value.replace("Z", "+00:00")
        return str(int(datetime.fromisoformat(cleaned).timestamp() * 1000))
    except (ValueError, TypeError):
        return ""


def _format_recipient(recipient: dict | None) -> str:
    """→ `Name <addr>`, the RFC form the client's parseaddr() consumes."""
    if not recipient:
        return ""
    email = (recipient.get("emailAddress") or {})
    address = email.get("address") or ""
    name = email.get("name") or ""
    if not address:
        return ""
    return f"{name} <{address}>" if name else address


def _format_recipients(recipients: list | None) -> str:
    return ", ".join(filter(None, (_format_recipient(r) for r in recipients or [])))


# ── Outbound ─────────────────────────────────────────────────────────────


def send_message(access_token: str, data: dict) -> dict:
    """Create a draft, then send it.

    NOT `POST /me/sendMail`: that returns 202 with an empty body, so there
    would be no id and no internetMessageId to dedup the Sent Items copy
    against when it comes back on the next delta sweep.

    Returns {message_id, thread_id, message_id_header}.

    The draft id does NOT survive the send — Exchange moves the item to
    Sent Items and reissues the id. `message_id_header` (internetMessageId)
    is assigned at draft creation and DOES survive, which is why the client
    stores it as the outbound row's secondary id: that is the only key the
    Sent Items copy can be deduped on.
    """
    reply_to_message_id = data.get("reply_to_message_id")
    body_html = data.get("body_html") or ""
    body_text = data.get("body_text") or ""
    body = (
        {"contentType": "HTML", "content": body_html}
        if body_html
        else {"contentType": "Text", "content": body_text}
    )

    if reply_to_message_id:
        # createReply is the ONLY correct way to thread a reply: Graph
        # refuses arbitrary RFC headers (internetMessageHeaders accepts
        # x-prefixed names only), so In-Reply-To/References cannot be set
        # by hand. createReply has Graph populate them.
        resp = _request(
            "POST", f"{GRAPH}/me/messages/{reply_to_message_id}/createReply", access_token, json={}
        )
        if resp.status_code not in (200, 201):
            raise GraphError(
                f"createReply failed: HTTP {resp.status_code} {resp.text[:300]}", resp.status_code
            )
        draft = resp.json()
        draft_id = draft.get("id")
        patch = _request(
            "PATCH", f"{GRAPH}/me/messages/{draft_id}", access_token, json={"body": body}
        )
        if patch.status_code not in (200, 201):
            raise GraphError(
                f"Draft update failed: HTTP {patch.status_code} {patch.text[:300]}", patch.status_code
            )
        draft = patch.json() or draft
    else:
        payload = {
            "subject": data.get("subject") or "",
            "body": body,
            "toRecipients": _to_graph_recipients(data.get("to")),
        }
        if data.get("cc"):
            payload["ccRecipients"] = _to_graph_recipients(data.get("cc"))
        if data.get("bcc"):
            payload["bccRecipients"] = _to_graph_recipients(data.get("bcc"))

        resp = _request("POST", f"{GRAPH}/me/messages", access_token, json=payload)
        if resp.status_code not in (200, 201):
            raise GraphError(
                f"Draft create failed: HTTP {resp.status_code} {resp.text[:300]}", resp.status_code
            )
        draft = resp.json()
        draft_id = draft.get("id")

    if not draft_id:
        raise GraphError("Graph returned a draft with no id", 0)

    send = _request("POST", f"{GRAPH}/me/messages/{draft_id}/send", access_token)
    if send.status_code not in (200, 202, 204):
        raise GraphError(
            f"Send failed: HTTP {send.status_code} {send.text[:300]}", send.status_code
        )

    return {
        "message_id": draft_id,
        "thread_id": draft.get("conversationId") or "",
        "message_id_header": draft.get("internetMessageId") or "",
    }


def _to_graph_recipients(value) -> list[dict]:
    if not value:
        return []
    addresses = value if isinstance(value, list) else [a.strip() for a in str(value).split(",")]
    return [{"emailAddress": {"address": a}} for a in addresses if a]
