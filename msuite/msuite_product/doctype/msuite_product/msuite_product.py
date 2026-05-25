import re

import frappe
from frappe.model.document import Document

from msuite.exceptions import MSuiteError


class MSuiteProduct(Document):
    def validate(self):
        self._validate_product_code()
        self._validate_feature_keys()
        self._check_duplicate_feature_keys()
        self._warn_if_deactivating()

    def on_trash(self):
        active_plans = frappe.db.get_list(
            "MSuite Plan",
            filters={"product": self.name, "is_active": 1},
            fields=["name"],
        )
        if active_plans:
            plan_names = ", ".join([p.name for p in active_plans])
            frappe.throw(
                f"Cannot delete product {self.name}: active plans exist ({plan_names})",
                MSuiteError,
            )

    def _validate_product_code(self):
        if not re.match(r"^[A-Z][A-Z_]*$", self.product_code or ""):
            frappe.throw(
                "Product Code must match ^[A-Z][A-Z_]*$ (uppercase letters and underscores only)",
                frappe.ValidationError,
            )

    def _validate_feature_keys(self):
        for row in self.features:
            if not re.match(r"^[a-z][a-z_]*$", row.feature_key or ""):
                frappe.throw(
                    f"Feature Key '{row.feature_key}' must match ^[a-z][a-z_]*$ (lowercase letters and underscores only)",
                    frappe.ValidationError,
                )

    def _check_duplicate_feature_keys(self):
        keys = [row.feature_key for row in self.features]
        seen = set()
        for key in keys:
            if key in seen:
                frappe.throw(
                    f"Duplicate feature key '{key}' in product features",
                    frappe.ValidationError,
                )
            seen.add(key)

    def _warn_if_deactivating(self):
        if self.is_new():
            return
        old_value = frappe.db.get_value("MSuite Product", self.name, "is_active")
        if old_value and not self.is_active:
            active_plans = frappe.db.count(
                "MSuite Plan", {"product": self.name, "is_active": 1}
            )
            if active_plans:
                frappe.msgprint(
                    f"Warning: {active_plans} active plan(s) exist for this product",
                    alert=True,
                )
