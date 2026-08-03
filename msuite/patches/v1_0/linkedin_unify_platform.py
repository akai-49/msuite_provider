"""
Collapse "LinkedIn Page" / "LinkedIn Profile" into a single "LinkedIn" platform.

LinkedIn used to require two MSuite App records (two Client ID/Secret pairs,
two scope sets). It is now one app with the union of member and organization
scopes; `services/oauth/linkedin.py` decides at callback time whether to
connect the person or their pages.

Rewrites:

  tabMSuite Auth Account       platform -> "LinkedIn"
  tabMSuite Connected Account  platform -> "LinkedIn", account_id -> full URN
  tabMSuite App                platform -> "LinkedIn", deactivated (see below)

Connected Account ids are normalized to the same URN form the client stores,
so both sides agree on one identifier:

  "org:105964662"  ->  "urn:li:organization:105964662"
  "Wz6GWgFRrU"     ->  "urn:li:person:Wz6GWgFRrU"

The old MSuite App rows are set `is_active = 0` rather than deleted: their
credentials are still the only thing that could refresh already-issued
tokens, and Connected Account rows link to them. Create the new unified
app (platform "LinkedIn") in the desk and the OAuth layer will pick it up —
`get_msuite_app` only ever matches active records.

Idempotent: every statement is scoped to rows still holding a legacy value.
"""

import frappe

_LEGACY = ("LinkedIn Page", "LinkedIn Profile")

logger = frappe.logger("msuite")


def execute() -> None:
	frappe.reload_doc("msuite_client", "doctype", "msuite_app")
	frappe.reload_doc("msuite_client", "doctype", "msuite_auth_account")
	frappe.reload_doc("msuite_client", "doctype", "msuite_connected_account")

	_normalize_connected_account_ids()

	for doctype in ("MSuite Auth Account", "MSuite Connected Account"):
		if not frappe.db.table_exists(doctype):
			continue
		frappe.db.sql(
			f"""
			UPDATE `tab{doctype}`
			SET platform = 'LinkedIn'
			WHERE platform IN %(legacy)s
			""",
			{"legacy": _LEGACY},
		)

	_retire_legacy_apps()
	frappe.db.commit()


def _normalize_connected_account_ids() -> None:
	"""Rewrite the two pre-merge id shapes to full URNs.

	Runs before the platform rewrite so the legacy platform value still
	tells us whether a bare id belongs to an org or a person.
	"""
	if not frappe.db.table_exists("MSuite Connected Account"):
		return

	# "org:123" was the Page handler's format.
	frappe.db.sql(
		"""
		UPDATE `tabMSuite Connected Account`
		SET account_id = CONCAT('urn:li:organization:', SUBSTRING(account_id, 5))
		WHERE platform = 'LinkedIn Page'
		  AND account_id LIKE 'org:%%'
		"""
	)
	# The Profile handler stored the bare OIDC `sub`.
	frappe.db.sql(
		"""
		UPDATE `tabMSuite Connected Account`
		SET account_id = CONCAT('urn:li:person:', account_id)
		WHERE platform = 'LinkedIn Profile'
		  AND account_id NOT LIKE 'urn:li:%%'
		"""
	)


def _retire_legacy_apps() -> None:
	if not frappe.db.table_exists("MSuite App"):
		return

	legacy_apps = frappe.get_all(
		"MSuite App",
		filters={"platform": ["in", _LEGACY]},
		fields=["name", "is_active"],
	)
	for app in legacy_apps:
		frappe.db.set_value(
			"MSuite App",
			app["name"],
			{"platform": "LinkedIn", "is_active": 0},
			update_modified=False,
		)

	if legacy_apps:
		logger.warning(
			f"linkedin_unify_platform: deactivated {len(legacy_apps)} legacy "
			"LinkedIn app(s). Create one active MSuite App with platform "
			"'LinkedIn' and the unified scopes before users can reconnect."
		)
