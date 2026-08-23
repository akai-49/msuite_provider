"""
Reload MSuite AWS Settings doctype and initialize min_recipient_threshold from doctype schema.
"""
import frappe


def execute():
	if not frappe.db.exists("DocType", "MSuite AWS Settings"):
		return

	frappe.reload_doc("msuite_client", "doctype", "msuite_aws_settings")

	meta = frappe.get_meta("MSuite AWS Settings")
	field = meta.get_field("min_recipient_threshold")
	if field and field.default:
		current = frappe.db.get_single_value("MSuite AWS Settings", "min_recipient_threshold")
		if not current:
			frappe.db.set_single_value("MSuite AWS Settings", "min_recipient_threshold", int(field.default))
			frappe.db.commit()
