import frappe
from frappe.model.document import Document
from frappe.utils import getdate

from msuite.constants import GrantStatus
from msuite.exceptions import InvalidGrantStateError


class MSuiteCustomerGrant(Document):
    def validate(self):
        self._validate_dates()
        self._validate_status_transition()
        self._validate_revoked_fields()

    def on_update(self):
        from msuite.utils.cache import invalidate_entitlement_cache
        invalidate_entitlement_cache(self.customer)

    def _validate_dates(self):
        if self.expires_on and self.granted_on:
            if getdate(self.expires_on) <= getdate(self.granted_on):
                frappe.throw(
                    "Expires On must be after Granted On",
                    frappe.ValidationError,
                )

    def _validate_status_transition(self):
        if self.is_new():
            return
        old_status = frappe.db.get_value("MSuite Customer Grant", self.name, "status")
        if not old_status or old_status == self.status:
            return

        allowed = {
            GrantStatus.ACTIVE: [GrantStatus.REVOKED, GrantStatus.EXPIRED],
        }
        allowed_targets = allowed.get(old_status, [])
        if self.status not in allowed_targets:
            frappe.throw(
                f"Status transition from {old_status} to {self.status} is not allowed",
                InvalidGrantStateError,
            )

    def _validate_revoked_fields(self):
        if self.status == GrantStatus.REVOKED and not self.revoked_on:
            frappe.throw(
                "Revoked On date must be set when status is Revoked",
                frappe.ValidationError,
            )
