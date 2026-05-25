import frappe
from frappe.tests.utils import FrappeTestCase


class TestMSuiteCouponUsage(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
