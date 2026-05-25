/*
 * MSuite Bundle form controller.
 *
 * Three states:
 *   1. Draft     — is_active=0, activated_on empty → "Activate Bundle" button
 *   2. Active    — is_active=1 → "Deactivate Bundle" button + Items dashboard
 *   3. Inactive  — is_active=0, activated_on set   → "Reactivate Bundle" button
 */

frappe.ui.form.on("MSuite Bundle", {
	refresh(frm) {
		if (frm.is_new()) return;

		const was_activated = !!frm.doc.activated_on;

		if (frm.doc.is_active) {
			// ── Active: show Deactivate button + Items dashboard ──
			frm.add_custom_button(
				__("Deactivate Bundle"),
				() => {
					frappe.confirm(
						`Deactivate <b>${frm.doc.bundle_name}</b>?<br><br>` +
							`Existing subscriptions continue but no new ` +
							`subscriptions can be created.`,
						() => {
							frappe
								.xcall(
									"msuite.msuite_bundle.doctype.msuite_bundle.msuite_bundle.deactivate_bundle",
									{ bundle_name: frm.doc.name }
								)
								.then(() => frm.reload_doc());
						}
					);
				},
				__("Actions")
			);

			// Show created Items in dashboard
			const items = (frm.doc.pricing || [])
				.filter((r) => r.item)
				.map(
					(r) =>
						`<b>${r.item}</b> (₹${r.rate}/${r.billing_interval})`
				)
				.join(", ");
			if (items) {
				frm.dashboard.add_comment(
					`Items: ${items}`,
					"blue",
					true
				);
			}
		} else if (was_activated) {
			// ── Deactivated: show Reactivate button ──
			frm.add_custom_button(
				__("Reactivate Bundle"),
				() => {
					frappe
						.xcall(
							"msuite.msuite_bundle.doctype.msuite_bundle.msuite_bundle.reactivate_bundle",
							{ bundle_name: frm.doc.name }
						)
						.then(() => frm.reload_doc());
				},
				__("Actions")
			);

			frm.set_intro(
				"This bundle is inactive. No new subscriptions can be created.",
				"yellow"
			);
		} else {
			// ── Draft: show Activate button (primary) ──
			frm.add_custom_button(__("Activate Bundle"), () => {
				frappe
					.xcall(
						"msuite.msuite_bundle.doctype.msuite_bundle.msuite_bundle.activate_bundle",
						{ bundle_name: frm.doc.name }
					)
					.then((r) => {
						frappe.show_alert({
							message: r.message,
							indicator: "green",
						});
						frm.reload_doc();
					});
			}).addClass("btn-primary");

			frm.set_intro(
				"This bundle is in draft. Configure pricing and components, " +
					"then click <b>Activate Bundle</b>.",
				"blue"
			);
		}
	},
});
