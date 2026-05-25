"""
Plan sync — push updated plan data to all active clients on a plan.

Finds clients through:
  Plan → Subscription Plan names → Subscription Plan Details →
  Subscriptions (Active/Trialing) → Customers → MSuite Clients (Active)

Also checks grants for trial/complimentary plans.
"""

import frappe
from frappe.utils import now

from msuite.constants import SyncStatus


@frappe.whitelist()
def push_to_all_clients(plan_name: str) -> dict:
    """
    Push updated plan data to all active clients on this plan.

    Args:
        plan_name: MSuite Plan document name

    Returns:
        {"pushed": N, "failed": N, "clients": [...], "message": "..."}
    """
    doc = frappe.get_doc("MSuite Plan", plan_name)
    if not doc.is_active:
        frappe.throw("Cannot push: plan is not active.", frappe.ValidationError)

    customers = _find_customers_on_plan(doc)

    if not customers:
        return _result(0, 0, [], "No active clients found on this plan.")

    # Find active MSuite Clients for these customers
    clients = frappe.get_all(
        "MSuite Client",
        filters={"customer": ["in", list(customers)], "status": "Active"},
        fields=["name", "customer"],
    )

    if not clients:
        return _result(0, 0, [], "No active clients found on this plan.")

    # Push plan to each client
    from msuite.services.client_service import build_client_plan_data, push_plan_to_client

    pushed, failed, results = 0, 0, []

    for client in clients:
        try:
            client_doc = frappe.get_doc("MSuite Client", client.name)
            plan_data = build_client_plan_data(client.customer)
            push_plan_to_client(client_doc, plan_data)

            client_doc.last_sync = now()
            client_doc.last_sync_status = SyncStatus.SUCCESS
            client_doc.sync_fail_count = 0
            client_doc.save(ignore_permissions=True)

            pushed += 1
            results.append({"client": client.name, "status": "success"})
        except Exception as e:
            failed += 1
            results.append({"client": client.name, "status": "failed", "error": str(e)})

    frappe.db.commit()
    msg = f"Pushed to {pushed} client(s). {failed} failed." if failed else f"Pushed to {pushed} client(s)."
    return _result(pushed, failed, results, msg)


def _find_customers_on_plan(doc) -> set:
    """Find all customers with active subscriptions or grants for this plan."""
    customers = set()

    # Via subscriptions: plan → subscription plan names → subscriptions → customers
    sub_plan_names = [r.subscription_plan for r in (doc.pricing or []) if r.subscription_plan]
    if sub_plan_names:
        subs = frappe.get_all(
            "Subscription Plan Detail",
            filters={"plan": ["in", sub_plan_names]},
            fields=["parent"],
        )
        for s in subs:
            sub_doc = frappe.db.get_value(
                "Subscription", s.parent, ["party", "status"], as_dict=True
            )
            if sub_doc and sub_doc.status in ("Active", "Trialing"):
                customers.add(sub_doc.party)

    # Via grants: trial/complimentary plans may not have subscriptions
    # granted_plan = the plan given to customer; trigger_plan = the plan that caused it
    grants = frappe.get_all(
        "MSuite Customer Grant",
        filters={"granted_plan": doc.name, "status": "Active"},
        fields=["customer"],
    )
    for g in grants:
        customers.add(g.customer)

    return customers


def _result(pushed, failed, clients, message):
    """Build a standard result dict."""
    return {"pushed": pushed, "failed": failed, "clients": clients, "message": message}
