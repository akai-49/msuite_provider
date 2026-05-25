"""Subscription management service."""
import frappe
from frappe.utils import add_days

from msuite.constants import MSUITE_LOGGER_NAME
from msuite.exceptions import SubscriptionAmendmentError

logger = frappe.logger(MSUITE_LOGGER_NAME)


def create_subscription(
    customer: str,
    subscription_plan_names: list[str],
    start_date: str,
    trial_days: int = 0,
    generate_invoice_at: str = "Beginning of the current subscription period",
    days_until_due: int = 7,
    additional_discount_percentage: float = 0.0,
    additional_discount_amount: float = 0.0,
) -> str:
    """
    Creates and submits an ERPNext Subscription.

    Args:
        customer: ERPNext Customer name
        subscription_plan_names: List of ERPNext Subscription Plan names
        start_date: ISO date string
        trial_days: 0 = no trial
        generate_invoice_at: Billing timing
        days_until_due: Payment due days
        additional_discount_percentage: Enterprise discount %
        additional_discount_amount: Enterprise discount amount

    Returns:
        Created Subscription document name

    Raises:
        frappe.DoesNotExistError: customer or plan not found
    """
    if not frappe.db.exists("Customer", customer):
        frappe.throw(f"Customer {customer} not found", frappe.DoesNotExistError)

    for plan_name in subscription_plan_names:
        if not frappe.db.exists("Subscription Plan", plan_name):
            frappe.throw(f"Subscription Plan {plan_name} not found", frappe.DoesNotExistError)

    doc = frappe.new_doc("Subscription")
    doc.party_type = "Customer"
    doc.party = customer
    doc.start_date = start_date
    doc.generate_invoice_at = generate_invoice_at
    doc.days_until_due = days_until_due

    if additional_discount_percentage > 0:
        doc.additional_discount_percentage = additional_discount_percentage
    if additional_discount_amount > 0:
        doc.additional_discount_amount = additional_discount_amount

    for plan_name in subscription_plan_names:
        doc.append("plans", {"plan": plan_name, "qty": 1})

    if trial_days > 0:
        doc.trial_period_start = start_date
        doc.trial_period_end = add_days(start_date, trial_days)

    doc.insert(ignore_permissions=True)
    doc.submit()

    logger.info(f"Created subscription {doc.name} for customer {customer}")
    return doc.name


def cancel_subscription(subscription_name: str, reason: str = "") -> None:
    """
    Cancels an ERPNext Subscription.

    Args:
        subscription_name: Subscription document name
        reason: Cancellation reason

    Raises:
        frappe.DoesNotExistError: subscription not found
    """
    doc = frappe.get_doc("Subscription", subscription_name)
    if doc.status != "Active":
        frappe.throw(
            f"Cannot cancel subscription {subscription_name}: status is {doc.status}",
            frappe.ValidationError,
        )
    doc.cancel()
    if reason:
        logger.info(f"Cancelled subscription {subscription_name}: {reason}")
    else:
        logger.info(f"Cancelled subscription {subscription_name}")


def amend_subscription(
    subscription_name: str,
    new_subscription_plan_names: list[str],
    effective_date: str,
    additional_discount_percentage: float = 0.0,
) -> str:
    """
    Handles plan upgrade or downgrade.

    Args:
        subscription_name: Original Subscription name
        new_subscription_plan_names: New plan list
        effective_date: When new subscription starts
        additional_discount_percentage: Updated enterprise discount

    Returns:
        New Subscription document name

    Raises:
        SubscriptionAmendmentError: if process fails
    """
    try:
        old_doc = frappe.get_doc("Subscription", subscription_name)
        customer = old_doc.party

        old_doc.cancel()

        new_name = create_subscription(
            customer=customer,
            subscription_plan_names=new_subscription_plan_names,
            start_date=effective_date,
            additional_discount_percentage=additional_discount_percentage,
        )

        from msuite.services.grant_service import apply_grants_after_amendment
        apply_grants_after_amendment(subscription_name, new_name)

        logger.info(f"Amended subscription {subscription_name} -> {new_name}")
        return new_name

    except Exception as e:
        frappe.throw(
            f"Failed to amend subscription {subscription_name}: {str(e)}",
            SubscriptionAmendmentError,
        )


def get_customer_subscriptions(customer: str) -> list[dict]:
    """
    Returns all subscriptions for a customer with status and plan details.

    Returns:
        List of subscription dicts
    """
    from msuite.services.trial_service import is_subscription_in_trial

    subscriptions = frappe.get_list(
        "Subscription",
        filters={"party_type": "Customer", "party": customer},
        fields=["name", "status", "start_date", "trial_period_end",
                "additional_discount_percentage"],
        order_by="creation desc",
    )

    # Batch-load all Subscription Plan → Item mappings
    all_sp_names = set()
    sub_docs = {}
    for sub in subscriptions:
        sub_doc = frappe.get_doc("Subscription", sub.name)
        sub_docs[sub.name] = sub_doc
        for row in sub_doc.plans:
            all_sp_names.add(row.plan)

    sp_item_map: dict[str, str] = {}
    if all_sp_names:
        for sp in frappe.get_list("Subscription Plan", filters={"name": ["in", list(all_sp_names)]}, fields=["name", "item"]):
            sp_item_map[sp.name] = sp.item

    result: list[dict] = []
    for sub in subscriptions:
        sub_doc = sub_docs[sub.name]
        plans = [
            {"plan": row.plan, "item": sp_item_map.get(row.plan)}
            for row in sub_doc.plans
        ]

        in_trial = is_subscription_in_trial(sub.name) if sub.status == "Active" else False

        result.append({
            "subscription": sub.name,
            "status": sub.status,
            "start_date": str(sub.start_date) if sub.start_date else None,
            "plans": plans,
            "trial_ends": str(sub.trial_period_end) if sub.trial_period_end else None,
            "next_invoice_date": None,
            "additional_discount_percentage": float(sub.additional_discount_percentage or 0),
            "is_trial": in_trial,
        })

    return result


def get_subscription_billing_summary(subscription_name: str) -> dict:
    """
    Returns full billing summary for a subscription.

    Returns:
        Billing summary dict
    """
    sub_doc = frappe.get_doc("Subscription", subscription_name)

    invoices = frappe.get_list(
        "Sales Invoice",
        filters={"subscription": subscription_name, "docstatus": ["!=", 2]},
        fields=["name", "posting_date", "grand_total", "status", "coupon_code", "outstanding_amount"],
        order_by="posting_date desc",
    )

    outstanding_invoices = [i for i in invoices if i.status in ("Unpaid", "Overdue")]
    total_outstanding = sum(float(i.outstanding_amount or 0) for i in outstanding_invoices)

    last_payment = frappe.get_list(
        "Payment Entry",
        filters={"party_type": "Customer", "party": sub_doc.party, "docstatus": 1},
        fields=["posting_date", "paid_amount"],
        order_by="posting_date desc",
        limit_page_length=1,
    )

    return {
        "subscription": subscription_name,
        "customer": sub_doc.party,
        "status": sub_doc.status,
        "start_date": str(sub_doc.start_date) if sub_doc.start_date else None,
        "next_invoice_date": None,
        "trial_ends": str(sub_doc.trial_period_end) if sub_doc.trial_period_end else None,
        "outstanding_invoices": len(outstanding_invoices),
        "total_outstanding": float(total_outstanding),
        "last_payment_date": str(last_payment[0].posting_date) if last_payment else None,
        "last_payment_amount": float(last_payment[0].paid_amount) if last_payment else None,
        "additional_discount_percentage": float(sub_doc.additional_discount_percentage or 0),
        "invoices": [
            {
                "invoice": i.name,
                "date": str(i.posting_date),
                "total": float(i.grand_total),
                "status": i.status,
                "coupon_used": i.coupon_code,
            }
            for i in invoices
        ],
    }
