"""
MSuite Auth Account controller.

Top-level authenticated entity per platform per client.
  - Meta: represents a Meta Business Account
  - Google: represents a Google Account
  - TikTok, LinkedIn, Twitter: represents the user's account

All Connected Accounts (WhatsApp, Facebook, Instagram, etc.) link to this
as their parent grouping entity.

Dedup: unique on (client + platform + account_id).
"""
import frappe
from frappe.model.document import Document

from msuite.constants import MSUITE_LOGGER_NAME

logger = frappe.logger(MSUITE_LOGGER_NAME)


class MSuiteAuthAccount(Document):
    def validate(self):
        self._enforce_unique()

    def _enforce_unique(self):
        """One record per (client, platform, account_id)."""
        existing = frappe.db.exists(
            "MSuite Auth Account",
            {
                "client": self.client,
                "platform": self.platform,
                "account_id": self.account_id,
                "name": ["!=", self.name],
            },
        )
        if existing:
            frappe.throw(
                f"{self.platform} auth account {self.account_id} already exists "
                f"for client {self.client}",
                frappe.ValidationError,
            )
