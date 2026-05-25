"""
Coupon validation and usage tracking service.

ERPNext native Coupon Code handles total usage tracking and expiry.
MSuite Coupon Usage adds per-customer one-use enforcement.
"""
import frappe
from frappe.utils import getdate, today

from msuite.constants import MSUITE_LOGGER_NAME
from msuite.exceptions import CouponValidationError, CouponAlreadyUsedError

logger = frappe.logger(MSUITE_LOGGER_NAME)


def validate_coupon_for_customer(coupon_code: str, customer: str) -> bool:
    """
    Validates a coupon before allowing it on an invoice.

    Args:
        coupon_code: ERPNext Coupon Code name
        customer: ERPNext Customer name

    Returns:
        True if valid

    Raises:
        CouponValidationError: coupon not found, expired, or max use exceeded
        CouponAlreadyUsedError: customer already used this coupon
    """
    if not frappe.db.exists("Coupon Code", coupon_code):
        frappe.throw(f"Coupon {coupon_code} not found", CouponValidationError)

    coupon_doc = frappe.get_doc("Coupon Code", coupon_code)

    # Check dates
    if coupon_doc.valid_from and getdate(today()) < getdate(coupon_doc.valid_from):
        frappe.throw("Coupon is not yet valid", CouponValidationError)

    if coupon_doc.valid_upto and getdate(today()) > getdate(coupon_doc.valid_upto):
        frappe.throw("Coupon has expired", CouponValidationError)

    # Check total usage
    if coupon_doc.maximum_use and coupon_doc.maximum_use > 0:
        if coupon_doc.used >= coupon_doc.maximum_use:
            frappe.throw("Coupon maximum uses exceeded", CouponValidationError)

    # Check per-customer usage
    if frappe.db.exists(
        "MSuite Coupon Usage",
        {"coupon_code": coupon_code, "customer": customer},
    ):
        frappe.throw(
            f"Customer {customer} has already used coupon {coupon_code}",
            CouponAlreadyUsedError,
        )

    return True


def record_coupon_usage(
    coupon_code: str,
    customer: str,
    invoice_name: str,
) -> None:
    """
    Records that a customer used a coupon. Idempotent.

    Args:
        coupon_code: ERPNext Coupon Code name
        customer: ERPNext Customer name
        invoice_name: Sales Invoice name
    """
    if frappe.db.exists(
        "MSuite Coupon Usage",
        {"coupon_code": coupon_code, "customer": customer},
    ):
        logger.warning(
            f"Coupon usage already recorded for {customer}/{coupon_code}, skipping"
        )
        return

    doc = frappe.new_doc("MSuite Coupon Usage")
    doc.coupon_code = coupon_code
    doc.customer = customer
    doc.used_on = today()
    doc.sales_invoice = invoice_name
    doc.insert(ignore_permissions=True)

    logger.info(f"Recorded coupon usage: {customer} used {coupon_code} on invoice {invoice_name}")


def get_coupon_usage_for_customer(
    customer: str,
    coupon_code: str | None = None,
) -> list[dict]:
    """
    Returns coupon usage history for a customer.

    Returns:
        List of usage dicts
    """
    filters: dict = {"customer": customer}
    if coupon_code:
        filters["coupon_code"] = coupon_code

    usages = frappe.get_list(
        "MSuite Coupon Usage",
        filters=filters,
        fields=["coupon_code", "used_on", "sales_invoice"],
        order_by="used_on desc",
    )

    # Batch-load coupon → pricing_rule mappings
    coupon_names = list({u.coupon_code for u in usages})
    coupon_pr_map: dict[str, str] = {}
    pr_discount_map: dict[str, float] = {}
    if coupon_names:
        for cc in frappe.get_list("Coupon Code", filters={"name": ["in", coupon_names]}, fields=["name", "pricing_rule"]):
            if cc.pricing_rule:
                coupon_pr_map[cc.name] = cc.pricing_rule
        pr_names = list(set(coupon_pr_map.values()))
        if pr_names:
            for pr in frappe.get_list("Pricing Rule", filters={"name": ["in", pr_names]}, fields=["name", "discount_percentage"]):
                if pr.discount_percentage:
                    pr_discount_map[pr.name] = float(pr.discount_percentage)

    result: list[dict] = []
    for usage in usages:
        discount_info = ""
        pr_name = coupon_pr_map.get(usage.coupon_code)
        if pr_name and pr_name in pr_discount_map:
            discount_info = f"{pr_discount_map[pr_name]}% off"

        result.append({
            "coupon_code": usage.coupon_code,
            "used_on": str(usage.used_on),
            "sales_invoice": usage.sales_invoice,
            "discount_applied": discount_info,
        })

    return result


def get_applicable_coupons(customer: str) -> list[dict]:
    """
    Returns all valid, unused coupons that a customer can apply.

    Returns:
        List of coupon dicts
    """
    all_coupons = frappe.get_list(
        "Coupon Code",
        filters=[
            ["valid_upto", ">=", today()],
        ],
        fields=["name", "valid_upto", "maximum_use", "used", "pricing_rule"],
        or_filters=[
            ["valid_upto", "is", "not set"],
            ["valid_upto", ">=", today()],
        ],
    )

    used_coupons = set(
        row.coupon_code
        for row in frappe.get_list(
            "MSuite Coupon Usage",
            filters={"customer": customer},
            fields=["coupon_code"],
        )
    )

    # Batch-load all pricing rules referenced by coupons
    pr_names = list({c.pricing_rule for c in all_coupons if c.pricing_rule})
    pr_data_map: dict[str, dict] = {}
    if pr_names:
        for pr in frappe.get_list("Pricing Rule", filters={"name": ["in", pr_names]}, fields=["name", "discount_percentage", "discount_amount"]):
            pr_data_map[pr.name] = pr

    result: list[dict] = []
    for coupon in all_coupons:
        if coupon.name in used_coupons:
            continue
        if coupon.maximum_use and coupon.maximum_use > 0 and coupon.used >= coupon.maximum_use:
            continue

        discount_pct = None
        discount_amt = None
        pr_data = pr_data_map.get(coupon.pricing_rule)
        if pr_data:
            discount_pct = float(pr_data.discount_percentage) if pr_data.discount_percentage else None
            discount_amt = float(pr_data.discount_amount) if pr_data.discount_amount else None

        remaining = None
        if coupon.maximum_use and coupon.maximum_use > 0:
            remaining = coupon.maximum_use - (coupon.used or 0)

        result.append({
            "coupon_code": coupon.name,
            "description": "",
            "discount_percentage": discount_pct,
            "discount_amount": discount_amt,
            "valid_upto": str(coupon.valid_upto) if coupon.valid_upto else None,
            "remaining_uses": remaining,
        })

    return result
