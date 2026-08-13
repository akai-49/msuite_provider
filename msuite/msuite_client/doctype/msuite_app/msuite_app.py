"""
MSuite App controller.

Stores platform app credentials for OAuth and webhooks.
One record per platform app. App secrets are encrypted
and NEVER shared with client instances.

Supported platforms:
  - Meta WhatsApp: Embedded Signup (app_id, app_secret, config_id)
  - Meta Social: OAuth for Facebook/Instagram/Ads (app_id, app_secret, redirect_uri)
  - Google, TikTok, LinkedIn, Twitter: OAuth (app_id, app_secret, redirect_uri)
  - AI Calling: not an OAuth app — just the one shared AWS backend URL,
    universal across every client (organizations are distinguished by the
    X-Organization-ID header the backend receives on each call).
  - MillionVerifier: not an OAuth app — a single API key (app_secret) is
    all `msuite.api.v1.email_verify` needs; MillionVerifier has no
    app_id/client_id concept, so app_id is not required for this platform.
"""
import frappe
from frappe.model.document import Document
from frappe.utils import get_url, now

from msuite.constants import ClientStatus, SyncStatus, MSUITE_LOGGER_NAME
from msuite.utils.validators import require_system_manager_or_msuite_manager

logger = frappe.logger(MSUITE_LOGGER_NAME)


class MSuiteApp(Document):
    def validate(self):
        if self.platform == "AI Calling":
            if self.ai_backend_url:
                self.ai_backend_url = self.ai_backend_url.strip().rstrip("/")
            return
        self._auto_generate_webhook_url()
        self._auto_generate_deauthorize_url()

    def on_update(self):
        """Auto-push to every active client when the AI Calling backend
        URL actually changed. Only condition for pushing: a non-empty
        URL — an intentionally-cleared URL is left for the admin to push
        explicitly (via the button) so a typo-clear doesn't silently
        disable AI Calling everywhere."""
        if self.platform != "AI Calling":
            return
        doc_before = self.get_doc_before_save()
        changed = not doc_before or doc_before.get("ai_backend_url") != self.ai_backend_url
        if changed and self.is_active and self.ai_backend_url:
            push_ai_calling_config(self.name)

    def _auto_generate_webhook_url(self):
        """Auto-generate webhook URL only if empty (preserves manual edits for ngrok)."""
        if self.webhook_url:
            return
        base_url = get_url()
        platform_slug = (self.platform or "").lower().replace(" ", "_")
        self.webhook_url = (
            f"{base_url}/api/method/msuite.api.v1.webhook.receive_{platform_slug}"
        )

    def _auto_generate_deauthorize_url(self):
        """Auto-generate deauthorize URL only if empty (preserves manual edits)."""
        if self.deauthorize_callback_url:
            return
        if self.platform and self.platform.startswith("Meta"):
            base_url = get_url()
            self.deauthorize_callback_url = (
                f"{base_url}/api/method/msuite.api.v1.webhook.deauthorize_callback"
            )


@frappe.whitelist()
def push_ai_calling_config(app_name: str) -> dict:
    """
    Push the AI Calling backend URL to every Active MSuite Client.

    No shared secret is involved — the client's whitelisted endpoints
    that the AWS backend calls back into are unauthenticated for now.
    """
    require_system_manager_or_msuite_manager()
    app = frappe.get_doc("MSuite App", app_name)
    if app.platform != "AI Calling":
        frappe.throw("This is not an AI Calling app record.", frappe.ValidationError)
    if not app.ai_backend_url:
        frappe.throw("Set 'AI Backend URL' before pushing to clients.", frappe.ValidationError)

    from msuite.services.client_service import push_credentials_to_client

    credentials = {"ai_backend_url": app.ai_backend_url}

    clients = frappe.get_all(
        "MSuite Client",
        filters={"status": ClientStatus.ACTIVE},
        fields=["name"],
    )

    synced, failed = 0, 0
    for client in clients:
        client_doc = frappe.get_doc("MSuite Client", client.name)
        try:
            push_credentials_to_client(client_doc, "AI Calling", credentials)
            client_doc.last_sync = now()
            client_doc.last_sync_status = SyncStatus.SUCCESS
            client_doc.sync_fail_count = 0
            synced += 1
        except Exception as e:
            logger.warning(f"AI Calling push failed for client {client.name}: {e}")
            client_doc.last_sync = now()
            client_doc.last_sync_status = SyncStatus.FAILED
            client_doc.sync_fail_count = (client_doc.sync_fail_count or 0) + 1
            failed += 1
        client_doc.save(ignore_permissions=True)

    frappe.db.commit()
    summary = f"{synced} synced, {failed} failed (of {len(clients)} active client(s))"
    logger.info(f"AI Calling config push complete: {summary}")
    return {"status": "success", "message": summary, "synced": synced, "failed": failed}
