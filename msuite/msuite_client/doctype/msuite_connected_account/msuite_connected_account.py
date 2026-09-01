"""
MSuite Connected Account controller.

Generic auth/token record for any platform. Links 1-to-1 with
platform-specific doctypes (MSuite WhatsApp Account, etc.)
that hold platform-specific metadata.

Dedup: unique on (client + platform + account_id).
"""
import frappe
from frappe.model.document import Document

from msuite.constants import MSUITE_LOGGER_NAME

logger = frappe.logger(MSUITE_LOGGER_NAME)


class MSuiteConnectedAccount(Document):
    def validate(self):
        self._enforce_unique_account()

    def _enforce_unique_account(self):
        """One record per (client, platform, account_id)."""
        existing = frappe.db.exists(
            "MSuite Connected Account",
            {
                "client": self.client,
                "platform": self.platform,
                "account_id": self.account_id,
                "name": ["!=", self.name],
            },
        )
        if existing:
            frappe.throw(
                f"{self.platform} account {self.account_id} is already "
                f"connected for client {self.client}",
                frappe.ValidationError,
            )

    def on_update(self):
        if self.platform == "WhatsApp" and self.account_id:
            from msuite.services.aws_routing_sync import sync_waba_route
            sync_waba_route(self.account_id, self.client, status=self.status)

    def on_trash(self):
        if self.platform == "WhatsApp" and self.account_id:
            from msuite.services.aws_routing_sync import delete_waba_route
            delete_waba_route(self.account_id)

