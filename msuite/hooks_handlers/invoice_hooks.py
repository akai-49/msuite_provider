"""
Sales Invoice event hooks.
Handles coupon usage recording on invoice submission.
"""
import frappe

from msuite.constants import MSUITE_LOGGER_NAME

logger = frappe.logger(MSUITE_LOGGER_NAME)


def on_invoice_submit(doc, method):
    """Fires on Sales Invoice on_submit."""
    try:
        if not doc.coupon_code:
            return

        from msuite.services.coupon_service import record_coupon_usage

        record_coupon_usage(doc.coupon_code, doc.customer, doc.name)
        logger.info(f"Recorded coupon {doc.coupon_code} usage for invoice {doc.name}")

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"MSuite: on_invoice_submit coupon recording error for {doc.name}",
        )
