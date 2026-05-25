"""Coupon API endpoints."""
import frappe

from msuite.constants import ErrorCode
from msuite.utils.validators import (
    success_response,
    error_response,
    require_permission,
)


@frappe.whitelist()
def validate_coupon(coupon_code: str, customer: str) -> dict:
    """Validates a coupon for a customer before applying to invoice."""
    try:
        require_permission("Customer", customer, "read")
        from msuite.services.coupon_service import validate_coupon_for_customer
        validate_coupon_for_customer(coupon_code, customer)

        coupon_doc = frappe.get_doc("Coupon Code", coupon_code)
        discount_pct = None
        discount_amt = None
        pricing_rule = coupon_doc.pricing_rule

        if pricing_rule:
            pr_data = frappe.db.get_value(
                "Pricing Rule",
                pricing_rule,
                ["discount_percentage", "discount_amount"],
                as_dict=True,
            )
            if pr_data:
                discount_pct = float(pr_data.discount_percentage) if pr_data.discount_percentage else None
                discount_amt = float(pr_data.discount_amount) if pr_data.discount_amount else None

        return success_response({
            "valid": True,
            "coupon_code": coupon_code,
            "discount_percentage": discount_pct,
            "discount_amount": discount_amt,
            "pricing_rule": pricing_rule,
        })
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.COUPON_INVALID, str(e))


@frappe.whitelist()
def get_applicable_coupons(customer: str) -> dict:
    """Returns all valid unused coupons for a customer."""
    try:
        require_permission("Customer", customer, "read")
        from msuite.services.coupon_service import get_applicable_coupons as svc_get
        data = svc_get(customer)
        return success_response(data)
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.CUSTOMER_NOT_FOUND, str(e))


@frappe.whitelist()
def get_usage_history(customer: str) -> dict:
    """Returns coupon usage history for a customer."""
    try:
        require_permission("Customer", customer, "read")
        from msuite.services.coupon_service import get_coupon_usage_for_customer
        data = get_coupon_usage_for_customer(customer)
        return success_response(data)
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.CUSTOMER_NOT_FOUND, str(e))
