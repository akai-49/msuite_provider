import frappe


def execute():
	"""Rename ai_analytics_bot feature key to analytics_agent and rename Query Log DocType & table."""
	# 1. Update Product Features
	if frappe.db.exists("DocType", "MSuite Product Feature"):
		rows = frappe.get_all(
			"MSuite Product Feature",
			filters={"feature_key": "ai_analytics_bot"},
			pluck="name",
		)
		for name in rows:
			frappe.db.set_value(
				"MSuite Product Feature",
				name,
				{"feature_key": "analytics_agent", "feature_label": "Analytics Agent"},
				update_modified=False,
			)
		if rows:
			print(f"MSuite Product Feature: ai_analytics_bot -> analytics_agent ({len(rows)} rows)")

	# 2. Rename DocType metadata if old DocType exists
	old_doctype = "MSuite AI Analytics Query Log"
	new_doctype = "MSuite Analytics Agent Query Log"

	if frappe.db.exists("DocType", old_doctype) and not frappe.db.exists("DocType", new_doctype):
		try:
			frappe.rename_doc("DocType", old_doctype, new_doctype, force=True, merge=False)
			print(f"Renamed DocType {old_doctype} -> {new_doctype}")
		except Exception as exc:
			print(f"DocType rename fallback: {exc}")

	# If both DocTypes exist in tabDocType, remove the obsolete one
	if frappe.db.exists("DocType", old_doctype) and frappe.db.exists("DocType", new_doctype):
		frappe.db.delete("DocField", {"parent": old_doctype})
		frappe.db.delete("DocPerm", {"parent": old_doctype})
		frappe.db.delete("DocType", {"name": old_doctype})

	# 3. Rename Query Log MariaDB Table if old table exists
	if frappe.db.table_exists(old_doctype) and not frappe.db.table_exists(new_doctype):
		try:
			frappe.db.sql(f"RENAME TABLE `tab{old_doctype}` TO `tab{new_doctype}`")
			print(f"Renamed table tab{old_doctype} -> tab{new_doctype}")
		except Exception as exc:
			print(f"Table rename skipped or failed: {exc}")

	# Clear table caches so model sync has fresh table listings
	frappe.flags.tables = None
	frappe.db.commit()
