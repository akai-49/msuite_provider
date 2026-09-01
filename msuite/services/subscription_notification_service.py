"""
Subscription Expiry Notification Service.

Monitors active, trialing, and grace-period subscriptions daily and sends
proactive email notifications to both the Customer and Provider Administrators
when a subscription is approaching expiry / billing cycle renewal.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import date_diff, get_datetime, getdate, now_datetime, nowdate

from msuite.constants import MSUITE_LOGGER_NAME

logger = frappe.logger(MSUITE_LOGGER_NAME)

# Trigger milestones in days before expiry
NOTIFICATION_MILESTONES = [7, 3, 1, 0]


def check_and_notify_expiring_subscriptions() -> dict:
	"""Daily cron task: inspect subscriptions and dispatch expiry emails.

	Returns:
	    dict with count of customers notified, admins notified, and errors.
	"""
	summary = {
		"checked": 0,
		"client_notified": 0,
		"admin_notified": 0,
		"skipped": 0,
		"errors": 0,
	}

	subscriptions = frappe.get_all(
		"Subscription",
		filters={
			"party_type": "Customer",
			"status": ["in", ["Active", "Trialing", "Grace Period", "Past Due Date"]],
		},
		fields=[
			"name",
			"party",
			"status",
			"start_date",
			"end_date",
			"current_invoice_start",
			"current_invoice_end",
			"trial_period_start",
			"trial_period_end",
			"cancel_at_period_end",
		],
	)

	today = getdate(nowdate())

	for sub in subscriptions:
		summary["checked"] += 1
		try:
			expiry_info = _get_subscription_expiry_info(sub, today)
			if not expiry_info:
				summary["skipped"] += 1
				continue

			days_left = expiry_info["days_left"]
			milestone = expiry_info["milestone"]

			if milestone is None:
				summary["skipped"] += 1
				continue

			# Deduplication via Redis cache
			cache_key = f"msuite:sub_expiry_notified:{sub.name}:{milestone}:{expiry_info['target_date']}"
			if frappe.cache.get_value(cache_key):
				summary["skipped"] += 1
				continue

			notified_client, notified_admin = _send_expiry_notifications(
				sub=sub,
				expiry_info=expiry_info,
			)

			if notified_client:
				summary["client_notified"] += 1
			if notified_admin:
				summary["admin_notified"] += 1

			# Cache for 30 days so this milestone for this period target date is never re-sent
			frappe.cache.set_value(cache_key, "1", expires_in_sec=86400 * 30)

		except Exception:
			summary["errors"] += 1
			frappe.log_error(
				frappe.get_traceback(),
				f"MSuite: Failed notifying for subscription {sub.name}",
			)

	logger.info(
		f"Subscription Expiry Notifications: {summary['checked']} checked, "
		f"{summary['client_notified']} clients notified, {summary['admin_notified']} admins notified, "
		f"{summary['errors']} errors"
	)
	return summary


def _get_subscription_expiry_info(sub: dict, today: any) -> dict | None:
	"""Determine target expiry date, days remaining, and matched milestone."""
	target_date = None
	expiry_type = "billing_period"

	# 1. Trial ending
	if sub.get("status") == "Trialing" or (
		sub.get("trial_period_end") and getdate(sub.get("trial_period_end")) >= today
	):
		target_date = getdate(sub.get("trial_period_end"))
		expiry_type = "trial"

	# 2. Fixed end date
	elif sub.get("end_date"):
		target_date = getdate(sub.get("end_date"))
		expiry_type = "fixed_end_date"

	# 3. Recurring billing period end
	elif sub.get("current_invoice_end"):
		target_date = getdate(sub.get("current_invoice_end"))
		expiry_type = "billing_period"

	# 4. Grace period
	elif sub.get("status") in ("Grace Period", "Past Due Date"):
		target_date = today
		expiry_type = "grace_period"

	if not target_date:
		return None

	days_left = date_diff(target_date, today)

	# Match milestone
	matched_milestone = None
	if sub.get("status") in ("Grace Period", "Past Due Date"):
		matched_milestone = "grace_period"
	elif days_left in NOTIFICATION_MILESTONES:
		matched_milestone = f"day_{days_left}"

	return {
		"target_date": str(target_date),
		"days_left": days_left,
		"milestone": matched_milestone,
		"expiry_type": expiry_type,
	}


def _get_customer_emails(customer_name: str) -> list[str]:
	"""Find email addresses for a Customer."""
	emails = set()

	# 1. Customer Doc email_id
	direct_email = frappe.db.get_value("Customer", customer_name, "email_id")
	if direct_email and direct_email.strip():
		emails.add(direct_email.strip())

	# 2. Linked Contact Doc
	contacts = frappe.db.sql(
		"""
        SELECT c.email_id
        FROM `tabContact` c
        JOIN `tabDynamic Link` dl ON dl.parent = c.name
        WHERE dl.link_doctype = 'Customer'
          AND dl.link_name = %s
          AND c.email_id IS NOT NULL AND c.email_id != ''
    """,
		(customer_name,),
		as_dict=True,
	)
	for row in contacts:
		if row.email_id and row.email_id.strip():
			emails.add(row.email_id.strip())

	return list(emails)


def _get_admin_emails() -> list[str]:
	"""Get System Managers for provider admin alerts."""
	admins = frappe.get_all(
		"Has Role",
		filters={"role": "System Manager", "parenttype": "User"},
		pluck="parent",
		limit=5,
	)
	# Exclude inactive or bot users
	valid_admins = []
	for admin in admins:
		if admin not in ("Administrator", "guest@example.com") and frappe.db.get_value("User", admin, "enabled"):
			valid_admins.append(admin)
	if not valid_admins and frappe.db.get_value("User", "Administrator", "email"):
		valid_admins.append(frappe.db.get_value("User", "Administrator", "email"))
	return valid_admins or ["admin@example.com"]


def _get_subscription_plans_summary(subscription_name: str) -> list[str]:
	"""Fetch plan names for a subscription."""
	plans = frappe.get_all(
		"Subscription Plan Detail",
		filters={"parent": subscription_name},
		pluck="plan",
	)
	return plans or ["MSuite Enterprise Plan"]


def _send_expiry_notifications(sub: dict, expiry_info: dict) -> tuple[bool, bool]:
	"""Send customized HTML emails to client and provider admin."""
	customer_name = sub.party
	sub_name = sub.name
	days_left = expiry_info["days_left"]
	target_date = expiry_info["target_date"]
	expiry_type = expiry_info["expiry_type"]
	status = sub.status
	plans = _get_subscription_plans_summary(sub_name)
	plans_str = ", ".join(plans)

	client_emails = _get_customer_emails(customer_name)
	admin_emails = _get_admin_emails()

	notified_client = False
	notified_admin = False

	# ── Client Email ──
	if client_emails:
		if status in ("Grace Period", "Past Due Date"):
			client_subject = f"Urgent: Action Required - Your MSuite Subscription is in Grace Period"
			urgency_header = "Subscription In Grace Period"
			desc_text = (
				f"Your MSuite subscription (<b>{sub_name}</b>) is currently in <b>Grace Period</b> due to an overdue payment. "
				f"Your workspace is currently in <b>Read-Only Mode</b> — all your historical campaigns, posts, and records remain safe. "
				f"Please complete your renewal payment promptly to restore full sending and publishing capabilities."
			)
		elif days_left == 0:
			client_subject = f"Your MSuite Subscription Expires Today ({target_date})"
			urgency_header = "Subscription Renewal Due Today"
			desc_text = (
				f"Your MSuite subscription (<b>{sub_name}</b>) reaches the end of its current period today (<b>{target_date}</b>). "
				f"Please ensure your renewal is processed to avoid any interruption in scheduled broadcasts or ad publishing."
			)
		elif days_left == 1:
			client_subject = f"Urgent Reminder: 1 Day Left on Your MSuite Subscription"
			urgency_header = "1 Day Until Renewal / Expiration"
			desc_text = (
				f"Your MSuite subscription (<b>{sub_name}</b>) will expire tomorrow on <b>{target_date}</b>. "
				f"Renew today to keep all active automations, live campaigns, and bulk broadcasts running without pause."
			)
		else:
			client_subject = f"Reminder: Your MSuite Subscription Expires in {days_left} Days"
			urgency_header = f"Subscription Renewal in {days_left} Days"
			desc_text = (
				f"This is a friendly reminder that your MSuite subscription (<b>{sub_name}</b>) is scheduled for renewal on <b>{target_date}</b> "
				f"({days_left} days remaining). All your workspace data, campaigns, and contacts remain fully active."
			)

		client_html = f"""
        <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;border:1px solid #e2e8f0;border-radius:8px;background:#ffffff;">
            <div style="margin-bottom:20px;">
                <span style="font-size:20px;font-weight:700;color:#0f172a;">MSuite</span>
            </div>
            <div style="padding:16px;background:#f8fafc;border-left:4px solid #3b82f6;border-radius:4px;margin-bottom:20px;">
                <h2 style="margin:0 0 8px 0;font-size:16px;color:#1e293b;">{urgency_header}</h2>
                <p style="margin:0;font-size:14px;color:#475569;line-height:1.5;">{desc_text}</p>
            </div>
            <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:24px;">
                <tr style="border-bottom:1px solid #f1f5f9;">
                    <td style="padding:8px 0;color:#64748b;">Customer:</td>
                    <td style="padding:8px 0;font-weight:600;color:#0f172a;text-align:right;">{customer_name}</td>
                </tr>
                <tr style="border-bottom:1px solid #f1f5f9;">
                    <td style="padding:8px 0;color:#64748b;">Subscription ID:</td>
                    <td style="padding:8px 0;font-weight:600;color:#0f172a;text-align:right;">{sub_name}</td>
                </tr>
                <tr style="border-bottom:1px solid #f1f5f9;">
                    <td style="padding:8px 0;color:#64748b;">Subscribed Plans:</td>
                    <td style="padding:8px 0;font-weight:600;color:#0f172a;text-align:right;">{plans_str}</td>
                </tr>
                <tr style="border-bottom:1px solid #f1f5f9;">
                    <td style="padding:8px 0;color:#64748b;">Renewal / Expiry Date:</td>
                    <td style="padding:8px 0;font-weight:600;color:#0f172a;text-align:right;">{target_date}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#64748b;">Current Status:</td>
                    <td style="padding:8px 0;font-weight:600;color:#0f172a;text-align:right;">{status}</td>
                </tr>
            </table>
            <div style="font-size:12px;color:#94a3b8;border-top:1px solid #f1f5f9;padding-top:16px;">
                If you have already processed payment, please ignore this reminder. For billing support, reply directly to this email.
            </div>
        </div>
        """

		frappe.sendmail(
			recipients=client_emails,
			subject=client_subject,
			message=client_html,
		)
		notified_client = True
		logger.info(f"Sent subscription expiry notice to client {customer_name} ({client_emails}) for {sub_name}")

	# ── Provider Admin Email ──
	if admin_emails:
		admin_subject = f"[Admin Alert] Subscription {sub_name} ({customer_name}) expiring in {days_left}d"
		if status in ("Grace Period", "Past Due Date"):
			admin_subject = f"[Admin Alert] Subscription {sub_name} ({customer_name}) is in Grace Period"

		admin_html = f"""
        <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;border:1px solid #e2e8f0;border-radius:6px;">
            <h3 style="margin-top:0;color:#0f172a;">MSuite Provider: Subscription Expiry Notice</h3>
            <p style="font-size:14px;color:#334155;">
                Customer <b>{customer_name}</b> subscription <b>{sub_name}</b> is reaching expiry/renewal.
            </p>
            <ul style="font-size:14px;color:#334155;line-height:1.6;">
                <li><b>Customer:</b> {customer_name}</li>
                <li><b>Subscription:</b> {sub_name}</li>
                <li><b>Plans:</b> {plans_str}</li>
                <li><b>Expiry / Target Date:</b> {target_date} ({days_left} days remaining)</li>
                <li><b>Status:</b> {status}</li>
                <li><b>Client Notified:</b> {'Yes (' + ', '.join(client_emails) + ')' if client_emails else 'No client email found'}</li>
            </ul>
        </div>
        """

		frappe.sendmail(
			recipients=admin_emails,
			subject=admin_subject,
			message=admin_html,
		)
		notified_admin = True
		logger.info(f"Sent subscription expiry notice to provider admin for {sub_name}")

	return notified_client, notified_admin
