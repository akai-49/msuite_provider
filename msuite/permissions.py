import frappe


def has_permission(doc, ptype, user):
    """Permission check for MSuite Customer Grant."""
    if frappe.session.user == "Administrator":
        return True
    if "MSuite Manager" in frappe.get_roles(user):
        return True
    if "System Manager" in frappe.get_roles(user):
        return True
    return False
