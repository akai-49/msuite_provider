frappe.ui.form.on("Customer", {
    refresh(frm) {
        if (!frm.is_new()) {
            frm.trigger("show_msuite_dashboard");
        }
    },

    show_msuite_dashboard(frm) {
        frappe.call({
            method: "msuite.api.v1.entitlement.get_entitlements",
            args: { customer: frm.doc.name },
            callback(r) {
                if (r.message && r.message.status === "success") {
                    let data = r.message.data;
                    render_msuite_section(frm, data);
                }
            },
        });
    },
});

function render_msuite_section(frm, data) {
    // Remove old section if exists
    frm.fields_dict.msuite_info_html && frm.fields_dict.msuite_info_html.$wrapper.empty();

    if (!frm.fields_dict.msuite_info_html) return;

    let html = "";

    if (data.active_plans && data.active_plans.length > 0) {
        html += `<div class="mb-4">`;
        html += `<h6 class="text-muted mb-2">ACTIVE PLANS</h6>`;
        html += `<div class="d-flex flex-wrap gap-2">`;
        data.active_plans.forEach((plan) => {
            html += `<span class="badge" style="background-color: var(--bg-green); color: var(--text-on-green); padding: 6px 12px; font-size: 12px; border-radius: 6px;">${plan}</span>`;
        });
        html += `</div></div>`;
    } else {
        html += `<div class="mb-4 text-muted"><em>No active plans</em></div>`;
    }

    if (data.active_grants && data.active_grants.length > 0) {
        html += `<div class="mb-4">`;
        html += `<h6 class="text-muted mb-2">GRANTS</h6>`;
        html += `<table class="table table-sm table-bordered" style="font-size: 12px;">`;
        html += `<thead><tr><th>Grant</th><th>Plan</th><th>Type</th><th>Expires</th></tr></thead><tbody>`;
        data.active_grants.forEach((g) => {
            let badge_color = {
                Paid: "blue",
                Complimentary: "green",
                Bundle: "orange",
                Trial: "yellow",
            }[g.grant_type] || "grey";
            html += `<tr>
                <td><a href="/app/msuite-customer-grant/${g.grant_name}">${g.grant_name}</a></td>
                <td>${g.granted_plan}</td>
                <td><span class="indicator-pill ${badge_color}">${g.grant_type}</span></td>
                <td>${g.expires_on || "Never"}</td>
            </tr>`;
        });
        html += `</tbody></table></div>`;
    }

    if (data.features && Object.keys(data.features).length > 0) {
        html += `<div class="mb-4">`;
        html += `<h6 class="text-muted mb-2">FEATURES</h6>`;
        html += `<table class="table table-sm table-bordered" style="font-size: 12px;">`;
        html += `<thead><tr><th>Feature</th><th>Status</th><th>Limit</th><th>Source</th></tr></thead><tbody>`;
        Object.entries(data.features).forEach(([key, feat]) => {
            let status = feat.enabled
                ? `<span class="indicator-pill green">Enabled</span>`
                : `<span class="indicator-pill red">Disabled</span>`;
            let limit_str = feat.limit === null ? "Unlimited" : `${feat.limit} ${feat.limit_label || ""}`;
            html += `<tr>
                <td><strong>${key}</strong></td>
                <td>${status}</td>
                <td>${feat.enabled ? limit_str : "-"}</td>
                <td>${feat.source_plan || "-"}</td>
            </tr>`;
        });
        html += `</tbody></table></div>`;
    }

    frm.fields_dict.msuite_info_html.$wrapper.html(html);
}
