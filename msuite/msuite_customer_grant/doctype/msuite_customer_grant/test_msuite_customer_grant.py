import frappe
from frappe.tests.utils import FrappeTestCase


class TestMSuiteCustomerGrant(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
