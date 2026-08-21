"""
Provider-side Microsoft Graph mail backend (services/mail/graph.py).

What this proves:

  • The normalized dict key set is IDENTICAL to Gmail's — the contract
    that keeps the client's parse()/dispatcher/dedup untouched.
  • Delta pagination is followed to exhaustion and the final deltaLink is
    what gets stored, for both folders.
  • 410 Gone resets to a BOUNDED 7-day resync, not a full-mailbox pull.
  • Tombstones (@removed) and drafts never reach the client. A tombstone
    carries only an id, so forwarding one would create a Channel Handle
    with an empty address.
  • Send returns both an id and an internetMessageId. The Graph id does not
    survive the send (Exchange reissues it in Sent Items), so the RFC id is
    the only usable dedup key for the outbound echo.
  • A reply goes through createReply — Graph refuses hand-set In-Reply-To.
  • 429 is retried once honouring Retry-After, then surfaces cleanly.

No network I/O — `requests` is mocked everywhere.
"""
import json
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from msuite.services.mail import graph

# The exact key set gmail_relay._fetch_message_details returns.
GMAIL_KEYS = {
    "message_id",
    "thread_id",
    "history_id",
    "internal_date",
    "from",
    "to",
    "cc",
    "bcc",
    "subject",
    "in_reply_to",
    "message_id_header",
    "body_text",
    "body_html",
    "attachments",
    "label_ids",
}


def _resp(payload, status=200, headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
    r.headers = headers or {}
    return r


def _graph_message(**overrides):
    msg = {
        "id": "AAMkAGgraph1=",
        "conversationId": "conv-1",
        "internetMessageId": "<abc123@contoso.com>",
        "subject": "Quote request",
        "from": {"emailAddress": {"name": "Buyer", "address": "buyer@acme.example"}},
        "toRecipients": [{"emailAddress": {"name": "Sales", "address": "sales@contoso.com"}}],
        "ccRecipients": [],
        "bccRecipients": [],
        "receivedDateTime": "2026-08-14T10:30:00Z",
        "body": {"contentType": "html", "content": "<p>How much?</p>"},
        "hasAttachments": False,
        "isDraft": False,
    }
    msg.update(overrides)
    return msg


class TestGraphNormalize(IntegrationTestCase):
    def test_delta_normalizes_to_gmail_shape(self):
        out = graph._normalize(_graph_message(), "tok")

        # The contract. If this drifts, the client's parse() silently loses
        # a field for one provider only.
        self.assertEqual(set(out.keys()), GMAIL_KEYS)

        self.assertEqual(out["message_id"], "AAMkAGgraph1=")
        self.assertEqual(out["thread_id"], "conv-1")
        self.assertEqual(out["message_id_header"], "<abc123@contoso.com>")
        self.assertEqual(out["from"], "Buyer <buyer@acme.example>")
        self.assertEqual(out["to"], "Sales <sales@contoso.com>")
        self.assertEqual(out["body_html"], "<p>How much?</p>")
        self.assertEqual(out["body_text"], "")
        # ms epoch, like Gmail's internalDate (2026-08-14T10:30:00Z)
        self.assertEqual(out["internal_date"], "1786703400000")

    def test_plaintext_body_lands_in_body_text(self):
        out = graph._normalize(
            _graph_message(body={"contentType": "text", "content": "plain words"}), "tok"
        )
        self.assertEqual(out["body_text"], "plain words")
        self.assertEqual(out["body_html"], "")

    def test_removed_tombstone_is_skipped(self):
        # Carries only an id — no `from`. Forwarding it would create a
        # Channel Handle with an empty platform_handle.
        self.assertIsNone(
            graph._normalize({"id": "x", "@removed": {"reason": "deleted"}}, "tok")
        )

    def test_drafts_skipped(self):
        self.assertIsNone(graph._normalize(_graph_message(isDraft=True), "tok"))

    def test_missing_id_skipped(self):
        self.assertIsNone(graph._normalize(_graph_message(id=""), "tok"))

    def test_attachments_fetched_only_when_flagged(self):
        with patch.object(graph, "_request") as req:
            req.return_value = _resp(
                {"value": [{"id": "att1", "name": "quote.pdf", "contentType": "application/pdf", "size": 12}]}
            )
            out = graph._normalize(_graph_message(hasAttachments=True), "tok")
        self.assertEqual(
            out["attachments"],
            [{"filename": "quote.pdf", "mime_type": "application/pdf", "attachment_id": "att1", "size": 12}],
        )

        with patch.object(graph, "_request") as req:
            graph._normalize(_graph_message(hasAttachments=False), "tok")
            req.assert_not_called()


class TestGraphPoll(IntegrationTestCase):
    def test_delta_paginates_and_stores_deltalink(self):
        page1 = {"value": [_graph_message(id="m1")], "@odata.nextLink": "https://graph/next"}
        page2 = {"value": [_graph_message(id="m2")], "@odata.deltaLink": "https://graph/delta-INBOX"}
        empty = {"value": [], "@odata.deltaLink": "https://graph/delta-SENT"}

        with patch.object(graph, "_request") as req:
            req.side_effect = [_resp(page1), _resp(page2), _resp(empty)]
            messages, cursor = graph.poll_messages("tok", None)

        self.assertEqual([m["message_id"] for m in messages], ["m1", "m2"])
        self.assertEqual(
            json.loads(cursor),
            {"inbox": "https://graph/delta-INBOX", "sentitems": "https://graph/delta-SENT"},
        )

    def test_both_folders_polled(self):
        """Sent Items is not optional: Gmail's list is label-agnostic, so
        without it the client never sees agent-sent mail and the pair-handle
        logic in parse() breaks."""
        with patch.object(graph, "_request") as req:
            req.return_value = _resp({"value": [], "@odata.deltaLink": "d"})
            graph.poll_messages("tok", None)

        urls = [c.args[1] for c in req.call_args_list]
        self.assertTrue(any("/mailFolders/inbox/messages/delta" in u for u in urls))
        self.assertTrue(any("/mailFolders/sentitems/messages/delta" in u for u in urls))

    def test_delta_410_resets_to_bounded_resync(self):
        with patch.object(graph, "_request") as req:
            req.side_effect = [
                _resp({}, status=410),  # inbox delta expired
                _resp({"value": [], "@odata.deltaLink": "d1"}),  # bounded resync
                _resp({"value": [], "@odata.deltaLink": "d2"}),  # sentitems
            ]
            _messages, cursor = graph.poll_messages("tok", json.dumps({"inbox": "stale", "sentitems": None}))

        resync = req.call_args_list[1]
        params = resync.kwargs.get("params") or {}
        # Bounded window, not the whole mailbox.
        self.assertIn("receivedDateTime ge", params.get("$filter", ""))
        self.assertEqual(json.loads(cursor)["inbox"], "d1")

    def test_corrupt_cursor_degrades_to_resync(self):
        with patch.object(graph, "_request") as req:
            req.return_value = _resp({"value": [], "@odata.deltaLink": "d"})
            _messages, cursor = graph.poll_messages("tok", "not-json{{")
        self.assertEqual(json.loads(cursor)["inbox"], "d")

    def test_messages_sorted_by_date(self):
        older = _graph_message(id="old", receivedDateTime="2026-08-14T09:00:00Z")
        newer = _graph_message(id="new", receivedDateTime="2026-08-14T11:00:00Z")
        with patch.object(graph, "_request") as req:
            req.side_effect = [
                _resp({"value": [newer, older], "@odata.deltaLink": "d1"}),
                _resp({"value": [], "@odata.deltaLink": "d2"}),
            ]
            messages, _cursor = graph.poll_messages("tok", None)
        self.assertEqual([m["message_id"] for m in messages], ["old", "new"])

    def test_partial_drain_does_not_rewind_cursor(self):
        """A round that ends without a deltaLink keeps the previous one."""
        with patch.object(graph, "_request") as req:
            req.side_effect = [
                _resp({"value": []}),  # inbox: no deltaLink
                _resp({"value": [], "@odata.deltaLink": "d2"}),
            ]
            _messages, cursor = graph.poll_messages("tok", json.dumps({"inbox": "keepme"}))
        self.assertEqual(json.loads(cursor)["inbox"], "keepme")


class TestGraphSend(IntegrationTestCase):
    def test_send_new_thread_returns_ids(self):
        draft = {"id": "draft-1", "conversationId": "conv-9", "internetMessageId": "<new@contoso.com>"}
        with patch.object(graph, "_request") as req:
            req.side_effect = [_resp(draft, status=201), _resp({}, status=202)]
            out = graph.send_message(
                "tok", {"to": "buyer@acme.example", "subject": "Hi", "body_html": "<p>Hi</p>"}
            )

        self.assertEqual(out["message_id"], "draft-1")
        self.assertEqual(out["thread_id"], "conv-9")
        # The Graph id is reissued when Exchange moves the item to Sent
        # Items, so this RFC id is the only key the echo can dedup on.
        self.assertEqual(out["message_id_header"], "<new@contoso.com>")

        create = req.call_args_list[0]
        self.assertTrue(create.args[1].endswith("/me/messages"))
        self.assertEqual(
            create.kwargs["json"]["toRecipients"],
            [{"emailAddress": {"address": "buyer@acme.example"}}],
        )
        self.assertTrue(req.call_args_list[1].args[1].endswith("/send"))

    def test_reply_uses_createreply_and_never_sets_in_reply_to(self):
        with patch.object(graph, "_request") as req:
            req.side_effect = [
                _resp({"id": "reply-draft"}, status=201),
                _resp(
                    {"id": "reply-draft", "conversationId": "conv-1", "internetMessageId": "<r@c.com>"}
                ),
                _resp({}, status=202),
            ]
            out = graph.send_message(
                "tok", {"reply_to_message_id": "parent-1", "body_html": "<p>Sure</p>"}
            )

        self.assertIn("/messages/parent-1/createReply", req.call_args_list[0].args[1])
        self.assertEqual(out["message_id_header"], "<r@c.com>")

        # Graph only accepts x-prefixed custom headers; hand-setting
        # In-Reply-To silently does nothing, which is why createReply exists.
        for call in req.call_args_list:
            body = call.kwargs.get("json") or {}
            self.assertNotIn("internetMessageHeaders", body)

    def test_send_failure_raises_grapherror(self):
        with patch.object(graph, "_request") as req:
            req.return_value = _resp({"error": "bad"}, status=400)
            with self.assertRaises(graph.GraphError):
                graph.send_message("tok", {"to": "x@y.com", "body_text": "hi"})


class TestGraphThrottling(IntegrationTestCase):
    def test_429_retries_once_honouring_retry_after(self):
        with patch.object(graph.requests, "request") as req, patch("time.sleep") as slept:
            req.side_effect = [
                _resp({}, status=429, headers={"Retry-After": "1"}),
                _resp({"value": []}, status=200),
            ]
            resp = graph._request("GET", "https://graph/x", "tok")

        self.assertEqual(resp.status_code, 200)
        slept.assert_called_once_with(1)
        self.assertEqual(req.call_count, 2)

    def test_429_twice_raises_throttled(self):
        with patch.object(graph.requests, "request") as req, patch("time.sleep"):
            req.side_effect = [
                _resp({}, status=429, headers={"Retry-After": "1"}),
                _resp({}, status=429, headers={"Retry-After": "1"}),
            ]
            with self.assertRaises(graph.GraphThrottled):
                graph._request("GET", "https://graph/x", "tok")

    def test_long_retry_after_fails_fast(self):
        """This runs inside a client HTTP request — never hold the
        connection open for a multi-minute Retry-After."""
        with patch.object(graph.requests, "request") as req, patch("time.sleep") as slept:
            req.return_value = _resp({}, status=429, headers={"Retry-After": "300"})
            with self.assertRaises(graph.GraphThrottled):
                graph._request("GET", "https://graph/x", "tok")
        slept.assert_not_called()
