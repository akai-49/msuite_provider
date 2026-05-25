"""Shared validation utilities and API response helpers."""
import hmac

import frappe
from frappe.utils import getdate
from frappe.utils.password import get_decrypted_password


def success_response(data: dict | list) -> dict:
    """Wraps data in standard success envelope."""
    return {"status": "success", "data": data}


def error_response(error_code: str, message: str) -> dict:
    """Returns standard error envelope."""
    return {"status": "error", "error_code": error_code, "message": message}


def require_customer_exists(customer: str) -> None:
    """Raises frappe.DoesNotExistError if customer not found."""
    if not frappe.db.exists("Customer", customer):
        frappe.throw(f"Customer {customer} not found", frappe.DoesNotExistError)


def require_permission(doctype: str, name: str | None = None, ptype: str = "read") -> None:
    """Validates current user has permission."""
    if name:
        if not frappe.has_permission(doctype, ptype, name):
            frappe.throw(f"No {ptype} permission for {doctype} {name}", frappe.PermissionError)
    else:
        if not frappe.has_permission(doctype, ptype):
            frappe.throw(f"No {ptype} permission for {doctype}", frappe.PermissionError)


def require_system_manager_or_msuite_manager() -> None:
    """Validates current user has System Manager or MSuite Manager role."""
    roles = frappe.get_roles()
    if "System Manager" not in roles and "MSuite Manager" not in roles:
        frappe.throw("Requires System Manager or MSuite Manager role", frappe.PermissionError)


def validate_date_string(date_str: str, field_name: str) -> None:
    """Validates date string is valid ISO format YYYY-MM-DD."""
    try:
        getdate(date_str)
    except Exception:
        frappe.throw(
            f"Invalid date format for {field_name}: {date_str}. Expected YYYY-MM-DD.",
            frappe.ValidationError,
        )


def validate_positive_float(value: float, field_name: str, allow_zero: bool = True) -> None:
    """Validates value is >= 0 (or > 0 if allow_zero=False)."""
    if allow_zero and value < 0:
        frappe.throw(f"{field_name} must be >= 0, got {value}", frappe.ValidationError)
    elif not allow_zero and value <= 0:
        frappe.throw(f"{field_name} must be > 0, got {value}", frappe.ValidationError)


def require_msuite_client_auth(client_identifier: str):
    """Validate inbound request from a client site via shared-secret headers.

    Mirror of the provider→client auth (X-MSuite-Provider-Key/Secret) that
    `_post_to_client` in `api/v1/webhook.py` already uses — here the
    direction is reversed: the client sends them so the provider can
    authenticate calls to whitelisted guest endpoints.

    Lookup is flexible: `client_identifier` may be either the MSuite
    Client's doc `name` (e.g. `E2E-CLIENT`) or the `client_code` field
    (e.g. `e2e-client-1ce99e`). Clients activated by older provider
    builds that only persisted `client_code` on the client side still
    work.

    Header names are kept identical to the reverse direction so the
    client has one set of credentials (api_key + api_secret on
    MSuite Settings) and one header convention regardless of direction.

    Args:
        client_identifier: MSuite Client doc name OR client_code.

    Returns:
        The validated MSuite Client document.

    Raises:
        AuthenticationError: missing / mismatched / unknown client.
    """
    key = (frappe.request.headers.get("X-MSuite-Provider-Key") or "").strip()
    secret = (frappe.request.headers.get("X-MSuite-Provider-Secret") or "").strip()

    if not key or not secret:
        frappe.throw(
            "Missing X-MSuite-Provider-Key / X-MSuite-Provider-Secret headers",
            frappe.AuthenticationError,
        )

    doc_name = _resolve_client_doc_name(client_identifier)
    if not doc_name:
        frappe.throw(
            f"MSuite Client not found: {client_identifier}",
            frappe.AuthenticationError,
        )

    client_doc = frappe.get_doc("MSuite Client", doc_name)
    stored_key = client_doc.api_key or ""
    stored_secret = get_decrypted_password(
        "MSuite Client", client_doc.name, "api_secret", raise_exception=False,
    ) or ""

    # Constant-time comparison — prevents timing attacks when the secret
    # has diverged on one side.
    if not (hmac.compare_digest(key, stored_key) and hmac.compare_digest(secret, stored_secret)):
        frappe.throw("Invalid client credentials", frappe.AuthenticationError)

    if client_doc.status != "Active":
        frappe.throw(
            f"Client is {client_doc.status or 'not active'} — cannot call this endpoint",
            frappe.PermissionError,
        )

    return client_doc


def _resolve_client_doc_name(identifier: str) -> str | None:
    """Map either a doc-name or a client_code to the MSuite Client doc name."""
    if not identifier:
        return None
    if frappe.db.exists("MSuite Client", identifier):
        return identifier
    by_code = frappe.db.get_value("MSuite Client", {"client_code": identifier}, "name")
    return by_code or None
