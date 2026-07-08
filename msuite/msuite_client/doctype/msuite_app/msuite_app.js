frappe.ui.form.on("MSuite App", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.platform !== "AI Calling") return;

		frm.add_custom_button(__("Push to All Clients"), () => {
			frappe.confirm(
				__("Push this AI Calling backend URL to every Active client?"),
				() => {
					frappe
						.xcall(
							"msuite.msuite_client.doctype.msuite_app.msuite_app.push_ai_calling_config",
							{ app_name: frm.doc.name }
						)
						.then((r) => {
							frappe.show_alert({ message: r.message, indicator: "green" });
						});
				}
			);
		}).addClass("btn-primary");
	},
});
