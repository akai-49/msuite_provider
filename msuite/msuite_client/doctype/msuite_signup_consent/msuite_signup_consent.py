"""
MSuite Signup Consent — immutable audit record of each signup event.

One record per signup attempt (FINISH, CANCEL, ERROR, etc.).
Stores permissions granted, client environment, and raw API responses.
Never pushed to client — provider-only compliance data.
"""
import frappe
from frappe.model.document import Document


class MSuiteSignupConsent(Document):
    pass
