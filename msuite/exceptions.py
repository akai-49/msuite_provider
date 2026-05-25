import frappe


class MSuiteError(frappe.ValidationError):
    """Base exception for all MSuite errors."""
    pass


class PlanNotFoundError(MSuiteError):
    pass


class GrantAlreadyExistsError(MSuiteError):
    pass


class BundleConfigurationError(MSuiteError):
    pass


class BundleNotFoundError(MSuiteError):
    pass


class PaymentVerificationError(MSuiteError):
    pass


class DuplicatePaymentError(MSuiteError):
    pass


class EntitlementError(MSuiteError):
    pass


class TrialConfigurationError(MSuiteError):
    pass


class SubscriptionAmendmentError(MSuiteError):
    pass


class CustomerGroupError(MSuiteError):
    pass


class InvalidGrantStateError(MSuiteError):
    pass


class CouponValidationError(MSuiteError):
    pass


class CouponAlreadyUsedError(MSuiteError):
    pass


class InvoiceError(MSuiteError):
    pass


class ClientConnectionError(MSuiteError):
    pass


class ClientActivationError(MSuiteError):
    pass


class OAuthError(MSuiteError):
    pass


class TokenExchangeError(OAuthError):
    pass


class AccountDiscoveryError(OAuthError):
    pass
