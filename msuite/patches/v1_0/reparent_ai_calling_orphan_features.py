import frappe

OLD_PARENT = "AI_CALLING"
NEW_PARENT = "AICALLING"
PARENTTYPE = "MSuite Product"
PARENTFIELD = "features"


def execute():
	"""Fold the orphaned `AI_CALLING` product-feature rows into `AICALLING`.

	`MSuite Product` is autonamed `field:product_code`, so the product code IS
	the docname. An earlier rename left 12 `MSuite Product Feature` child rows
	pointing at a parent `AI_CALLING` that no longer exists, while the live
	product `AICALLING` carries its own complete set of the same 12 keys.

	Per-row policy, so this is correct whichever way a given database drifted:

	  * key missing on AICALLING -> re-parent the row (record is preserved)
	  * key already on AICALLING -> the orphan is redundant. Repoint any
	    `MSuite Plan Feature.product_feature` links onto the surviving row
	    first, then drop the duplicate.

	The repoint step matters because plans link to product features by
	DOCNAME, not by feature_key: deleting a referenced row would silently
	blank that plan's feature and drop it out of entitlement resolution.
	On this database no plan row references the orphans, so it is a no-op —
	but a database where the orphans won the link is exactly the one this
	patch must not corrupt.

	Idempotent: once the orphans are gone the first query returns nothing.
	"""
	if not frappe.db.exists("DocType", "MSuite Product Feature"):
		return

	# Nothing to migrate into — bail rather than invent a parent.
	if not frappe.db.exists("MSuite Product", NEW_PARENT):
		return

	orphans = frappe.db.get_all(
		"MSuite Product Feature",
		filters={"parent": OLD_PARENT, "parenttype": PARENTTYPE},
		fields=["name", "feature_key"],
		order_by="idx asc",
	)
	if not orphans:
		return

	live = {
		r.feature_key: r.name
		for r in frappe.db.get_all(
			"MSuite Product Feature",
			filters={"parent": NEW_PARENT, "parenttype": PARENTTYPE},
			fields=["name", "feature_key"],
		)
	}

	max_idx = (
		frappe.db.sql(
			"""select coalesce(max(idx), 0) from `tabMSuite Product Feature`
			where parent = %s and parenttype = %s""",
			(NEW_PARENT, PARENTTYPE),
		)[0][0]
		or 0
	)

	reparented = repointed = removed = 0

	for row in orphans:
		twin = live.get(row.feature_key)

		if not twin:
			max_idx += 1
			frappe.db.set_value(
				"MSuite Product Feature",
				row.name,
				{
					"parent": NEW_PARENT,
					"parenttype": PARENTTYPE,
					"parentfield": PARENTFIELD,
					"idx": max_idx,
				},
				update_modified=False,
			)
			live[row.feature_key] = row.name
			reparented += 1
			continue

		refs = frappe.db.get_all(
			"MSuite Plan Feature", filters={"product_feature": row.name}, pluck="name"
		)
		for ref in refs:
			frappe.db.set_value(
				"MSuite Plan Feature", ref, "product_feature", twin, update_modified=False
			)
		repointed += len(refs)

		frappe.db.delete("MSuite Product Feature", {"name": row.name})
		removed += 1

	frappe.db.commit()

	print(
		f"MSuite Product Feature: {OLD_PARENT} -> {NEW_PARENT} "
		f"({reparented} re-parented, {removed} redundant removed, "
		f"{repointed} plan links repointed)"
	)
