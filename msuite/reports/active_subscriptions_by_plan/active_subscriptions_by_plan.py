"""Active Subscriptions by Plan report."""
import frappe
from frappe.utils import flt, getdate, today


def execute(filters=None):
    columns = get_columns()
    data = get_data(filters)
    return columns, data


def get_columns():
    return [
        {"fieldname": "plan_name", "label": "Plan Name", "fieldtype": "Data", "width": 200},
        {"fieldname": "product", "label": "Product", "fieldtype": "Data", "width": 120},
        {"fieldname": "tier", "label": "Tier", "fieldtype": "Data", "width": 100},
        {"fieldname": "active_subscribers", "label": "Active Subscribers", "fieldtype": "Int", "width": 130},
        {"fieldname": "monthly_revenue", "label": "Monthly Revenue (₹)", "fieldtype": "Currency", "width": 150},
        {"fieldname": "in_trial_count", "label": "In Trial", "fieldtype": "Int", "width": 100},
        {"fieldname": "grace_period_count", "label": "Grace Period", "fieldtype": "Int", "width": 110},
    ]


def get_data(filters):
    plans = frappe.get_list(
        "MSuite Plan",
        filters={"is_active": 1},
        fields=["name", "plan_name", "product", "tier", "item"],
    )

    if filters.get("product"):
        plans = [p for p in plans if p.product == filters["product"]]
    if filters.get("tier"):
        plans = [p for p in plans if p.tier == filters["tier"]]

    result = []
    for plan in plans:
        sub_plan_name = frappe.db.get_value("Subscription Plan", {"item": plan.item}, "name")
        if not sub_plan_name:
            continue

        active_subs = frappe.get_list(
            "Subscription",
            filters={"status": "Active"},
            fields=["name", "trial_period_end"],
        )

        active_count = 0
        trial_count = 0
        grace_count = 0
        rate = frappe.db.get_value("Subscription Plan", sub_plan_name, "cost") or 0

        for sub in active_subs:
            sub_plans = frappe.get_list(
                "Subscription Plan Detail",
                filters={"parent": sub.name, "plan": sub_plan_name},
                fields=["name"],
            )
            if sub_plans:
                active_count += 1
                if sub.trial_period_end and getdate(today()) <= getdate(sub.trial_period_end):
                    trial_count += 1

        grace_subs = frappe.get_list(
            "Subscription",
            filters={"status": "Past Due Date"},
            fields=["name"],
        )
        for sub in grace_subs:
            sub_plans = frappe.get_list(
                "Subscription Plan Detail",
                filters={"parent": sub.name, "plan": sub_plan_name},
                fields=["name"],
            )
            if sub_plans:
                grace_count += 1

        result.append({
            "plan_name": plan.plan_name,
            "product": plan.product,
            "tier": plan.tier,
            "active_subscribers": active_count,
            "monthly_revenue": flt(active_count * float(rate)),
            "in_trial_count": trial_count,
            "grace_period_count": grace_count,
        })

    return result
