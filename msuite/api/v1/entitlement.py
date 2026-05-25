"""Entitlement API endpoints."""
import frappe

from msuite.constants import ErrorCode
from msuite.utils.validators import (
    success_response,
    error_response,
    require_permission,
    require_system_manager_or_msuite_manager,
)


@frappe.whitelist()
def get_entitlements(customer: str) -> dict:
    """Returns full entitlement map for a customer."""
    try:
        require_permission("Customer", customer, "read")
        from msuite.services.entitlement_service import get_customer_entitlements
        data = get_customer_entitlements(customer)
        return success_response(data)
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.CUSTOMER_NOT_FOUND, str(e))


@frappe.whitelist()
def check_feature(customer: str, feature_key: str) -> dict:
    """Returns access status for a single feature."""
    try:
        require_permission("Customer", customer, "read")
        from msuite.services.entitlement_service import get_customer_entitlements
        entitlements = get_customer_entitlements(customer)
        feature = entitlements.get("features", {}).get(feature_key)
        if not feature:
            return success_response({"enabled": False, "limit": None, "limit_label": None})
        return success_response({
            "enabled": feature.get("enabled", False),
            "limit": feature.get("limit"),
            "limit_label": feature.get("limit_label"),
        })
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.CUSTOMER_NOT_FOUND, str(e))


@frappe.whitelist()
def get_active_plans(customer: str) -> dict:
    """Returns list of active plans and grants for a customer."""
    try:
        require_permission("Customer", customer, "read")
        from msuite.services.grant_service import get_active_grants_for_customer
        grants = get_active_grants_for_customer(customer)
        return success_response(grants)
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.CUSTOMER_NOT_FOUND, str(e))


@frappe.whitelist()
def invalidate_cache(customer: str) -> dict:
    """Manually invalidates entitlement cache for a customer."""
    try:
        require_system_manager_or_msuite_manager()
        from msuite.utils.cache import invalidate_entitlement_cache
        invalidate_entitlement_cache(customer)
        return success_response({"message": f"Cache invalidated for {customer}"})
    except frappe.PermissionError:
        return error_response(ErrorCode.PERMISSION_DENIED, "Permission denied")
    except Exception as e:
        return error_response(ErrorCode.INVALID_INPUT, str(e))
