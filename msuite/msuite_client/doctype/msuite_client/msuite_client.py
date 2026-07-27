"""
MSuite Client controller.

Represents a client Frappe instance connected to this provider.
Linked 1-to-1 with an ERPNext Customer (billing entity).

Lifecycle: Draft → Test Connection → Activate → (Active) → Suspend → Reactivate
"""
import re
import secrets

import frappe
import requests
from frappe.model.document import Document
from frappe.utils import now

from msuite.constants import (
    ClientStatus,
    ConnectedAccountStatus,
    SyncStatus,
    GRAPH_API_BASE,
    MSUITE_LOGGER_NAME,
)
from msuite.exceptions import ClientActivationError
from msuite.utils.validators import require_system_manager_or_msuite_manager

logger = frappe.logger(MSUITE_LOGGER_NAME)


class MSuiteClient(Document):
    def before_insert(self):
        """Auto-generate client_code on first save."""
        if not self.client_code:
            self.client_code = _generate_client_code(self.client_name)

    def validate(self):
        self._validate_client_url()

    def _validate_client_url(self):
        """Ensure URL is well-formed and strip trailing slash."""
        url = (self.client_url or "").strip().rstrip("/")
        if not url:
            frappe.throw("Client URL is required", frappe.ValidationError)
        if not url.startswith("http://") and not url.startswith("https://"):
            frappe.throw(
                "Client URL must start with http:// or https://",
                frappe.ValidationError,
            )
        self.client_url = url


def _generate_client_code(client_name: str) -> str:
    """Generate a short unique code: slugified-name-random6."""
    slug = re.sub(r"[^a-z0-9]+", "-", client_name.lower()).strip("-")[:20]
    random_part = secrets.token_hex(3)  # 6 hex chars
    return f"{slug}-{random_part}"


# ======================================================================
# Whitelisted methods (called from JS buttons)
# ======================================================================


@frappe.whitelist()
def test_connection(client_name: str) -> dict:
    """
    Test connectivity to a client instance.
    Calls the client's health_check endpoint.

    Args:
        client_name: MSuite Client document name

    Returns:
        dict with status, message, app_version
    """
    require_system_manager_or_msuite_manager()
    doc = frappe.get_doc("MSuite Client", client_name)
    from msuite.services.client_service import test_client_connection
    result = test_client_connection(doc.client_url)
    return result


@frappe.whitelist()
def activate_client(client_name: str) -> dict:
    """
    Activate a client instance.

    Generates fresh api_key + api_secret, builds plan data from customer's
    entitlements, pushes everything to the client, and sets status=Active.

    Works on any non-Active status (Draft, Suspended, Disconnected). Useful
    when a client instance has been reinstalled and needs fresh credentials.
    For active clients, use push_plan / suspend / reactivate instead.

    Args:
        client_name: MSuite Client document name

    Returns:
        Success dict
    """
    require_system_manager_or_msuite_manager()
    doc = frappe.get_doc("MSuite Client", client_name)

    if doc.status == ClientStatus.ACTIVE:
        frappe.throw(
            "Client is already active. Use Suspend or Push Plan instead.",
            ClientActivationError,
        )

    from msuite.services.client_service import (
        generate_credentials,
        build_client_plan_data,
        activate_client_instance,
    )

    # Generate credentials
    api_key, api_secret = generate_credentials()

    # Build plan data from customer's entitlements
    plan_data = build_client_plan_data(doc.customer)

    # Push activation to client
    result = activate_client_instance(doc, api_key, api_secret, plan_data)

    # Success — save credentials and update status
    doc.api_key = api_key
    doc.api_secret = api_secret
    doc.status = ClientStatus.ACTIVE
    doc.activated_on = now()
    doc.last_sync = now()
    doc.last_sync_status = SyncStatus.SUCCESS
    doc.sync_fail_count = 0
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    logger.info(f"Activated client {client_name} at {doc.client_url}")
    return {
        "status": "success",
        "message": f"Client '{client_name}' activated successfully",
    }


@frappe.whitelist()
def push_plan(client_name: str) -> dict:
    """
    Push current plan/entitlement data to an active client.

    Args:
        client_name: MSuite Client document name

    Returns:
        Success dict
    """
    require_system_manager_or_msuite_manager()
    doc = frappe.get_doc("MSuite Client", client_name)

    if doc.status != ClientStatus.ACTIVE:
        frappe.throw(
            f"Cannot push plan: client status is {doc.status}",
            frappe.ValidationError,
        )

    from msuite.services.client_service import build_client_plan_data, push_plan_to_client

    plan_data = build_client_plan_data(doc.customer)
    push_plan_to_client(doc, plan_data)

    doc.last_sync = now()
    doc.last_sync_status = SyncStatus.SUCCESS
    doc.sync_fail_count = 0
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "status": "success",
        "message": f"Plan pushed to '{client_name}'",
    }


@frappe.whitelist()
def push_credentials(client_name: str) -> dict:
    """
    Re-push all connected account credentials to a client.

    Iterates all active Connected Accounts for this client and pushes
    their credentials (token + metadata) to the client instance. Useful
    when the client was reinstalled, credentials were missing during
    initial signup, or after token rotation.

    Each platform has a builder that reconstructs the push payload from
    stored Provider-side records — the same shape the OAuth discovery
    created on first connect.
    """
    require_system_manager_or_msuite_manager()
    doc = frappe.get_doc("MSuite Client", client_name)

    if doc.status != ClientStatus.ACTIVE:
        frappe.throw("Client must be active to push credentials.", frappe.ValidationError)

    from msuite.services.oauth.base import push_account_to_client

    accounts = frappe.get_all(
        "MSuite Connected Account",
        filters={"client": client_name, "status": ConnectedAccountStatus.ACTIVE},
        fields=["name", "platform", "account_id", "msuite_app"],
    )

    pushed = 0
    for account in accounts:
        builder = _PUSH_PAYLOAD_BUILDERS.get(account.platform)
        if not builder:
            continue

        ca = frappe.get_doc("MSuite Connected Account", account.name)
        token = ca.get_password("access_token") if ca.access_token else ""
        payload = builder(ca, token)
        if not payload:
            continue

        push_account_to_client(client_name, account.platform, payload)
        pushed += 1

    doc.last_sync = now()
    doc.last_sync_status = SyncStatus.SUCCESS
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "status": "success",
        "message": f"Pushed credentials for {pushed} account(s) to '{client_name}'",
    }


# ── Push payload builders (one per platform) ──────────────────────────────
#
# Each builder receives a Connected Account doc + decrypted token, and
# returns the dict shape the Client expects. Returns None to skip.


def _build_whatsapp_payload(ca, token: str) -> dict | None:
    wa = frappe.db.get_value(
        "MSuite WhatsApp Account",
        {"connected_account": ca.name},
        ["name", "waba_id", "waba_name", "waba_currency", "waba_timezone"],
        as_dict=True,
    )
    if not wa:
        return None

    phones = frappe.get_all(
        "MSuite WhatsApp Phone",
        filters={"parent": wa.name},
        fields=["phone_number_id", "display_phone_number", "verified_name",
                 "quality_rating", "messaging_limit_tier", "webhook_verify_token"],
    )

    app_id = ""
    if ca.msuite_app:
        app_id = frappe.db.get_value("MSuite App", ca.msuite_app, "app_id") or ""

    biz_id, biz_name = _get_business_info(ca)

    return {
        "waba_id": wa.waba_id,
        "waba_name": wa.waba_name or "",
        "waba_currency": wa.waba_currency or "",
        "waba_timezone": wa.waba_timezone or "",
        "business_id": biz_id,
        "business_name": biz_name,
        "app_id": app_id,
        "system_user_token": token,
        "phones": [dict(p) for p in phones],
    }


def _build_facebook_payload(ca, token: str) -> dict | None:
    fb = frappe.db.get_value(
        "MSuite Facebook Account",
        {"connected_account": ca.name},
        ["page_id", "page_name"],
        as_dict=True,
    )
    if not fb:
        return None

    biz_id, biz_name = _get_business_info(ca)

    return {
        "page_id": fb.page_id,
        "page_name": fb.page_name or "",
        "page_access_token": token,
        "business_id": biz_id,
        "business_name": biz_name,
    }


def _build_instagram_payload(ca, token: str) -> dict | None:
    ig = frappe.db.get_value(
        "MSuite Instagram Account",
        {"connected_account": ca.name},
        ["instagram_user_id", "username", "linked_page_id", "linked_page_name"],
        as_dict=True,
    )
    if not ig:
        return None

    biz_id, _ = _get_business_info(ca)

    return {
        "user_id": ig.instagram_user_id,
        "username": ig.username or "",
        "access_token": token,
        "linked_page_id": ig.linked_page_id or "",
        "linked_page_name": ig.linked_page_name or "",
        "business_id": biz_id,
    }


def _build_meta_ads_payload(ca, token: str) -> dict | None:
    ads = frappe.db.get_value(
        "MSuite Meta Ads Account",
        {"connected_account": ca.name},
        ["ad_account_id", "ad_account_name", "currency", "timezone"],
        as_dict=True,
    )
    if not ads:
        return None

    biz_id, biz_name = _get_business_info(ca)

    return {
        "ad_account_id": ads.ad_account_id,
        "ad_account_name": ads.ad_account_name or "",
        "access_token": token,
        "currency": ads.currency or "",
        "timezone": ads.timezone or "",
        "business_id": biz_id,
        "business_name": biz_name,
    }


def _build_gmail_payload(ca, token: str) -> dict | None:
    """Mirror of the Gmail credential push in oauth/google.py — the daily
    token-refresh cron uses this to keep the client's Email Account token
    fresh. refresh_token / app_secret are NEVER included."""
    if not ca.account_id:
        return None
    return {
        "gmail_address": ca.account_id,
        "google_account_name": ca.display_name or ca.account_id,
        "access_token": token,
        "token_expires_at": str(ca.token_expiry or ""),
    }


def _build_youtube_payload(ca, token: str) -> dict | None:
    """Mirror of the YouTube credential push in oauth/google.py — keeps the
    client's Social Account token fresh after a cron/on-demand refresh."""
    if not ca.account_id:
        return None
    return {
        "channel_id": ca.account_id,
        "channel_title": ca.display_name or ca.account_id,
        "access_token": token,
        "token_expires_at": str(ca.token_expiry or ""),
    }


def _get_business_info(ca) -> tuple[str, str]:
    """Extract business ID + name from the Connected Account's Auth Account."""
    if not ca.auth_account:
        return "", ""
    auth = frappe.db.get_value(
        "MSuite Auth Account", ca.auth_account,
        ["account_id", "account_name"], as_dict=True,
    )
    if not auth:
        return "", ""
    return auth.account_id or "", auth.account_name or ""


_PUSH_PAYLOAD_BUILDERS = {
    "WhatsApp":  _build_whatsapp_payload,
    "Facebook":  _build_facebook_payload,
    "Instagram": _build_instagram_payload,
    "Meta Ads":  _build_meta_ads_payload,
    "Gmail":     _build_gmail_payload,
    "YouTube":   _build_youtube_payload,
}


@frappe.whitelist()
def suspend_client(client_name: str) -> dict:
    """
    Suspend a client. Tells the client to deactivate.

    Args:
        client_name: MSuite Client document name

    Returns:
        Success dict
    """
    require_system_manager_or_msuite_manager()
    doc = frappe.get_doc("MSuite Client", client_name)

    if doc.status != ClientStatus.ACTIVE:
        frappe.throw("Client is not active", frappe.ValidationError)

    from msuite.services.client_service import suspend_client_instance

    try:
        suspend_client_instance(doc)
    except Exception:
        logger.warning(f"Could not notify client {client_name} of suspension")

    doc.status = ClientStatus.SUSPENDED
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "status": "success",
        "message": f"Client '{client_name}' suspended",
    }


@frappe.whitelist()
def reactivate_client(client_name: str) -> dict:
    """
    Reactivate a suspended/disconnected client by re-pushing plan data.

    If the client instance was reinstalled (has no stored credentials),
    the push will fail with "Workspace not activated". In that case we
    return a special status so the caller can prompt the user to activate
    fresh (regenerate credentials), instead of silently reactivating.

    Args:
        client_name: MSuite Client document name

    Returns:
        On success:  {"status": "success", "message": ...}
        On fresh instance: {"status": "needs_fresh_activation", "message": ...}
    """
    require_system_manager_or_msuite_manager()
    doc = frappe.get_doc("MSuite Client", client_name)

    if doc.status not in (ClientStatus.SUSPENDED, ClientStatus.DISCONNECTED):
        frappe.throw(
            f"Cannot reactivate: status is {doc.status}",
            frappe.ValidationError,
        )

    from msuite.services.client_service import build_client_plan_data, push_plan_to_client

    plan_data = build_client_plan_data(doc.customer)

    try:
        push_plan_to_client(doc, plan_data)
    except Exception as e:
        # Detect the specific "Workspace not activated" error from a fresh client.
        # This happens when the client instance was reinstalled and has no credentials.
        if _is_fresh_instance_error(e):
            return {
                "status": "needs_fresh_activation",
                "message": (
                    f"Client '{client_name}' appears to be a fresh instance. "
                    f"Credentials are missing on the client side."
                ),
            }
        # Any other failure — re-raise so the real error surfaces
        raise

    doc.status = ClientStatus.ACTIVE
    doc.last_sync = now()
    doc.last_sync_status = SyncStatus.SUCCESS
    doc.sync_fail_count = 0
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "status": "success",
        "message": f"Client '{client_name}' reactivated",
    }


def _is_fresh_instance_error(exc: Exception) -> bool:
    """
    Detect the 'Workspace not activated' error from a fresh client instance.

    The client raises this AuthenticationError when validate_provider_auth
    is called but MSuite Settings.api_key is empty (fresh install).
    """
    msg = str(exc).lower()
    return "workspace not activated" in msg or "unauthorized" in msg and "not activated" in msg


@frappe.whitelist()
def revoke_whatsapp_access(connected_account_name: str) -> dict:
    """
    Revoke WhatsApp access for a connected account.

    Calls Meta's oauth/revoke endpoint to permanently invalidate the system
    user token, then marks the Connected Account as Revoked.

    Args:
        connected_account_name: MSuite Connected Account document name

    Returns:
        Success dict
    """
    require_system_manager_or_msuite_manager()
    ca = frappe.get_doc("MSuite Connected Account", connected_account_name)

    if ca.status == ConnectedAccountStatus.REVOKED:
        frappe.throw("Access is already revoked.")

    # Get the app secret for the token revocation call
    app = frappe.get_doc("MSuite App", ca.msuite_app) if ca.msuite_app else None
    token = ca.get_password("access_token") if ca.access_token else None

    if token and app:
        app_id = app.app_id
        app_secret = app.get_password("app_secret")

        try:
            resp = requests.get(
                f"{GRAPH_API_BASE}/oauth/revoke",
                params={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "revoke_token": token,
                    "access_token": f"{app_id}|{app_secret}",
                },
                timeout=30,
            )
            result = resp.json()
            if not result.get("success"):
                logger.warning(f"Meta token revocation response: {result}")
        except Exception as e:
            logger.error(f"Failed to call Meta oauth/revoke: {e}")

    # Mark Connected Account as revoked
    ca.status = ConnectedAccountStatus.REVOKED
    ca.save(ignore_permissions=True)

    # Create consent record for the revocation (audit trail)
    frappe.get_doc({
        "doctype": "MSuite Signup Consent",
        "client": ca.client,
        "connected_account": ca.name,
        "platform": ca.platform,
        "consent_status": "Revoked",
        "event_type": "FINISH",
        "waba_id": ca.account_id,
        "consented_at": ca.connected_at,
        "revoked_at": now(),
    }).insert(ignore_permissions=True)

    # Push revocation to client
    try:
        from msuite.services.oauth.base import push_account_to_client
        push_account_to_client(ca.client, ca.platform, {
            "waba_id": ca.account_id,
            "revoked": True,
        })
    except Exception as e:
        logger.warning(f"Failed to push revocation to client: {e}")

    frappe.db.commit()
    logger.info(f"Revoked WhatsApp access for {connected_account_name}")

    return {
        "status": "success",
        "message": f"WhatsApp access revoked for {ca.display_name or ca.account_id}",
    }
