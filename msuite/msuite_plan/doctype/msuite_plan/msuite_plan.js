/*
 * MSuite Plan form controller.
 *
 * Three states:
 *   1. Draft     — is_active=0, activated_on empty → "Activate Plan" button
 *   2. Active    — is_active=1 → "Deactivate Plan" button + Items dashboard
 *   3. Inactive  — is_active=0, activated_on set   → "Reactivate Plan" button
 */

frappe.ui.form.on("MSuite Plan", {
	refresh(frm) {
		// Auto-populate features when product is first selected
		if (
			frm.doc.product &&
			(!frm.doc.features || frm.doc.features.length === 0)
		) {
			frm.trigger("product");
		}

		if (frm.is_new()) return;

		const was_activated = !!frm.doc.activated_on;

		if (frm.doc.is_active) {
			// ── Active: Actions group ──
			frm.add_custom_button(
				__("Push to All Clients"),
				function() {
					frappe.call({
						method: "msuite.msuite_plan.doctype.msuite_plan.plan_sync.push_to_all_clients",
						args: { plan_name: frm.doc.name },
						freeze: true,
						freeze_message: __("Pushing plan to clients..."),
						callback: function(r) {
							if (r.message) {
								frappe.show_alert({
									message: r.message.message,
									indicator: r.message.failed ? "orange" : "green",
								});
							}
						}
					});
				},
				__("Actions")
			);

			frm.add_custom_button(
				__("Deactivate Plan"),
				() => {
					frappe.confirm(
						`Deactivate <b>${frm.doc.plan_name}</b>?<br><br>` +
							`Existing subscriptions continue but no new ` +
							`subscriptions can be created.`,
						() => {
							frappe
								.xcall(
									"msuite.msuite_plan.doctype.msuite_plan.plan_activation.deactivate_plan",
									{ plan_name: frm.doc.name }
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
				__("Reactivate Plan"),
				() => {
					frappe
						.xcall(
							"msuite.msuite_plan.doctype.msuite_plan.plan_activation.reactivate_plan",
							{ plan_name: frm.doc.name }
						)
						.then(() => frm.reload_doc());
				},
				__("Actions")
			);

			frm.set_intro(
				"This plan is inactive. No new subscriptions can be created.",
				"yellow"
			);
		} else {
			// ── Draft: show Activate button (primary) ──
			frm.add_custom_button(__("Activate Plan"), () => {
				frappe
					.xcall(
						"msuite.msuite_plan.doctype.msuite_plan.plan_activation.activate_plan",
						{ plan_name: frm.doc.name }
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
				"This plan is in draft. Configure pricing, features, " +
					"and grant rules, then click <b>Activate Plan</b>.",
				"blue"
			);
		}
	},

	product(frm) {
		// Auto-populate features table from the selected product
		if (
			frm.doc.product &&
			(!frm.doc.features || frm.doc.features.length === 0)
		) {
			frappe.call({
				method: "frappe.client.get",
				args: { doctype: "MSuite Product", name: frm.doc.product },
				callback(r) {
					if (r.message && r.message.features) {
						frm.clear_table("features");
						r.message.features.forEach((feat) => {
							let row = frm.add_child("features");
							row.product_feature = feat.name;
							row.is_enabled = 0;
						});
						frm.refresh_field("features");
						frappe.show_alert({
							message: `Added ${r.message.features.length} features from ${frm.doc.product}. Set enabled/limits for each.`,
							indicator: "blue",
						});
					}
				},
			});
		}
	},
});
