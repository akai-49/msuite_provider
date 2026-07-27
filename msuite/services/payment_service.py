"""
Payment orchestration service.

Orchestrates ERPNext's native Payment Request and Payment Entry.
Adds HMAC verification, idempotency, and grace period handling.
"""
import hashlib
import hmac

import frappe
from frappe.utils import today

from msuite.constants import MSUITE_LOGGER_NAME
from msuite.exceptions import (
    PaymentVerificationError,
    DuplicatePaymentError,
    MSuiteError,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)


def create_payment_request(
    invoice_name: str,
    gateway_account: str,
    send_email: bool = True,
) -> str:
    """
    Creates a Payment Request for an unpaid Sales Invoice.

    Args:
        invoice_name: Sales Invoice name
        gateway_account: ERPNext Payment Gateway Account name
        send_email: Whether to send payment link to customer

    Returns:
        Payment Request document name

    Raises:
        frappe.DoesNotExistError: invoice not found
        DuplicatePaymentError: Payment Request already exists
    """
    if not frappe.db.exists("Sales Invoice", invoice_name):
        frappe.throw(f"Sales Invoice {invoice_name} not found", frappe.DoesNotExistError)

    outstanding = frappe.db.get_value("Sales Invoice", invoice_name, "outstanding_amount")
    if not outstanding or outstanding <= 0:
        frappe.throw(f"Invoice {invoice_name} is already paid", frappe.ValidationError)

    existing_pr = frappe.db.exists(
        "Payment Request",
        {"reference_name": invoice_name, "status": ["in", ["Initiated", "Requested"]]},
    )
    if existing_pr:
        frappe.throw(
            f"Payment Request already exists for invoice {invoice_name}",
            DuplicatePaymentError,
        )

    from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

    pr = make_payment_request(
        dt="Sales Invoice",
        dn=invoice_name,
        submit_doc=True,
        mute_email=not send_email,
        payment_gateway_account=gateway_account,
        return_doc=True,
    )

    logger.info(f"Created Payment Request {pr.name} for invoice {invoice_name}")
    return pr.name


def process_webhook(
    gateway: str,
    payload: dict,
    raw_body: bytes,
    signature_header: str,
) -> dict:
    """
    Processes incoming payment gateway webhook.

    Args:
        gateway: Gateway identifier string
        payload: Parsed JSON payload dict
        raw_body: Raw request body bytes
        signature_header: Signature from request header

    Returns:
        {"status": "processed", "payment_entry": str}

    Raises:
        PaymentVerificationError: HMAC invalid
        DuplicatePaymentError: Already processed
    """
    if not verify_hmac_signature(gateway, raw_body, signature_header):
        frappe.throw("HMAC signature verification failed", PaymentVerificationError)

    gateway_reference = _extract_gateway_reference(gateway, payload)

    if frappe.db.exists("Payment Entry", {"reference_no": gateway_reference}):
        frappe.throw(
            f"Payment already processed: {gateway_reference}",
            DuplicatePaymentError,
        )

    pr = frappe.db.get_value(
        "Payment Request",
        {"reference_no": gateway_reference},
        ["name", "reference_name"],
        as_dict=True,
    )

    # No guessing: a webhook whose gateway reference matches no Payment
    # Request must NOT be applied to some arbitrary open invoice. Doing so
    # would mark an unrelated customer's invoice as paid. Log and skip.
    if not pr:
        logger.warning(
            f"No Payment Request matches gateway reference {gateway_reference!r} "
            f"({gateway}); skipping — no Payment Entry created."
        )
        return {"status": "unmatched", "payment_entry": None}

    paid_amount = _extract_paid_amount(gateway, payload)
    entry_name = create_payment_entry(pr.name, gateway_reference, paid_amount)
    if pr.reference_name:
        _reactivate_subscription_if_grace_period(pr.reference_name)

    return {"status": "processed", "payment_entry": entry_name}


def create_payment_entry(
    payment_request_name: str,
    gateway_reference: str,
    paid_amount: float,
    payment_date: str | None = None,
) -> str:
    """
    Creates and submits a Payment Entry.

    Args:
        payment_request_name: Payment Request name
        gateway_reference: Gateway transaction ID
        paid_amount: Amount paid
        payment_date: Optional date, defaults to today

    Returns:
        Payment Entry document name
    """
    pr_doc = frappe.get_doc("Payment Request", payment_request_name)
    invoice_name = pr_doc.reference_name
    invoice_doc = frappe.get_doc("Sales Invoice", invoice_name)

    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.party_type = "Customer"
    pe.party = invoice_doc.customer
    pe.paid_amount = paid_amount
    pe.received_amount = paid_amount
    pe.reference_no = gateway_reference
    pe.reference_date = payment_date or today()
    pe.paid_to = frappe.db.get_value(
        "Company", invoice_doc.company, "default_cash_account"
    ) or frappe.db.get_value("Company", invoice_doc.company, "default_bank_account")
    pe.paid_from = frappe.db.get_value(
        "Company", invoice_doc.company, "default_receivable_account"
    )

    pe.append("references", {
        "reference_doctype": "Sales Invoice",
        "reference_name": invoice_name,
        "allocated_amount": paid_amount,
    })

    pe.insert(ignore_permissions=True)
    pe.submit()

    logger.info(f"Created Payment Entry {pe.name} for invoice {invoice_name}")
    return pe.name


def get_payment_status(invoice_name: str) -> dict:
    """
    Returns payment status for a Sales Invoice.

    Returns:
        Payment status dict
    """
    if not frappe.db.exists("Sales Invoice", invoice_name):
        return {"error": f"Invoice {invoice_name} not found"}

    inv = frappe.get_doc("Sales Invoice", invoice_name)

    pr = frappe.db.get_value(
        "Payment Request",
        {"reference_name": invoice_name},
        ["name", "status", "payment_url"],
        as_dict=True,
    )

    payment_entries = frappe.get_list(
        "Payment Entry Reference",
        filters={"reference_doctype": "Sales Invoice", "reference_name": invoice_name},
        fields=["parent", "allocated_amount"],
    )

    entries = []
    for pe_ref in payment_entries:
        # Only include submitted Payment Entries
        pe_data = frappe.db.get_value(
            "Payment Entry", pe_ref.parent, ["docstatus", "posting_date"], as_dict=True
        )
        if not pe_data or pe_data.docstatus != 1:
            continue
        entries.append({
            "entry": pe_ref.parent,
            "amount": float(pe_ref.allocated_amount or 0),
            "date": str(pe_data.posting_date) if pe_data.posting_date else None,
        })

    return {
        "invoice": invoice_name,
        "status": inv.status,
        "grand_total": float(inv.grand_total),
        "outstanding_amount": float(inv.outstanding_amount),
        "paid_amount": float(inv.grand_total - inv.outstanding_amount),
        "payment_request": pr.name if pr else None,
        "payment_request_status": pr.status if pr else None,
        "payment_url": pr.payment_url if pr else None,
        "payment_entries": entries,
    }


def verify_hmac_signature(
    gateway: str,
    raw_body: bytes,
    signature_header: str,
) -> bool:
    """
    Gateway-specific HMAC verification.

    Args:
        gateway: Gateway identifier
        raw_body: Raw request body bytes
        signature_header: Full signature header value

    Returns:
        bool: True if valid
    """
    try:
        secret = get_gateway_secret(gateway)
        if not secret:
            return False

        if gateway == "razorpay":
            expected = hmac.new(
                secret.encode("utf-8"), raw_body, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(expected, signature_header)

        elif gateway == "stripe":
            parts = dict(item.split("=", 1) for item in signature_header.split(","))
            timestamp = parts.get("t", "")
            sig = parts.get("v1", "")
            signed_payload = f"{timestamp}.".encode() + raw_body
            expected = hmac.new(
                secret.encode("utf-8"), signed_payload, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(expected, sig)

        elif gateway == "hitpay":
            expected = hmac.new(
                secret.encode("utf-8"), raw_body, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(expected, signature_header)

        return False
    except Exception:
        logger.warning(f"HMAC verification error for gateway {gateway}", exc_info=True)
        return False


def get_gateway_secret(gateway: str) -> str:
    """
    Returns HMAC webhook secret for gateway.

    Args:
        gateway: Gateway identifier

    Returns:
        Secret string

    Raises:
        MSuiteError: if secret not configured
    """
    secret = frappe.conf.get(f"{gateway}_webhook_secret")
    if secret:
        return secret

    gateway_account = frappe.db.get_value(
        "Payment Gateway Account",
        {"payment_gateway": gateway.title()},
        "name",
    )
    if gateway_account:
        secret = frappe.db.get_value(
            "Payment Gateway Account", gateway_account, "secret"
        )
    if not secret:
        frappe.throw(
            f"Webhook secret not configured for gateway {gateway}",
            MSuiteError,
        )
    return secret


def _extract_gateway_reference(gateway: str, payload: dict) -> str:
    # Extracts gateway reference from payload
    if gateway == "razorpay":
        return payload.get("payload", {}).get("payment", {}).get("entity", {}).get("id", "")
    elif gateway == "stripe":
        return payload.get("data", {}).get("object", {}).get("id", "")
    elif gateway == "hitpay":
        return payload.get("payment_id", "")
    return ""


def _extract_paid_amount(gateway: str, payload: dict) -> float:
    # Extracts paid amount from payload
    if gateway == "razorpay":
        amount = payload.get("payload", {}).get("payment", {}).get("entity", {}).get("amount", 0)
        return float(amount) / 100  # Razorpay sends in paise
    elif gateway == "stripe":
        amount = payload.get("data", {}).get("object", {}).get("amount", 0)
        return float(amount) / 100  # Stripe sends in cents
    elif gateway == "hitpay":
        return float(payload.get("amount", 0))
    return 0.0


def _reactivate_subscription_if_grace_period(invoice_name: str) -> bool:
    # Checks if subscription should be reactivated after payment
    subscription = frappe.db.get_value("Sales Invoice", invoice_name, "subscription")
    if not subscription:
        return False

    status = frappe.db.get_value("Subscription", subscription, "status")
    if status == "Past Due Date":
        logger.info(
            f"Subscription {subscription} may be reactivated after payment for {invoice_name}"
        )
        return True
    return False
