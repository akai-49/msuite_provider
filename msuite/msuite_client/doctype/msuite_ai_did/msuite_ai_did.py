"""
MSuite AI DID — registry mapping a telephony DID (phone number) to the
MSuite Client site that owns it.

Populated automatically: client sites call
``msuite.api.v1.ai_calling.register_did`` whenever a Voice Inbound Config
or AI Calling Organization Config is saved. The AI Calling FastAPI backend
then routes every callback through the provider, which resolves the target
client site from this registry — no per-client URL configuration anywhere.
"""
import frappe
from frappe.model.document import Document


class MSuiteAIDID(Document):

    def validate(self):
        if self.did:
            self.did = self.did.strip()
