"""
MSuite Plan — controller.

Handles document lifecycle (validate, on_update, on_trash).
Activation + billing artifacts delegated to plan_activation.py.
Client sync delegated to plan_sync.py.

Lifecycle: Draft → Activate → (Active) → Deactivate → Reactivate
"""

import re

import frappe
from frappe.model.document import Document

from msuite.exceptions import MSuiteError

from .plan_activation import (
    sync_existing_rates,
    sync_legacy_item_field,
    sync_pricing_rules,
    validate_no_active_grants,
    validate_no_active_subscriptions,
    validate_no_historical_invoices,
    cascade_delete_if_safe,
)


class MSuitePlan(Document):

    def validate(self):
        """Runs on every save. Validates data but does NOT create Items."""
        self._validate_plan_code()
        self._validate_no_duplicate_features()
        self._validate_no_duplicate_pricing_intervals()
        self._warn_missing_features()
        self._validate_grants()

        # Sync rates to existing Items when plan is active
        if self.is_active:
            sync_existing_rates(self)

        sync_legacy_item_field(self)

    def on_update(self):
        """Sync Pricing Rules for grant rules after every save."""
        if self.is_active:
            sync_pricing_rules(self)

    def on_trash(self):
        """Block deletion if plan has financial history; cascade if safe."""
        validate_no_active_grants(self)
        validate_no_active_subscriptions(self)
        validate_no_historical_invoices(self)
        cascade_delete_if_safe(self)

    # ── Validations ──────────────────────────────────────────────────────

    def _validate_plan_code(self):
        if not re.match(r"^[A-Z][A-Z0-9-]*$", self.plan_code or ""):
            frappe.throw(
                "Plan Code must match ^[A-Z][A-Z0-9-]*$ "
                "(uppercase letters, digits, hyphens)",
                frappe.ValidationError,
            )

    def _validate_no_duplicate_features(self):
        seen = set()
        for row in self.features:
            if row.product_feature in seen:
                frappe.throw(
                    f"Duplicate product feature '{row.product_feature}' in plan features",
                    frappe.ValidationError,
                )
            seen.add(row.product_feature)

    def _validate_no_duplicate_pricing_intervals(self):
        seen = set()
        for row in (self.pricing or []):
            if row.billing_interval in seen:
                frappe.throw(
                    f"Duplicate billing interval '{row.billing_interval}' in pricing table",
                    frappe.ValidationError,
                )
            seen.add(row.billing_interval)

    def _warn_missing_features(self):
        """Non-blocking warning if plan doesn't cover all product features."""
        if not self.product:
            return
        product_doc = frappe.get_doc("MSuite Product", self.product)
        product_feature_names = {f.name for f in product_doc.features}
        plan_feature_names = {row.product_feature for row in self.features}
        missing = product_feature_names - plan_feature_names
        if missing:
            frappe.msgprint(
                f"Plan does not cover all product features. Missing: {len(missing)} feature(s)",
                alert=True,
            )

    def _validate_grants(self):
        """Prevents self-grants and circular grants."""
        for row in self.grants:
            if row.granted_plan == self.name:
                frappe.throw("Plan cannot grant itself (self-grant not allowed)", MSuiteError)

            if not frappe.db.get_value("MSuite Plan", row.granted_plan, "is_active"):
                frappe.throw(f"Granted plan {row.granted_plan} is not active", MSuiteError)

            # One-level circular grant check
            granted_doc = frappe.get_doc("MSuite Plan", row.granted_plan)
            for sub_grant in granted_doc.grants:
                if sub_grant.granted_plan == self.name:
                    frappe.throw(
                        f"Circular grant: {self.name} grants {row.granted_plan}, "
                        f"which grants {self.name}",
                        MSuiteError,
                    )
