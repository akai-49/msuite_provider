import frappe
from frappe.model.document import Document
from frappe.utils import getdate, today


class MSuiteCouponUsage(Document):
    def validate(self):
        self._validate_unique_usage()
        self._validate_used_on_not_future()

    def _validate_unique_usage(self):
        existing = frappe.db.exists(
            "MSuite Coupon Usage",
            {"coupon_code": self.coupon_code, "customer": self.customer, "name": ["!=", self.name]},
        )
        if existing:
            frappe.throw(
                f"Customer {self.customer} has already used coupon {self.coupon_code}",
                frappe.ValidationError,
            )

    def _validate_used_on_not_future(self):
        if self.used_on and getdate(self.used_on) > getdate(today()):
            frappe.throw(
                "Used On date cannot be in the future",
                frappe.ValidationError,
            )
