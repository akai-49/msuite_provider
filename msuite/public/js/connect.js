/**
 * MSuite Connect — admin-mode page controller.
 *
 * Renders the platform card grid + connected-account list for an operator
 * who picks a client from the dropdown. All data comes from the public API
 * `msuite.api.v1.auth.get_connect_config`; this file contains no platform
 * configuration of its own, so adding a new platform requires only one
 * server-side change.
 *
 * The page hosts two flow types:
 *   • flow === "embedded_signup" → WhatsApp (uses Meta's FB.login SDK)
 *   • flow === "oauth_popup"     → everything else (standard OAuth popup)
 */
(function () {
    "use strict";

    const METHOD_CONFIG        = "msuite.api.v1.auth.get_connect_config";
    const METHOD_START_AUTH    = "msuite.api.v1.auth.start_auth";
    const METHOD_EXCHANGE_WA   = "msuite.api.v1.auth.exchange_whatsapp";
    const FB_SDK_URL           = "https://connect.facebook.net/en_US/sdk.js";
    const FB_SDK_GRAPH_VERSION = "v24.0";
    const FB_POPUP_ORIGINS     = [
        "https://www.facebook.com",
        "https://web.facebook.com",
    ];

    const state = {
        clientName: null,
        config:     null,
    };

    // ── Bootstrap ────────────────────────────────────────────────────────
    document.addEventListener("DOMContentLoaded", init);

    function init() {
        loadClients();
        bindClientSelector();
    }

    // ── Client list ──────────────────────────────────────────────────────
    function loadClients() {
        frappe.call({
            method: "frappe.client.get_list",
            args: {
                doctype: "MSuite Client",
                fields: ["name", "client_name", "status"],
                order_by: "client_name asc",
                limit_page_length: 100,
            },
            callback(r) {
                populateClientDropdown(r.message || []);
                show("#app");
                hide("#loading");
            },
        });
    }

    function populateClientDropdown(clients) {
        const select = document.getElementById("client-select");
        clients.forEach((c) => {
            const opt = document.createElement("option");
            opt.value = c.name;
            opt.textContent = `${c.client_name} (${c.status})`;
            select.appendChild(opt);
        });
    }

    function bindClientSelector() {
        document.getElementById("client-select").addEventListener("change", (e) => {
            const name = e.target.value;
            if (!name) {
                resetPage();
                return;
            }
            loadClient(name);
        });
    }

    function resetPage() {
        hide("#client-info", "#platform-grid", "#connected-section");
    }

    // ── Load client config ───────────────────────────────────────────────
    function loadClient(clientName) {
        frappe.call({
            method: METHOD_CONFIG,
            args: { client_name: clientName },
            callback(r) {
                if (!r.message || r.message.status !== "success") {
                    showAlert("Failed to load client config", "error");
                    return;
                }
                state.clientName = clientName;
                state.config     = r.message.data;
                renderAll();
            },
        });
    }

    function renderAll() {
        renderClientInfo();
        renderPlatformGrid();
        renderConnectedAccounts();
    }

    // ── Client info strip ────────────────────────────────────────────────
    function renderClientInfo() {
        const info   = document.getElementById("client-info");
        const status = document.getElementById("client-status");
        const cust   = document.getElementById("client-customer");

        const client = state.config.client;
        status.textContent = client.status;
        status.className   = `status-badge status-${client.status.toLowerCase()}`;
        cust.textContent   = client.customer ? `Customer: ${client.customer}` : "";
        show("#client-info");
    }

    // ── Platform grid ────────────────────────────────────────────────────
    function renderPlatformGrid() {
        const grid = document.getElementById("platform-grid");
        grid.innerHTML = "";

        const clientActive = state.config.client.status === "Active";

        if (!clientActive) {
            showAlert(
                "Client must be Active to connect accounts. Activate it from the MSuite Client form first.",
                "warning"
            );
        }

        state.config.platforms.forEach((p) => {
            grid.appendChild(buildPlatformCard(p, clientActive));
        });

        show("#platform-grid");
    }

    function buildPlatformCard(platform, clientActive) {
        const card = document.createElement("div");
        card.className = "platform-card";

        const disabled = !clientActive || !platform.ready;

        card.innerHTML = `
            <div class="platform-icon">${platform.icon}</div>
            <h3>${escapeHtml(platform.label)}</h3>
            <p>${escapeHtml(platform.description)}</p>
            <button class="btn btn-${platform.key}" ${disabled ? "disabled" : ""}>
                ${disabled ? "Unavailable" : "Connect"}
            </button>
            ${
                !platform.ready && platform.reason
                    ? `<div class="platform-reason">${escapeHtml(platform.reason)}</div>`
                    : ""
            }
        `;

        if (!disabled) {
            card.querySelector("button").addEventListener("click", () => {
                dispatchConnect(platform);
            });
        }

        return card;
    }

    // ── Connect dispatch ─────────────────────────────────────────────────
    function dispatchConnect(platform) {
        if (platform.flow === "embedded_signup") {
            connectWhatsApp(platform);
        } else {
            connectViaOAuthPopup(platform.key);
        }
    }

    // ── WhatsApp Embedded Signup ─────────────────────────────────────────
    function connectWhatsApp(platform) {
        if (!platform.app_id || !platform.config_id) {
            showAlert(
                "WhatsApp app is missing app_id or config_id.",
                "error"
            );
            return;
        }

        ensureFbSdk(platform.app_id).then(() => triggerWhatsAppLogin(platform));
    }

    function ensureFbSdk(appId) {
        return new Promise((resolve) => {
            if (typeof window.FB !== "undefined") {
                resolve();
                return;
            }
            const script = document.createElement("script");
            script.src = FB_SDK_URL;
            script.onload = () => {
                window.FB.init({
                    appId,
                    cookie:  true,
                    xfbml:   true,
                    version: FB_SDK_GRAPH_VERSION,
                });
                resolve();
            };
            document.head.appendChild(script);
        });
    }

    function triggerWhatsAppLogin(platform) {
        let session = null;

        const listener = (event) => {
            if (!FB_POPUP_ORIGINS.includes(event.origin)) return;
            try {
                const data = JSON.parse(event.data);
                if (data.type === "WA_EMBEDDED_SIGNUP") session = data;
            } catch (_) {
                // non-JSON message, ignore
            }
        };
        window.addEventListener("message", listener);

        window.FB.login(
            (response) => {
                window.removeEventListener("message", listener);
                if (!response.authResponse || !response.authResponse.code) {
                    showAlert("WhatsApp signup was cancelled.", "warning");
                    return;
                }
                exchangeWhatsAppCode(response.authResponse.code, session);
            },
            {
                config_id: platform.config_id,
                response_type: "code",
                override_default_response_type: true,
                extras: { setup: {} },
            }
        );
    }

    function exchangeWhatsAppCode(code, session) {
        showAlert("Exchanging code — please wait…", "info");

        const args = { client_name: state.clientName, code };
        if (session && session.data) {
            args.waba_id         = session.data.waba_id || "";
            args.phone_number_id = session.data.phone_number_id || "";
            args.event           = session.event || "FINISH";
            args.business_id     = session.data.business_id || "";
        }

        frappe.call({
            method: METHOD_EXCHANGE_WA,
            args,
            callback(r) {
                if (r.message && r.message.status === "success") {
                    const d = r.message.data;
                    showAlert(
                        `Connected ${d.waba_count} WABA(s) with ${d.phone_count} phone number(s).`,
                        "success"
                    );
                    loadClient(state.clientName);
                } else {
                    showAlert("WhatsApp connection failed.", "error");
                }
            },
            error(e) {
                showAlert("WhatsApp connection error: " + (e.message || e), "error");
            },
        });
    }

    // ── Standard OAuth popup ─────────────────────────────────────────────
    function connectViaOAuthPopup(platformKey) {
        frappe.call({
            method: METHOD_START_AUTH,
            args: { platform: platformKey, client_name: state.clientName },
            callback(r) {
                if (!r.message || r.message.status !== "success") {
                    showAlert(
                        "Failed to start OAuth: " + JSON.stringify(r.message),
                        "error"
                    );
                    return;
                }
                openOAuthPopup(r.message.data.auth_url);
            },
        });
    }

    function openOAuthPopup(url) {
        window.open(url, "msuite_oauth", "width=600,height=700,scrollbars=yes");

        const handler = (event) => {
            const data = event.data;
            if (!data || !data.type) return;

            if (data.type === "msuite_connected") {
                window.removeEventListener("message", handler);
                showAlert(`Connected ${data.count} account(s).`, "success");
                loadClient(state.clientName);
            } else if (data.type === "msuite_auth_error") {
                window.removeEventListener("message", handler);
                showAlert("Authorization failed: " + (data.error || "Unknown"), "error");
            }
        };
        window.addEventListener("message", handler);
    }

    // ── Connected accounts list ──────────────────────────────────────────
    function renderConnectedAccounts() {
        const section = document.getElementById("connected-section");
        const list    = document.getElementById("account-list");
        const accounts = state.config.connected_accounts || [];

        if (!accounts.length) {
            list.innerHTML = '<div class="empty-state">No accounts connected yet.</div>';
            show("#connected-section");
            return;
        }

        list.innerHTML = accounts.map(buildAccountRow).join("");
        show("#connected-section");
    }

    function buildAccountRow(acc) {
        const name = escapeHtml(acc.account_name || acc.account_id || "");
        const biz  = escapeHtml(acc.business_name || "");
        const status = escapeHtml(acc.status || "");
        const platform = escapeHtml(acc.platform || "");
        return `
            <div class="account-row">
                <div class="account-platform">${platform}</div>
                <div class="account-name">${name}</div>
                <div class="account-business">${biz}</div>
                <span class="account-status ${status}">${status}</span>
            </div>
        `;
    }

    // ── Alerts ───────────────────────────────────────────────────────────
    function showAlert(message, type) {
        const alerts = document.getElementById("alerts");
        const div = document.createElement("div");
        div.className = `alert alert-${type}`;
        div.textContent = message;
        alerts.innerHTML = "";
        alerts.appendChild(div);
        if (type !== "error") {
            setTimeout(() => div.remove(), 5000);
        }
    }

    // ── DOM helpers ──────────────────────────────────────────────────────
    function show(...selectors) {
        selectors.forEach((sel) => {
            const el = document.querySelector(sel);
            if (el) el.style.display = "";
        });
    }

    function hide(...selectors) {
        selectors.forEach((sel) => {
            const el = document.querySelector(sel);
            if (el) el.style.display = "none";
        });
    }

    function escapeHtml(text) {
        const div = document.createElement("div");
        div.textContent = text ?? "";
        return div.innerHTML;
    }
})();
