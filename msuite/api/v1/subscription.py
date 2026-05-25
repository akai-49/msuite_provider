"""Subscription API endpoints."""
import json

import frappe

from msuite.constants import ErrorCode
from msuite.utils.validators import (
    success_response,
    error_response,
    require_permission,
    require_system_manager_or_msuite_manager,
    validate_date_string,
)


@frappe.whitelist()
def create_subscription(
    customer: str,
    subscription_plan_names: str,
    start_date: str,
    trial_days: int = 0,
    generate_invoice_at: str = "Beginning of the current subscription period",
    days_until_due: int = 7,
    additional_discount_percentage: float = 0.0,
    additional_discount_amount: float = 0.0,
) -> dict:
    """Creates a new Subscription."""
    try:
        require_system_manager_or_msuite_manager()
        validate_date_string(start_date, "start_date")

        plan_names = json.loads(subscription_plan_names)
        if not isinstance(plan_names, list):
            return error_response(ErrorCode.INVALID_INPUT, "subscription_plan_names must be a JSON array")

        from msuite.services.subscription_service import create_subscription as svc_create
        name = svc_create(
            customer=customer,
            subscription_plan_names=plan_names,
            start_date=start_date,
            trial_days=int(trial_days),
            generate_invoice_at=generate_invoice_at,
            days_until_due=int(days_until_due),
            additional_discount_percentage=float(additional_discount_percentage),
            additional_discount_amount=float(additional_discount_amount),
        )
        return success_response({"subscription": name})
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except json.JSONDecodeError:
        return error_response(ErrorCode.INVALID_INPUT, "Invalid JSON for subscription_plan_names")
    except Exception as e:
        return error_response(ErrorCode.INVALID_INPUT, str(e))


@frappe.whitelist()
def cancel_subscription(subscription_name: str, reason: str = "") -> dict:
    """Cancels a Subscription."""
    try:
        require_system_manager_or_msuite_manager()
        from msuite.services.subscription_service import cancel_subscription as svc_cancel
        svc_cancel(subscription_name, reason)
        return success_response({"message": f"Subscription {subscription_name} cancelled"})
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.SUBSCRIPTION_NOT_FOUND, str(e))


@frappe.whitelist()
def amend_subscription(
    subscription_name: str,
    new_subscription_plan_names: str,
    effective_date: str,
    additional_discount_percentage: float = 0.0,
) -> dict:
    """Upgrades or downgrades a Subscription."""
    try:
        require_system_manager_or_msuite_manager()
        validate_date_string(effective_date, "effective_date")

        plan_names = json.loads(new_subscription_plan_names)

        from msuite.services.subscription_service import amend_subscription as svc_amend
        new_name = svc_amend(
            subscription_name, plan_names, effective_date,
            float(additional_discount_percentage),
        )
        return success_response({"subscription": new_name})
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.AMENDMENT_FAILED, str(e))


@frappe.whitelist()
def get_customer_subscriptions(customer: str) -> dict:
    """Returns all subscriptions for a customer."""
    try:
        require_permission("Customer", customer, "read")
        from msuite.services.subscription_service import get_customer_subscriptions as svc_get
        data = svc_get(customer)
        return success_response(data)
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.CUSTOMER_NOT_FOUND, str(e))


@frappe.whitelist()
def get_billing_summary(subscription_name: str) -> dict:
    """Returns full billing summary for a subscription."""
    try:
        require_permission("Subscription", subscription_name, "read")
        from msuite.services.subscription_service import get_subscription_billing_summary
        data = get_subscription_billing_summary(subscription_name)
        return success_response(data)
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.SUBSCRIPTION_NOT_FOUND, str(e))
