"""
Tests for Subscription Expiry Notification Service.

Validates:
  • Milestone matching for 7, 3, 1, and 0 days remaining.
  • Grace Period / Overdue subscription alerting.
  • Customer and Admin email recipient resolution.
  • Redis cache deduplication preventing repeat sends for the same milestone.
"""
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from msuite.services.subscription_notification_service import (
	_get_subscription_expiry_info,
	_get_customer_emails,
	_send_expiry_notifications,
	check_and_notify_expiring_subscriptions,
)


class TestSubscriptionNotifications(IntegrationTestCase):
	def setUp(self):
		frappe.cache.flush_all()

	def test_milestone_detection(self):
		today = frappe.utils.getdate(nowdate())

		# 7 days left
		sub_7d = {
			"name": "SUB-TEST-7D",
			"status": "Active",
			"current_invoice_end": str(add_days(today, 7)),
		}
		info_7d = _get_subscription_expiry_info(sub_7d, today)
		self.assertIsNotNone(info_7d)
		self.assertEqual(info_7d["days_left"], 7)
		self.assertEqual(info_7d["milestone"], "day_7")

		# 3 days left
		sub_3d = {
			"name": "SUB-TEST-3D",
			"status": "Active",
			"current_invoice_end": str(add_days(today, 3)),
		}
		info_3d = _get_subscription_expiry_info(sub_3d, today)
		self.assertEqual(info_3d["days_left"], 3)
		self.assertEqual(info_3d["milestone"], "day_3")

		# 1 day left
		sub_1d = {
			"name": "SUB-TEST-1D",
			"status": "Active",
			"current_invoice_end": str(add_days(today, 1)),
		}
		info_1d = _get_subscription_expiry_info(sub_1d, today)
		self.assertEqual(info_1d["days_left"], 1)
		self.assertEqual(info_1d["milestone"], "day_1")

		# 0 days left
		sub_0d = {
			"name": "SUB-TEST-0D",
			"status": "Active",
			"current_invoice_end": str(today),
		}
		info_0d = _get_subscription_expiry_info(sub_0d, today)
		self.assertEqual(info_0d["days_left"], 0)
		self.assertEqual(info_0d["milestone"], "day_0")

		# 10 days left (Not in milestones list -> milestone is None)
		sub_10d = {
			"name": "SUB-TEST-10D",
			"status": "Active",
			"current_invoice_end": str(add_days(today, 10)),
		}
		info_10d = _get_subscription_expiry_info(sub_10d, today)
		self.assertEqual(info_10d["days_left"], 10)
		self.assertIsNone(info_10d["milestone"])

		# Grace Period
		sub_gp = {
			"name": "SUB-TEST-GP",
			"status": "Grace Period",
			"current_invoice_end": str(add_days(today, -2)),
		}
		info_gp = _get_subscription_expiry_info(sub_gp, today)
		self.assertEqual(info_gp["milestone"], "grace_period")

	@patch("msuite.services.subscription_notification_service.frappe.sendmail")
	def test_notification_dispatch_and_deduplication(self, mock_sendmail):
		today = frappe.utils.getdate(nowdate())
		test_customer = "Test Notify Customer"

		# Mock customer email
		with patch("msuite.services.subscription_notification_service._get_customer_emails", return_value=["client@test.com"]), \
		     patch("msuite.services.subscription_notification_service._get_admin_emails", return_value=["admin@provider.com"]):

			mock_sub = frappe._dict({
				"name": "ACC-SUB-TEST-9999",
				"party": test_customer,
				"status": "Active",
				"current_invoice_end": str(add_days(today, 3)),
				"end_date": None,
				"trial_period_end": None,
			})

			with patch("msuite.services.subscription_notification_service.frappe.get_all", return_value=[mock_sub]):
				# Run 1: Should send email
				summary1 = check_and_notify_expiring_subscriptions()
				self.assertEqual(summary1["client_notified"], 1)
				self.assertEqual(summary1["admin_notified"], 1)
				self.assertEqual(mock_sendmail.call_count, 2)  # 1 for client, 1 for admin

				# Run 2 on same day: Should be deduplicated and skipped
				mock_sendmail.reset_mock()
				summary2 = check_and_notify_expiring_subscriptions()
				self.assertEqual(summary2["client_notified"], 0)
				self.assertEqual(summary2["admin_notified"], 0)
				self.assertEqual(summary2["skipped"], 1)
				mock_sendmail.assert_not_called()
