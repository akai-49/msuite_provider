"""
MSuite App controller.

Stores platform app credentials for OAuth and webhooks.
One record per platform app. App secrets are encrypted
and NEVER shared with client instances.

Supported platforms:
  - Meta WhatsApp: Embedded Signup (app_id, app_secret, config_id)
  - Meta Social: OAuth for Facebook/Instagram/Ads (app_id, app_secret, redirect_uri)
  - Google, TikTok, LinkedIn, Twitter: OAuth (app_id, app_secret, redirect_uri)
"""
import frappe
from frappe.model.document import Document
from frappe.utils import get_url


class MSuiteApp(Document):
    def validate(self):
        self._auto_generate_webhook_url()
        self._auto_generate_deauthorize_url()

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
