# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

import unittest
from unittest.mock import patch, MagicMock

import frappe

from msuite.api.v1.analytics_agent import (
    _require_mcp_backend_key,
    get_mcp_client_credentials,
    proxy_tool_call,
)


def mock_frappe_throw(msg, exc_type):
    """Helper to mock frappe.throw() to raise the exception without context."""
    raise exc_type(msg)


class TestMcpBackendKeyGuard(unittest.TestCase):
    def test_rejects_when_key_not_configured(self):
        with patch.object(frappe, "conf", {"mcp_backend_shared_key": None}), \
             patch("frappe.throw", side_effect=mock_frappe_throw), \
             patch("frappe.logger"):
            with self.assertRaises(frappe.AuthenticationError):
                _require_mcp_backend_key()

    def test_rejects_missing_header(self):
        fake_request = MagicMock()
        fake_request.headers.get.return_value = None
        with patch.object(frappe, "conf", {"mcp_backend_shared_key": "secret123"}), \
             patch.object(frappe, "request", fake_request), \
             patch("frappe.throw", side_effect=mock_frappe_throw):
            with self.assertRaises(frappe.AuthenticationError):
                _require_mcp_backend_key()

    def test_rejects_wrong_key(self):
        fake_request = MagicMock()
        fake_request.headers.get.return_value = "wrong-key"
        with patch.object(frappe, "conf", {"mcp_backend_shared_key": "secret123"}), \
             patch.object(frappe, "request", fake_request), \
             patch("frappe.throw", side_effect=mock_frappe_throw):
            with self.assertRaises(frappe.AuthenticationError):
                _require_mcp_backend_key()

    def test_accepts_correct_key(self):
        fake_request = MagicMock()
        fake_request.headers.get.return_value = "secret123"
        with patch.object(frappe, "conf", {"mcp_backend_shared_key": "secret123"}), \
             patch.object(frappe, "request", fake_request):
            _require_mcp_backend_key()  # must not raise


class TestGetMcpClientCredentials(unittest.TestCase):
    def _authed_request(self):
        fake_request = MagicMock()
        fake_request.headers.get.return_value = "secret123"
        return fake_request

    def test_unauthenticated_call_is_rejected(self):
        fake_request = MagicMock()
        fake_request.headers.get.return_value = None
        with patch.object(frappe, "conf", {"mcp_backend_shared_key": "secret123"}), \
             patch.object(frappe, "request", fake_request), \
             patch("frappe.throw", side_effect=mock_frappe_throw):
            with self.assertRaises(frappe.AuthenticationError):
                get_mcp_client_credentials("tenant-a")

    def test_unknown_client_code_returns_error(self):
        fake_db = MagicMock()
        fake_db.get_value.return_value = None
        fake_db.exists.return_value = False
        with patch.object(frappe, "conf", {"mcp_backend_shared_key": "secret123"}), \
             patch.object(frappe, "request", self._authed_request()), \
             patch.object(frappe, "db", fake_db):
            result = get_mcp_client_credentials("no-such-tenant")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "CLIENT_NOT_FOUND")

    def test_inactive_client_returns_error(self):
        fake_db = MagicMock()
        fake_db.get_value.return_value = "CLIENT-1"
        fake_doc = MagicMock(status="Suspended")
        with patch.object(frappe, "conf", {"mcp_backend_shared_key": "secret123"}), \
             patch.object(frappe, "request", self._authed_request()), \
             patch.object(frappe, "db", fake_db), \
             patch.object(frappe, "get_doc", return_value=fake_doc):
            result = get_mcp_client_credentials("tenant-a")
        self.assertEqual(result["error_code"], "CLIENT_NOT_ACTIVE")

    def test_active_client_returns_credentials(self):
        fake_db = MagicMock()
        fake_db.get_value.return_value = "CLIENT-1"
        fake_doc = MagicMock(status="Active", api_key="key123", client_url="https://tenant-a.example.com")
        fake_doc.name = "CLIENT-1"
        with patch.object(frappe, "conf", {"mcp_backend_shared_key": "secret123"}), \
             patch.object(frappe, "request", self._authed_request()), \
             patch.object(frappe, "db", fake_db), \
             patch.object(frappe, "get_doc", return_value=fake_doc), \
             patch("frappe.utils.password.get_decrypted_password", return_value="secret-abc"):
            result = get_mcp_client_credentials("tenant-a")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"], {
            "client_url": "https://tenant-a.example.com",
            "api_key": "key123",
            "api_secret": "secret-abc",
        })


class TestProxyToolCallGated(unittest.TestCase):
    def test_unauthenticated_call_is_rejected(self):
        fake_request = MagicMock()
        fake_request.headers.get.return_value = None
        with patch.object(frappe, "conf", {"mcp_backend_shared_key": "secret123"}), \
             patch.object(frappe, "request", fake_request), \
             patch("frappe.throw", side_effect=mock_frappe_throw):
            with self.assertRaises(frappe.AuthenticationError):
                proxy_tool_call("tenant-a", "some.method", {})


if __name__ == "__main__":
    unittest.main()
