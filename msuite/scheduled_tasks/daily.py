"""Daily scheduled tasks for MSuite."""
import frappe

from msuite.constants import MSUITE_LOGGER_NAME

logger = frappe.logger(MSUITE_LOGGER_NAME)


def expire_trial_grants():
    """Expires stale trial grants and triggers paid grant application."""
    try:
        from msuite.services.grant_service import expire_stale_trial_grants

        result = expire_stale_trial_grants()
        logger.info(f"Daily trial grant expiry: {len(result)} grants processed")

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "MSuite: Daily expire_trial_grants failed",
        )


def reconcile_customer_groups():
    """Reconciles Customer Group memberships for recently changed grants."""
    try:
        from msuite.services.customer_group_service import reconcile_all_groups

        summary = reconcile_all_groups()
        logger.info(
            f"Daily group reconciliation: {summary['customers_checked']} customers, "
            f"{summary['additions']} additions, {summary['removals']} removals, "
            f"{len(summary['errors'])} errors"
        )

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "MSuite: Daily reconcile_customer_groups failed",
        )


def sync_active_clients():
    """Pushes fresh plan data to all active client instances."""
    try:
        from msuite.services.client_service import sync_all_active_clients

        summary = sync_all_active_clients()
        logger.info(
            f"Daily client sync: {summary['synced']} synced, "
            f"{summary['failed']} failed, {summary['disconnected']} disconnected"
        )

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "MSuite: Daily sync_active_clients failed",
        )


def refresh_expiring_tokens():
    """Refresh tokens for connected accounts approaching expiry.

    Uses the comprehensive refresh module which also pushes updated
    tokens to clients and sends admin notifications for non-refreshable
    tokens (Meta user tokens).
    """
    try:
        from msuite.services.oauth.refresh import refresh_all_tokens

        summary = refresh_all_tokens()
        logger.info(
            f"Token refresh: {summary['refreshed']} refreshed, "
            f"{summary['notified']} notified, {summary['failed']} failed"
        )

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "MSuite: Daily refresh_expiring_tokens failed",
        )


def notify_expiring_subscriptions():
    """Scan and notify customers and provider admins about expiring subscriptions."""
    try:
        from msuite.services.subscription_notification_service import (
            check_and_notify_expiring_subscriptions,
        )

        summary = check_and_notify_expiring_subscriptions()
        logger.info(
            f"Subscription expiry notify: {summary['checked']} checked, "
            f"{summary['client_notified']} clients notified, {summary['admin_notified']} admins notified"
        )

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "MSuite: Daily notify_expiring_subscriptions failed",
        )

