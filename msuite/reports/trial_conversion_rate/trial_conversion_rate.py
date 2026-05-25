"""Trial Conversion Rate report."""
import frappe
from frappe.utils import flt, date_diff


def execute(filters=None):
    columns = get_columns()
    data = get_data(filters)
    return columns, data


def get_columns():
    return [
        {"fieldname": "product", "label": "Product", "fieldtype": "Data", "width": 150},
        {"fieldname": "trials_started", "label": "Trials Started", "fieldtype": "Int", "width": 120},
        {"fieldname": "converted", "label": "Converted to Paid", "fieldtype": "Int", "width": 130},
        {"fieldname": "churned", "label": "Churned", "fieldtype": "Int", "width": 100},
        {"fieldname": "still_in_trial", "label": "Still in Trial", "fieldtype": "Int", "width": 110},
        {"fieldname": "conversion_rate", "label": "Conversion Rate (%)", "fieldtype": "Percent", "width": 130},
        {"fieldname": "avg_days", "label": "Avg Days to Convert", "fieldtype": "Float", "width": 140},
    ]


def get_data(filters):
    grant_filters = {"grant_type": "Trial"}
    if filters.get("from_date"):
        grant_filters["granted_on"] = [">=", filters["from_date"]]
    if filters.get("to_date"):
        grant_filters["granted_on"] = ["<=", filters["to_date"]]

    trial_grants = frappe.get_list(
        "MSuite Customer Grant",
        filters=grant_filters,
        fields=["name", "customer", "granted_plan", "status", "granted_on"],
    )

    product_stats = {}
    for grant in trial_grants:
        product = frappe.db.get_value("MSuite Plan", grant.granted_plan, "product")
        if filters.get("product") and product != filters["product"]:
            continue

        if product not in product_stats:
            product_stats[product] = {
                "trials_started": 0, "converted": 0, "churned": 0,
                "still_in_trial": 0, "conversion_days": [],
            }

        stats = product_stats[product]
        stats["trials_started"] += 1

        if grant.status == "Active":
            stats["still_in_trial"] += 1
        elif grant.status == "Revoked":
            paid_grant = frappe.db.get_value(
                "MSuite Customer Grant",
                {"customer": grant.customer, "grant_type": "Paid", "granted_plan": ["like", f"%{product}%"]},
                ["name", "granted_on"], as_dict=True,
            )
            if paid_grant:
                stats["converted"] += 1
                days = date_diff(paid_grant.granted_on, grant.granted_on)
                stats["conversion_days"].append(days)
            else:
                stats["churned"] += 1
        elif grant.status == "Expired":
            stats["churned"] += 1

    result = []
    for product, stats in product_stats.items():
        avg_days = 0
        if stats["conversion_days"]:
            avg_days = flt(sum(stats["conversion_days"]) / len(stats["conversion_days"]), 1)

        conversion_rate = 0
        if stats["trials_started"] > 0:
            conversion_rate = flt(stats["converted"] / stats["trials_started"] * 100, 1)

        result.append({
            "product": product,
            "trials_started": stats["trials_started"],
            "converted": stats["converted"],
            "churned": stats["churned"],
            "still_in_trial": stats["still_in_trial"],
            "conversion_rate": conversion_rate,
            "avg_days": avg_days,
        })

    return result
