"""
MSuite Bundle controller.

Lifecycle: Draft → Activate → (Active) → Deactivate → Reactivate
  - Draft: is_active=0, activated_on=null. Save only validates.
  - Activate: creates bundle Items (with is_msuite_bundle=1) + Subscription Plans.
  - Active: rate changes sync to Items/Sub Plans on save.
  - Deactivate: sets is_active=0. Existing subscriptions continue.
  - Reactivate: sets is_active=1. Items already exist.
"""
import re

import frappe
from frappe.model.document import Document
from frappe.utils import now

from msuite.constants import PlanTier
from msuite.exceptions import BundleConfigurationError, MSuiteError
from msuite.utils.validators import require_system_manager_or_msuite_manager

# Billing interval → item code suffix
INTERVAL_SUFFIX = {"Monthly": "MONTHLY", "Quarterly": "QUARTERLY", "Annual": "ANNUAL"}

# Billing interval → ERPNext Subscription Plan fields
INTERVAL_TO_ERPNEXT = {
    "Monthly": ("Month", 1),
    "Quarterly": ("Month", 3),
    "Annual": ("Year", 1),
}


class MSuiteBundle(Document):
    # ------------------------------------------------------------------
    # Frappe hooks
    # ------------------------------------------------------------------

    def validate(self):
        """Runs on every save. Validates data but does NOT create Items."""
        self._validate_bundle_code()
        self._validate_components()
        self._validate_no_duplicate_pricing_intervals()

    def on_update(self):
        """Sync rates to existing Items when bundle is active."""
        if self.is_active:
            self._sync_existing_rates()

    def on_trash(self):
        """Block deletion if bundle has financial history; cascade if safe."""
        self._validate_no_active_subscriptions()
        self._validate_no_historical_invoices()
        self._cascade_delete_if_safe()

    # ------------------------------------------------------------------
    # Validations (run on every save)
    # ------------------------------------------------------------------

    def _validate_bundle_code(self):
        if not re.match(r"^[A-Z][A-Z0-9-]*$", self.bundle_code or ""):
            frappe.throw(
                "Bundle Code must match ^[A-Z][A-Z0-9-]*$",
                frappe.ValidationError,
            )

    def _validate_components(self):
        """Validates minimum 2 components, no duplicates, no Trial-tier."""
        errors = []
        if len(self.components) < 2:
            errors.append("Bundle must have at least 2 components")

        plans = [row.plan for row in self.components]
        if len(plans) != len(set(plans)):
            errors.append("Duplicate plans in bundle components")

        for row in self.components:
            plan_data = frappe.db.get_value(
                "MSuite Plan", row.plan, ["is_active", "tier"], as_dict=True
            )
            if not plan_data:
                errors.append(f"Plan {row.plan} not found")
                continue
            if not plan_data.is_active:
                errors.append(f"Plan {row.plan} is not active")
            if plan_data.tier == PlanTier.TRIAL:
                errors.append(
                    f"Trial-tier plan {row.plan} cannot be a bundle component"
                )

        if errors:
            frappe.throw(
                "Bundle validation failed:\n" + "\n".join(errors),
                BundleConfigurationError,
            )

    def _validate_no_duplicate_pricing_intervals(self):
        seen = set()
        for row in self.pricing or []:
            if row.billing_interval in seen:
                frappe.throw(
                    f"Duplicate billing interval '{row.billing_interval}' "
                    "in pricing table",
                    frappe.ValidationError,
                )
            seen.add(row.billing_interval)

    # ------------------------------------------------------------------
    # Activation: validate readiness + create billing artifacts
    # ------------------------------------------------------------------

    def _validate_activation_readiness(self):
        """Validates that the bundle is fully configured for activation."""
        if self.is_active:
            frappe.throw("Bundle is already active", frappe.ValidationError)

        if not self.bundle_code:
            frappe.throw(
                "Bundle Code is required before activation",
                frappe.ValidationError,
            )

        if not self.pricing:
            frappe.throw(
                "At least one pricing row is required before activation",
                frappe.ValidationError,
            )

        # Components are validated in validate() on every save

    def _create_items_and_subscription_plans(self):
        """
        Creates ERPNext Item (with bundle flags) + Subscription Plan
        for each pricing row. Called once during activation. Idempotent.
        """
        if not self.bundle_code:
            return

        item_group = (
            "Bundles"
            if frappe.db.exists("Item Group", "Bundles")
            else "All Item Groups"
        )
        created = []

        for row in self.pricing or []:
            suffix = INTERVAL_SUFFIX.get(row.billing_interval, "MONTHLY")
            item_code = f"{self.bundle_code}-{suffix}"
            rate = float(row.rate or 0)

            # Skip if already created (idempotent)
            if row.item and frappe.db.exists("Item", row.item):
                self._sync_rate(row.item, rate)
                continue

            # Create Item with bundle flags
            if not frappe.db.exists("Item", item_code):
                item = frappe.new_doc("Item")
                item.item_code = item_code
                item.item_name = f"{self.bundle_name} ({row.billing_interval})"
                item.item_group = item_group
                item.stock_uom = "Nos"
                item.is_stock_item = 0
                item.include_item_in_manufacturing = 0
                item.standard_rate = rate
                item.is_msuite_bundle = 1
                item.msuite_bundle = self.name
                item.insert(ignore_permissions=True)

            # Create Subscription Plan
            if not frappe.db.exists("Subscription Plan", item_code):
                interval, count = INTERVAL_TO_ERPNEXT.get(
                    row.billing_interval, ("Month", 1)
                )
                sp = frappe.new_doc("Subscription Plan")
                sp.plan_name = item_code
                sp.item = item_code
                sp.price_determination = "Fixed Rate"
                sp.cost = rate
                sp.billing_interval = interval
                sp.billing_interval_count = count
                sp.insert(ignore_permissions=True)

            # Write back to the pricing row
            row.item = item_code
            row.subscription_plan = item_code
            created.append(f"{item_code} (₹{rate}/{row.billing_interval})")

        if created:
            frappe.msgprint(
                f"Created: {', '.join(created)}",
                alert=True,
                indicator="green",
            )

    # ------------------------------------------------------------------
    # Rate syncing (runs on every save of an active bundle)
    # ------------------------------------------------------------------

    def _sync_existing_rates(self):
        """Sync pricing rates to existing Items and Subscription Plans."""
        for row in self.pricing or []:
            if row.item and frappe.db.exists("Item", row.item):
                self._sync_rate(row.item, float(row.rate or 0))

    def _sync_rate(self, item_code, rate):
        """Update Item standard_rate and Subscription Plan cost if changed."""
        frappe.db.set_value("Item", item_code, "standard_rate", rate)
        sp = frappe.db.get_value(
            "Subscription Plan", {"item": item_code}, "name"
        )
        if sp:
            frappe.db.set_value("Subscription Plan", sp, "cost", rate)

    # ------------------------------------------------------------------
    # Delete protection
    # ------------------------------------------------------------------

    def _validate_no_active_subscriptions(self):
        item_codes = [r.item for r in (self.pricing or []) if r.item]
        if not item_codes:
            return

        sp_names = frappe.get_all(
            "Subscription Plan",
            filters={"item": ["in", item_codes]},
            pluck="name",
        )
        if not sp_names:
            return

        active_subs = frappe.get_all(
            "Subscription Plan Detail",
            filters={"plan": ["in", sp_names], "parenttype": "Subscription"},
            fields=["parent"],
            group_by="parent",
        )
        active_sub_names = []
        for s in active_subs:
            status = frappe.db.get_value(
                "Subscription", s.parent, ["docstatus", "status"], as_dict=True
            )
            if status and status.status not in ("Cancelled", "Completed"):
                active_sub_names.append(s.parent)

        if active_sub_names:
            display = ", ".join(active_sub_names[:3])
            if len(active_sub_names) > 3:
                display += "..."
            frappe.throw(
                f"Cannot delete {self.bundle_name}. "
                f"Active subscriptions reference bundle item(s): {display}. "
                f"Cancel all subscriptions first.",
                MSuiteError,
            )

    def _validate_no_historical_invoices(self):
        for row in self.pricing or []:
            if not row.item:
                continue
            if frappe.db.exists(
                "Sales Invoice Item",
                {"item_code": row.item, "docstatus": ["!=", 2]},
            ):
                frappe.throw(
                    f"Cannot delete {self.bundle_name}. Sales Invoices exist "
                    f"for item {row.item}. Deactivate the bundle instead.",
                    MSuiteError,
                )

    def _cascade_delete_if_safe(self):
        """Delete orphaned Subscription Plans and Items."""
        for row in self.pricing or []:
            if row.subscription_plan and frappe.db.exists(
                "Subscription Plan", row.subscription_plan
            ):
                frappe.delete_doc(
                    "Subscription Plan", row.subscription_plan,
                    ignore_permissions=True, force=True,
                )
            if row.item and frappe.db.exists("Item", row.item):
                try:
                    frappe.delete_doc(
                        "Item", row.item,
                        ignore_permissions=True, force=True,
                    )
                except Exception as e:
                    frappe.log_error(
                        f"Could not delete Item {row.item}: {str(e)}",
                        "MSuite Bundle Delete",
                    )


# ======================================================================
# Whitelisted activation/deactivation methods (called from JS buttons)
# ======================================================================


@frappe.whitelist()
def activate_bundle(bundle_name: str) -> dict:
    """
    Activate an MSuite Bundle.

    Validates readiness, creates ERPNext Items (with bundle flags) and
    Subscription Plans for each pricing row, sets is_active=1.

    Args:
        bundle_name: MSuite Bundle document name (bundle_code)

    Returns:
        Success dict with bundle name and message
    """
    require_system_manager_or_msuite_manager()
    doc = frappe.get_doc("MSuite Bundle", bundle_name)
    doc._validate_activation_readiness()
    doc._create_items_and_subscription_plans()
    doc.is_active = 1
    doc.activated_on = now()
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "status": "success",
        "bundle": doc.name,
        "message": f"Bundle '{doc.bundle_name}' activated successfully",
    }


@frappe.whitelist()
def deactivate_bundle(bundle_name: str) -> dict:
    """
    Deactivate an MSuite Bundle.

    Existing subscriptions continue. No new subscriptions can use this bundle.

    Args:
        bundle_name: MSuite Bundle document name

    Returns:
        Success dict
    """
    require_system_manager_or_msuite_manager()
    doc = frappe.get_doc("MSuite Bundle", bundle_name)
    if not doc.is_active:
        frappe.throw("Bundle is already inactive", frappe.ValidationError)

    doc.is_active = 0
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "status": "success",
        "message": f"Bundle '{doc.bundle_name}' deactivated",
    }


@frappe.whitelist()
def reactivate_bundle(bundle_name: str) -> dict:
    """
    Reactivate a previously activated bundle. Items already exist.

    Args:
        bundle_name: MSuite Bundle document name

    Returns:
        Success dict
    """
    require_system_manager_or_msuite_manager()
    doc = frappe.get_doc("MSuite Bundle", bundle_name)
    if doc.is_active:
        frappe.throw("Bundle is already active", frappe.ValidationError)

    # Verify Items still exist
    for row in doc.pricing or []:
        if row.item and not frappe.db.exists("Item", row.item):
            frappe.throw(
                f"Item {row.item} no longer exists. "
                "Cannot reactivate. Create a new bundle instead.",
                frappe.ValidationError,
            )

    doc.is_active = 1
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "status": "success",
        "message": f"Bundle '{doc.bundle_name}' reactivated",
    }
