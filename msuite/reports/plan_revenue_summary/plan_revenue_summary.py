"""Plan Revenue Summary report."""
import frappe
from frappe.utils import flt


def execute(filters=None):
    columns = get_columns()
    data = get_data(filters)
    return columns, data


def get_columns():
    return [
        {"fieldname": "plan_name", "label": "Plan Name", "fieldtype": "Data", "width": 200},
        {"fieldname": "product", "label": "Product", "fieldtype": "Data", "width": 120},
        {"fieldname": "tier", "label": "Tier", "fieldtype": "Data", "width": 100},
        {"fieldname": "invoice_count", "label": "Invoice Count", "fieldtype": "Int", "width": 100},
        {"fieldname": "total_revenue", "label": "Total Revenue (₹)", "fieldtype": "Currency", "width": 150},
        {"fieldname": "mrr", "label": "MRR (₹)", "fieldtype": "Currency", "width": 120},
        {"fieldname": "active_subscribers", "label": "Active Subscribers", "fieldtype": "Int", "width": 130},
        {"fieldname": "arpu", "label": "Avg Revenue Per User (₹)", "fieldtype": "Currency", "width": 150},
    ]


def get_data(filters):
    conditions = {"docstatus": 1}
    if filters.get("from_date"):
        conditions["posting_date"] = [">=", filters["from_date"]]
    if filters.get("to_date"):
        conditions["posting_date"] = ["<=", filters["to_date"]]

    invoices = frappe.get_list(
        "Sales Invoice",
        filters=conditions,
        fields=["name", "grand_total", "customer"],
    )

    plan_data = {}
    for inv in invoices:
        items = frappe.get_list(
            "Sales Invoice Item",
            filters={"parent": inv.name},
            fields=["item_code", "amount"],
        )
        for item in items:
            msuite_plan = frappe.db.get_value(
                "MSuite Plan", {"item": item.item_code, "is_active": 1},
                ["name", "plan_name", "product", "tier"], as_dict=True,
            )
            if not msuite_plan:
                continue

            key = msuite_plan.name
            if key not in plan_data:
                plan_data[key] = {
                    "plan_name": msuite_plan.plan_name,
                    "product": msuite_plan.product,
                    "tier": msuite_plan.tier,
                    "invoice_count": 0,
                    "total_revenue": 0,
                    "customers": set(),
                }
            plan_data[key]["invoice_count"] += 1
            plan_data[key]["total_revenue"] += flt(item.amount)
            plan_data[key]["customers"].add(inv.customer)

    result = []
    for key, data in plan_data.items():
        subscribers = len(data["customers"])
        result.append({
            "plan_name": data["plan_name"],
            "product": data["product"],
            "tier": data["tier"],
            "invoice_count": data["invoice_count"],
            "total_revenue": data["total_revenue"],
            "mrr": flt(data["total_revenue"] / max(data["invoice_count"], 1)),
            "active_subscribers": subscribers,
            "arpu": flt(data["total_revenue"] / max(subscribers, 1)),
        })

    return result
