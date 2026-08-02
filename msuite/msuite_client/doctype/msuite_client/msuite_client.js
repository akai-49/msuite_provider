/*
 * MSuite Client form controller.
 *
 * Four states:
 *   1. Draft        → "Test Connection" + "Activate" buttons
 *   2. Active       → "Push Plan" + "Suspend" buttons + sync dashboard
 *   3. Suspended    → "Reactivate" button
 *   4. Disconnected → "Test Connection" + "Reactivate" buttons
 */

const METHOD_PREFIX =
	"msuite.msuite_client.doctype.msuite_client.msuite_client";

frappe.ui.form.on("MSuite Client", {
	refresh(frm) {
		if (frm.is_new()) return;

		// ── Draft: Test + Activate ──
		if (frm.doc.status === "Draft") {
			frm.add_custom_button(__("Test Connection"), () => {
				frappe
					.xcall(`${METHOD_PREFIX}.test_connection`, {
						client_name: frm.doc.name,
					})
					.then((r) => {
						if (r.status === "ok") {
							frappe.show_alert({
								message: `Connection OK (${r.app} v${r.app_version})`,
								indicator: "green",
							});
						} else {
							frappe.show_alert({
								message: `Connection failed: ${r.message || "Unknown error"}`,
								indicator: "red",
							});
						}
					})
					.catch((e) => {
						frappe.show_alert({
							message: `Connection failed: ${e.message || e}`,
							indicator: "red",
						});
					});
			});

			frm.add_custom_button(__("Activate"), () => {
				frappe.confirm(
					`Activate client <b>${frm.doc.client_name}</b>?<br><br>` +
						`This will generate credentials and push plan data to ` +
						`<b>${frm.doc.client_url}</b>.`,
					() => {
						frappe
							.xcall(`${METHOD_PREFIX}.activate_client`, {
								client_name: frm.doc.name,
							})
							.then((r) => {
								frappe.show_alert({
									message: r.message,
									indicator: "green",
								});
								frm.reload_doc();
							});
					}
				);
			}).addClass("btn-primary");

			frm.set_intro(
				"Enter the client URL and click <b>Test Connection</b>, then <b>Activate</b>.",
				"blue"
			);
		}

		// ── Active: Actions + Connect + Suspend ──
		else if (frm.doc.status === "Active") {

			// Actions group: Push Plan + Push Credentials + Suspend
			frm.add_custom_button(__("Push Plan"), () => {
				frappe
					.xcall(`${METHOD_PREFIX}.push_plan`, {
						client_name: frm.doc.name,
					})
					.then((r) => {
						frappe.show_alert({ message: r.message, indicator: "green" });
						frm.reload_doc();
					});
			}, __("Actions"));

			frm.add_custom_button(__("Push Credentials"), () => {
				frappe.confirm(
					__("Re-push all connected account credentials (tokens, metadata) to this client?"),
					() => {
						frappe
							.xcall(`${METHOD_PREFIX}.push_credentials`, {
								client_name: frm.doc.name,
							})
							.then((r) => {
								frappe.show_alert({ message: r.message, indicator: "green" });
								frm.reload_doc();
							});
					}
				);
			}, __("Actions"));

			frm.page.set_inner_btn_group_as_primary(__("Actions"));

			// ── Connect buttons (Integrations group) ──
			frm.add_custom_button(
				__("WhatsApp (Embedded Signup)"),
				() => _launch_whatsapp_signup(frm),
				__("Connect")
			);

			frm.add_custom_button(
				__("Meta (Facebook & Instagram)"),
				() => _launch_oauth_popup(frm, "meta_social"),
				__("Connect")
			);

			frm.add_custom_button(
				__("Meta Ads"),
				() => _launch_oauth_popup(frm, "meta_ads"),
				__("Connect")
			);

			frm.add_custom_button(
				__("Suspend"),
				() => {
					frappe.confirm(
						`Suspend client <b>${frm.doc.client_name}</b>?<br><br>` +
							`The client will be notified and features will be disabled.`,
						() => {
							frappe
								.xcall(`${METHOD_PREFIX}.suspend_client`, {
									client_name: frm.doc.name,
								})
								.then(() => frm.reload_doc());
						}
					);
				},
				__("Actions")
			);

			// Show sync health in dashboard
			const sync_info = frm.doc.last_sync
				? `Last sync: ${frappe.datetime.prettyDate(frm.doc.last_sync)} (${frm.doc.last_sync_status || "Unknown"})`
				: "Never synced";
			frm.dashboard.add_comment(sync_info, "blue", true);

			if (frm.doc.sync_fail_count > 0) {
				frm.dashboard.add_comment(
					`${frm.doc.sync_fail_count} consecutive sync failure(s)`,
					"orange",
					true
				);
			}

			// Show connected accounts
			_load_connected_accounts(frm);
		}

		// ── Suspended: Reactivate ──
		else if (frm.doc.status === "Suspended") {
			frm.add_custom_button(__("Reactivate"), () => _reactivate(frm), __("Actions"));
			frm.set_intro("This client is suspended. Features are disabled.", "orange");
		}

		// ── Disconnected: Test + Reactivate ──
		else if (frm.doc.status === "Disconnected") {
			frm.add_custom_button(__("Test Connection"), () => {
				frappe
					.xcall(`${METHOD_PREFIX}.test_connection`, {
						client_name: frm.doc.name,
					})
					.then((r) => {
						if (r.status === "ok") {
							frappe.show_alert({
								message: "Connection restored!",
								indicator: "green",
							});
						}
					});
			});

			frm.add_custom_button(__("Reactivate"), () => _reactivate(frm), __("Actions"));

			frm.set_intro(
				"This client is disconnected (3+ sync failures). Test connection and reactivate.",
				"red"
			);
		}
	},
});

// ── Reactivate flow (handles fresh-instance detection) ──

function _reactivate(frm) {
	frappe.xcall(`${METHOD_PREFIX}.reactivate_client`, {
		client_name: frm.doc.name,
	}).then((r) => {
		if (r && r.status === "needs_fresh_activation") {
			frappe.confirm(
				`${r.message}<br><br>Regenerate credentials and activate fresh?`,
				() => {
					frappe.xcall(`${METHOD_PREFIX}.activate_client`, {
						client_name: frm.doc.name,
					}).then((r2) => {
						frappe.show_alert({ message: r2.message, indicator: "green" });
						frm.reload_doc();
					});
				}
			);
			return;
		}
		frappe.show_alert({ message: r.message, indicator: "green" });
		frm.reload_doc();
	});
}

// ── WhatsApp Embedded Signup ──

function _launch_whatsapp_signup(frm) {
	// Fetch MSuite App config for WhatsApp
	frappe.call({
		method: "frappe.client.get_list",
		args: {
			doctype: "MSuite App",
			filters: { platform: "Meta WhatsApp", is_active: 1 },
			fields: ["app_id", "config_id", "allow_coexistence"],
			limit_page_length: 1,
		},
		callback(r) {
			if (!r.message || !r.message.length) {
				frappe.msgprint(
					"No active Meta WhatsApp app configured. Create one in MSuite App.",
					"Error"
				);
				return;
			}
			const app = r.message[0];
			_do_whatsapp_embedded_signup(frm, app.app_id, app.config_id, app.allow_coexistence);
		},
	});
}

function _do_whatsapp_embedded_signup(frm, app_id, config_id, allow_coexistence) {
	// Load Facebook SDK if not already loaded
	if (typeof FB === "undefined") {
		const script = document.createElement("script");
		script.src = "https://connect.facebook.net/en_US/sdk.js";
		script.onload = () => {
			FB.init({ appId: app_id, cookie: true, xfbml: true, version: "v24.0" });
			_trigger_fb_login(frm, config_id, allow_coexistence);
		};
		document.head.appendChild(script);
	} else {
		_trigger_fb_login(frm, config_id, allow_coexistence);
	}
}

function _trigger_fb_login(frm, config_id, allow_coexistence) {
	// Capture session info from Embedded Signup v2 (waba_id, phone_number_id)
	let session_info = null;
	let coexistence_rejected = false;

	const sessionInfoListener = (event) => {
		if (
			event.origin !== "https://www.facebook.com" &&
			event.origin !== "https://web.facebook.com"
		)
			return;

		try {
			const data = JSON.parse(event.data);
			if (data.type === "WA_EMBEDDED_SIGNUP") {
				if (data.event === "FINISH" || data.event === "FINISH_ONLY_WABA") {
					session_info = data;
				} else if (data.event === "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING") {
					if (!allow_coexistence) {
						coexistence_rejected = true;
					} else {
						session_info = data;
					}
				} else if (data.event === "CANCEL") {
					// Log cancel event for consent audit
					frappe.xcall("msuite.api.v1.auth.log_signup_event", {
						client_name: frm.doc.name,
						event: "CANCEL",
						current_step: data.data?.current_step || "",
						waba_id: data.data?.waba_id || "",
					});
				} else if (data.event === "ERROR") {
					// Log error event for consent audit
					frappe.xcall("msuite.api.v1.auth.log_signup_event", {
						client_name: frm.doc.name,
						event: "ERROR",
						error_message: data.data?.error_message || "",
						waba_id: data.data?.waba_id || "",
					});
				}
			}
		} catch (e) {
			// Not JSON — ignore
		}
	};

	window.addEventListener("message", sessionInfoListener);

	FB.login(
		(response) => {
			window.removeEventListener("message", sessionInfoListener);

			if (coexistence_rejected) {
				frappe.msgprint(
					"Coexistence onboarding is disabled. This customer is already using " +
					"WhatsApp Business App. Enable 'Allow Coexistence Onboarding' in " +
					"MSuite App settings to allow connecting existing Business App numbers.",
					"Coexistence Not Allowed"
				);
				return;
			}

			if (response.authResponse && response.authResponse.code) {
				frappe.show_alert({
					message: "Exchanging code...",
					indicator: "blue",
				});

				const payload = {
					client_name: frm.doc.name,
					code: response.authResponse.code,
				};

				// Include all session_info data captured by the listener
				if (session_info && session_info.data) {
					payload.waba_id = session_info.data.waba_id || "";
					payload.phone_number_id = session_info.data.phone_number_id || "";
					payload.event = session_info.event || "FINISH";
					payload.business_id = session_info.data.business_id || "";
				}

				frappe
					.xcall(
						"msuite.api.v1.auth.exchange_whatsapp",
						payload
					)
					.then((r) => {
						if (r.status === "success") {
							const d = r.data;
							frappe.show_alert({
								message: `Connected ${d.waba_count} WABA(s), ${d.phone_count} phone(s)`,
								indicator: "green",
							});
							frm.reload_doc();
						}
					});
			} else {
				frappe.show_alert({
					message: "WhatsApp signup cancelled or failed",
					indicator: "orange",
				});
			}
		},
		{
			config_id: config_id,
			response_type: "code",
			override_default_response_type: true,
			scope: "whatsapp_business_management,whatsapp_business_messaging",
			extras: {
				setup: {},
				// Without this the dialog only offers "Create a WhatsApp
				// Business account" — existing Business App numbers are
				// never listed.
				...(allow_coexistence
					? { featureType: "whatsapp_business_app_onboarding" }
					: {}),
			},
		}
	);
}

// ── OAuth popup (works for any registered platform) ──

function _launch_oauth_popup(frm, platform) {
	frappe
		.xcall("msuite.api.v1.auth.start_auth", {
			platform,
			client_name: frm.doc.name,
		})
		.then((r) => {
			if (r.status === "success") {
				const popup = window.open(
					r.data.auth_url,
					"msuite_oauth",
					"width=600,height=700,scrollbars=yes"
				);

				const handler = (event) => {
					const data = event.data;
					if (data && data.type === "msuite_connected") {
						window.removeEventListener("message", handler);
						frappe.show_alert({
							message: `Connected ${data.count} account(s)`,
							indicator: "green",
						});
						frm.reload_doc();
					} else if (data && data.type === "msuite_auth_error") {
						window.removeEventListener("message", handler);
						frappe.msgprint(data.error || "Authorization failed", "Error");
					}
				};
				window.addEventListener("message", handler);
			}
		});
}

// ── Connected Accounts Dashboard ──

function _load_connected_accounts(frm) {
	// Load auth accounts first, then connected accounts grouped under them
	frappe.call({
		method: "frappe.client.get_list",
		args: {
			doctype: "MSuite Auth Account",
			filters: { client: frm.doc.name },
			fields: ["name", "platform", "account_name", "account_id", "verification_status"],
			order_by: "platform asc",
			limit_page_length: 50,
		},
		callback(r) {
			const auth_accounts = r.message || [];

			frappe.call({
				method: "frappe.client.get_list",
				args: {
					doctype: "MSuite Connected Account",
					filters: { client: frm.doc.name },
					fields: [
						"name", "platform", "display_name", "account_id",
						"status", "auth_account", "connected_at",
					],
					order_by: "platform asc, connected_at desc",
					limit_page_length: 100,
				},
				callback(r2) {
					_render_accounts_dashboard(frm, auth_accounts, r2.message || []);
				},
			});
		},
	});
}

function _render_accounts_dashboard(frm, auth_accounts, connected_accounts) {
	if (!auth_accounts.length && !connected_accounts.length) return;

	let html = '<h6 class="text-muted mt-3 mb-2">CONNECTED ACCOUNTS</h6>';

	// Group connected accounts by auth_account
	const grouped = {};
	const ungrouped = [];

	for (const acc of connected_accounts) {
		if (acc.auth_account) {
			if (!grouped[acc.auth_account]) grouped[acc.auth_account] = [];
			grouped[acc.auth_account].push(acc);
		} else {
			ungrouped.push(acc);
		}
	}

	// Render each auth account group
	for (const auth of auth_accounts) {
		const verified = auth.verification_status === "VERIFIED"
			? ' <span class="indicator-pill green" style="font-size:10px;">Verified</span>'
			: "";
		html += `<div style="margin-bottom:12px;">`;
		html += `<div style="font-weight:600;font-size:13px;margin-bottom:4px;">`;
		html += `<a href="/app/msuite-auth-account/${auth.name}">${auth.account_name || auth.account_id}</a>`;
		html += ` <span style="color:#868e96;font-weight:400;">(${auth.platform})</span>${verified}</div>`;

		const children = grouped[auth.name] || [];
		if (children.length) {
			html += '<table class="table table-sm table-bordered" style="font-size:12px;margin-bottom:0;">';
			for (const acc of children) {
				const badge = { Active: "green", Expired: "orange", Revoked: "red" }[acc.status] || "grey";
				const revoke_btn = acc.status === "Active"
					? `<button class="btn btn-xs btn-danger revoke-access-btn" data-account="${acc.name}" data-name="${acc.display_name || acc.account_id}" style="font-size:11px;">Revoke</button>`
					: "";
				html += `<tr>
					<td style="width:100px;">${acc.platform}</td>
					<td><a href="/app/msuite-connected-account/${acc.name}">${acc.display_name || acc.account_id}</a></td>
					<td style="width:80px;"><span class="indicator-pill ${badge}">${acc.status}</span></td>
					<td style="width:70px;text-align:center;">${revoke_btn}</td>
				</tr>`;
			}
			html += "</table>";
		} else {
			html += '<div style="font-size:12px;color:#868e96;padding:4px 8px;">No accounts connected</div>';
		}
		html += "</div>";
	}

	// Render ungrouped accounts (no auth account)
	if (ungrouped.length) {
		html += '<div style="margin-bottom:12px;">';
		html += '<div style="font-weight:600;font-size:13px;margin-bottom:4px;color:#868e96;">Other Accounts</div>';
		html += '<table class="table table-sm table-bordered" style="font-size:12px;margin-bottom:0;">';
		for (const acc of ungrouped) {
			const badge = { Active: "green", Expired: "orange", Revoked: "red" }[acc.status] || "grey";
			const revoke_btn = acc.status === "Active"
				? `<button class="btn btn-xs btn-danger revoke-access-btn" data-account="${acc.name}" data-name="${acc.display_name || acc.account_id}" style="font-size:11px;">Revoke</button>`
				: "";
			html += `<tr>
				<td style="width:100px;">${acc.platform}</td>
				<td><a href="/app/msuite-connected-account/${acc.name}">${acc.display_name || acc.account_id}</a></td>
				<td style="width:80px;"><span class="indicator-pill ${badge}">${acc.status}</span></td>
				<td style="width:70px;text-align:center;">${revoke_btn}</td>
			</tr>`;
		}
		html += "</table></div>";
	}

	frm.dashboard.add_comment(html, "blue", true);

	// Bind revoke buttons
	frm.dashboard.$body.find('.revoke-access-btn').on('click', function() {
		const account = $(this).data('account');
		const display = $(this).data('name');
		frappe.confirm(
			__('Revoke access for <b>{0}</b>? This will permanently invalidate the token.', [display]),
			function() {
				frappe.xcall(
					'msuite.msuite_client.doctype.msuite_client.msuite_client.revoke_whatsapp_access',
					{ connected_account_name: account }
				).then(function(r) {
					frappe.show_alert({ message: r.message, indicator: 'green' });
					frm.reload_doc();
				});
			}
		);
	});
}
