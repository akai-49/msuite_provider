import frappe

# feature_key -> (new key, new label)
RENAMES = {
	"voice_blasts": ("broadcasts", "Broadcasts"),
	"voice_blast_limit": ("broadcast_limit", "Broadcast Recipients"),
}


def execute():
	"""Rename AI Calling feature keys after the Voice Blast -> AI Calling Broadcast refactor.

	MSuite Product Feature is a child table with hash autoname, and MSuite Plan
	Feature links to it by docname, so changing feature_key breaks no links.
	Idempotent: rows already carrying the new key are skipped.
	"""
	if not frappe.db.exists("DocType", "MSuite Product Feature"):
		return

	for old, (new, label) in RENAMES.items():
		rows = frappe.get_all(
			"MSuite Product Feature", filters={"feature_key": old}, pluck="name"
		)
		for name in rows:
			frappe.db.set_value(
				"MSuite Product Feature",
				name,
				{"feature_key": new, "feature_label": label},
				update_modified=False,
			)
		if rows:
			print(f"MSuite Product Feature: {old} -> {new} ({len(rows)} rows)")

	# limit_label is free text on the plan rows ("100 per campaign").
	if frappe.db.exists("DocType", "MSuite Plan Feature"):
		stale = frappe.get_all(
			"MSuite Plan Feature",
			filters={"limit_label": ["like", "%per campaign%"]},
			fields=["name", "limit_label"],
		)
		for row in stale:
			frappe.db.set_value(
				"MSuite Plan Feature",
				row.name,
				"limit_label",
				row.limit_label.replace("per campaign", "per broadcast"),
				update_modified=False,
			)
		if stale:
			print(f"MSuite Plan Feature: limit_label per campaign -> per broadcast ({len(stale)} rows)")

	frappe.db.commit()
