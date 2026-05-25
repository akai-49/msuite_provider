import frappe
from frappe.tests.utils import FrappeTestCase

from msuite.exceptions import PaymentVerificationError


class TestPaymentFlow(FrappeTestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_verify_hmac_returns_false_for_invalid(self):
        """Invalid HMAC should return False."""
        from msuite.services.payment_service import verify_hmac_signature

        result = verify_hmac_signature("razorpay", b"test body", "invalid_signature")
        self.assertFalse(result)

    def test_get_payment_status_for_nonexistent_invoice(self):
        """Should return error for non-existent invoice."""
        from msuite.services.payment_service import get_payment_status

        result = get_payment_status("NONEXISTENT-SINV-001")
        self.assertIn("error", result)

    def test_extract_gateway_reference_razorpay(self):
        """Should correctly extract Razorpay payment ID."""
        from msuite.services.payment_service import _extract_gateway_reference

        payload = {
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_test123"
                    }
                }
            }
        }
        ref = _extract_gateway_reference("razorpay", payload)
        self.assertEqual(ref, "pay_test123")

    def test_extract_gateway_reference_stripe(self):
        """Should correctly extract Stripe payment ID."""
        from msuite.services.payment_service import _extract_gateway_reference

        payload = {
            "data": {
                "object": {
                    "id": "pi_test456"
                }
            }
        }
        ref = _extract_gateway_reference("stripe", payload)
        self.assertEqual(ref, "pi_test456")
