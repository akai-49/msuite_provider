"""Payment API endpoints."""
import json

import frappe

from msuite.constants import ErrorCode
from msuite.utils.validators import (
    success_response,
    error_response,
    require_system_manager_or_msuite_manager,
    require_permission,
)


@frappe.whitelist()
def create_payment_request(
    invoice_name: str,
    gateway_account: str,
    send_email: bool = True,
) -> dict:
    """Creates Payment Request and returns payment URL."""
    try:
        require_system_manager_or_msuite_manager()
        from msuite.services.payment_service import create_payment_request as svc_create
        pr_name = svc_create(invoice_name, gateway_account, send_email)
        payment_url = frappe.db.get_value("Payment Request", pr_name, "payment_url")
        return success_response({
            "payment_request": pr_name,
            "payment_url": payment_url,
        })
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.INVOICE_NOT_FOUND, str(e))


@frappe.whitelist(allow_guest=True)
def webhook(gateway: str) -> dict:
    """
    Receives payment gateway webhooks.
    allow_guest=True: gateway cannot authenticate.
    Returns HTTP 200 always.
    """
    try:
        raw_body = frappe.request.get_data()
        signature_header = (
            frappe.request.headers.get("X-Razorpay-Signature")
            or frappe.request.headers.get("Stripe-Signature")
            or frappe.request.headers.get("X-HITPAY-HMAC256")
            or ""
        )

        payload = json.loads(raw_body) if raw_body else {}

        from msuite.services.payment_service import process_webhook as svc_process
        result = svc_process(gateway, payload, raw_body, signature_header)
        return result

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"MSuite: Webhook error for {gateway}")
        return {"status": "error", "message": str(e)}


@frappe.whitelist()
def get_payment_status(invoice_name: str) -> dict:
    """Returns payment status for an invoice."""
    try:
        require_permission("Sales Invoice", invoice_name, "read")
        from msuite.services.payment_service import get_payment_status as svc_status
        data = svc_status(invoice_name)
        return success_response(data)
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.INVOICE_NOT_FOUND, str(e))
