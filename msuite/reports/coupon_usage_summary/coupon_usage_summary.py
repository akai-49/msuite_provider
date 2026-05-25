"""Coupon Usage Summary report."""
import frappe
from frappe.utils import flt, getdate, today


def execute(filters=None):
    columns = get_columns()
    data = get_data(filters)
    return columns, data


def get_columns():
    return [
        {"fieldname": "coupon_code", "label": "Coupon Code", "fieldtype": "Link", "options": "Coupon Code", "width": 150},
        {"fieldname": "pricing_rule", "label": "Pricing Rule", "fieldtype": "Data", "width": 200},
        {"fieldname": "total_uses", "label": "Total Uses", "fieldtype": "Int", "width": 100},
        {"fieldname": "max_uses", "label": "Max Uses", "fieldtype": "Int", "width": 100},
        {"fieldname": "remaining_uses", "label": "Remaining", "fieldtype": "Int", "width": 100},
        {"fieldname": "total_discount", "label": "Total Discount (₹)", "fieldtype": "Currency", "width": 140},
        {"fieldname": "valid_upto", "label": "Valid Upto", "fieldtype": "Date", "width": 120},
        {"fieldname": "status", "label": "Status", "fieldtype": "Data", "width": 100},
    ]


def get_data(filters):
    coupon_filters = {}
    if filters.get("coupon_code"):
        coupon_filters["name"] = filters["coupon_code"]

    coupons = frappe.get_list(
        "Coupon Code",
        filters=coupon_filters,
        fields=["name", "pricing_rule", "used", "maximum_use", "valid_upto"],
    )

    result = []
    for coupon in coupons:
        usage_filters = {"coupon_code": coupon.name}
        if filters.get("from_date"):
            usage_filters["used_on"] = [">=", filters["from_date"]]
        if filters.get("to_date"):
            usage_filters["used_on"] = ["<=", filters["to_date"]]

        usage_count = frappe.db.count("MSuite Coupon Usage", usage_filters)

        remaining = None
        if coupon.maximum_use and coupon.maximum_use > 0:
            remaining = coupon.maximum_use - (coupon.used or 0)

        status = "Active"
        if coupon.valid_upto and getdate(coupon.valid_upto) < getdate(today()):
            status = "Expired"
        elif coupon.maximum_use and coupon.maximum_use > 0 and (coupon.used or 0) >= coupon.maximum_use:
            status = "Exhausted"

        result.append({
            "coupon_code": coupon.name,
            "pricing_rule": coupon.pricing_rule,
            "total_uses": coupon.used or 0,
            "max_uses": coupon.maximum_use or 0,
            "remaining_uses": remaining,
            "total_discount": 0,
            "valid_upto": coupon.valid_upto,
            "status": status,
        })

    return result
