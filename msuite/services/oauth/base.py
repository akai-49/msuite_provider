"""
Shared helpers for all OAuth platform handlers.

Provides:
  - Auth account upsert (top-level entity: Meta Business, Google Account, etc.)
  - Connected account upsert (per-platform account with token)
  - Platform-specific detail upsert (WhatsApp, Facebook, Instagram, Ads)
  - Credential push to client
  - MSuite App lookup
"""
import frappe
from frappe.utils import now

from msuite.constants import ConnectedAccountStatus, MSUITE_LOGGER_NAME
from msuite.exceptions import OAuthError

logger = frappe.logger(MSUITE_LOGGER_NAME)


# ── MSuite App lookup ────────────────────────────────────────────────────


def get_msuite_app(platform: str):
    """
    Fetch the active MSuite App record for a platform.

    Normalizes platform key (e.g., "linkedin_page" -> "LinkedIn Page") and matches
    against document name, app_name, or platform field.
    """
    if platform.lower() == "linkedin":
        platform = "LinkedIn Page"

    terms = [
        platform,
        platform.replace("_", " "),
        platform.replace("_", " ").title(),
        platform.lower(),
    ]
    for term in terms:
        app_name = (
            frappe.db.get_value("MSuite App", {"name": term, "is_active": 1}, "name")
            or frappe.db.get_value("MSuite App", {"app_name": term, "is_active": 1}, "name")
            or frappe.db.get_value("MSuite App", {"platform": term, "is_active": 1}, "name")
        )
        if app_name:
            return frappe.get_doc("MSuite App", app_name)

    frappe.throw(
        f"No active MSuite App configured for {platform}. "
        f"Create one in MSuite App.",
        OAuthError,
    )


# ── Auth Account (top-level entity) ─────────────────────────────────────


def upsert_auth_account(
    client_name: str,
    platform: str,
    account_id: str,
    data: dict,
) -> str:
    """
    Create or update an MSuite Auth Account.

    This is the top-level entity: Meta Business Account, Google Account, etc.
    All Connected Accounts link to this as their parent.

    Dedup by (client, platform, account_id).

    Args:
        client_name: MSuite Client document name
        platform: Parent company (Meta, Google, TikTok, LinkedIn, Twitter)
        account_id: Platform-specific ID (Meta Business ID, Google Account ID)
        data: Dict with account_name, verification_status, profile_picture_url

    Returns:
        MSuite Auth Account document name
    """
    if platform and platform.startswith("LinkedIn"):
        platform = "LinkedIn"

    existing = frappe.db.get_value(
        "MSuite Auth Account",
        {"client": client_name, "platform": platform, "account_id": account_id},
        "name",
    )

    if existing:
        doc = frappe.get_doc("MSuite Auth Account", existing)
        for key, value in data.items():
            if value is not None:
                setattr(doc, key, value)
        doc.save(ignore_permissions=True)
        logger.info(f"Updated {platform} auth account {account_id} for {client_name}")
        return doc.name

    doc = frappe.new_doc("MSuite Auth Account")
    doc.client = client_name
    doc.platform = platform
    doc.account_id = account_id
    doc.connected_at = now()
    for key, value in data.items():
        if value is not None:
            setattr(doc, key, value)
    doc.insert(ignore_permissions=True)
    logger.info(f"Created {platform} auth account {account_id} for {client_name}")
    return doc.name


# ── Connected Account (per-platform, holds token) ───────────────────────


def upsert_connected_account(
    client_name: str,
    platform: str,
    account_id: str,
    data: dict,
) -> str:
    """
    Create or update an MSuite Connected Account.

    Dedup by (client, platform, account_id).

    Args:
        client_name: MSuite Client document name
        platform: Product/service (WhatsApp, Facebook, Instagram, etc.)
        account_id: Platform-specific account ID
        data: Dict with fields: display_name, auth_account (Link),
              msuite_app, access_token, token_type, token_expiry

    Returns:
        MSuite Connected Account document name
    """
    existing = frappe.db.get_value(
        "MSuite Connected Account",
        {"client": client_name, "platform": platform, "account_id": account_id},
        "name",
    )

    # Extract token separately — don't mutate the caller's dict
    access_token = data.get("access_token")
    data = {k: v for k, v in data.items() if k != "access_token"}

    if existing:
        doc = frappe.get_doc("MSuite Connected Account", existing)
        for key, value in data.items():
            if value is not None:
                setattr(doc, key, value)
        doc.last_refreshed = now()
        if access_token:
            doc.access_token = access_token
            # Re-authorising revives the account. Only the OAuth
            # discover_accounts flows reach this function, and they run
            # solely because the customer just completed the consent
            # dialog for this asset — so a fresh token means it is live
            # again.
            #
            # Without this, `status` was set on INSERT only: an account
            # that had been disconnected (or revoked) stayed
            # `Disconnected` forever after reconnecting. Everything
            # downstream looked healthy — token valid, Page subscribed —
            # but webhook routing filters on `status == "Active"`, so
            # every inbound event for that asset was silently dropped.
            # That is exactly how Facebook/Instagram DMs went missing
            # while WhatsApp (never disconnected) kept working.
            doc.status = ConnectedAccountStatus.ACTIVE
            doc.needs_reauth = 0
            if not doc.connected_at:
                doc.connected_at = now()
        doc.save(ignore_permissions=True)
        logger.info(f"Updated {platform} account {account_id} for {client_name}")
        return doc.name

    doc = frappe.new_doc("MSuite Connected Account")
    doc.client = client_name
    doc.platform = platform
    doc.account_id = account_id
    doc.status = ConnectedAccountStatus.ACTIVE
    doc.connected_at = now()
    for key, value in data.items():
        if value is not None:
            setattr(doc, key, value)
    if access_token:
        doc.access_token = access_token
    doc.insert(ignore_permissions=True)
    logger.info(f"Created {platform} account {account_id} for {client_name}")
    return doc.name


# ── Platform-specific detail upsert ──────────────────────────────────────


def upsert_whatsapp_account(
    connected_account_name: str,
    waba_id: str,
    waba_name: str,
    waba_currency: str,
    waba_timezone: str,
    phones: list[dict],
) -> str:
    """Create or update MSuite WhatsApp Account (linked to Connected Account)."""
    existing = frappe.db.get_value(
        "MSuite WhatsApp Account",
        {"connected_account": connected_account_name},
        "name",
    )

    if existing:
        doc = frappe.get_doc("MSuite WhatsApp Account", existing)
        doc.waba_id = waba_id
        doc.waba_name = waba_name
        doc.waba_currency = waba_currency
        doc.waba_timezone = waba_timezone
        _sync_phones(doc, phones)
        doc.save(ignore_permissions=True)
        return doc.name

    doc = frappe.new_doc("MSuite WhatsApp Account")
    doc.connected_account = connected_account_name
    doc.waba_id = waba_id
    doc.waba_name = waba_name
    doc.waba_currency = waba_currency
    doc.waba_timezone = waba_timezone
    for phone in phones:
        doc.append("phones", phone)
    doc.insert(ignore_permissions=True)
    return doc.name


def _sync_phones(doc, phones: list[dict]):
    """Sync phone numbers — upsert by phone_number_id."""
    existing_ids = {r.phone_number_id: r for r in doc.phones}
    for phone in phones:
        pid = phone.get("phone_number_id")
        if pid in existing_ids:
            row = existing_ids[pid]
            for key, value in phone.items():
                setattr(row, key, value)
        else:
            doc.append("phones", phone)


def upsert_facebook_account(connected_account_name: str, data: dict) -> str:
    """Create or update MSuite Facebook Account."""
    return _upsert_platform_detail("MSuite Facebook Account", connected_account_name, data)


def upsert_instagram_account(connected_account_name: str, data: dict) -> str:
    """Create or update MSuite Instagram Account."""
    return _upsert_platform_detail("MSuite Instagram Account", connected_account_name, data)


def upsert_meta_ads_account(connected_account_name: str, data: dict) -> str:
    """Create or update MSuite Meta Ads Account."""
    return _upsert_platform_detail("MSuite Meta Ads Account", connected_account_name, data)


def _upsert_platform_detail(doctype: str, connected_account_name: str, data: dict) -> str:
    """Generic upsert for any platform-specific detail doctype."""
    existing = frappe.db.get_value(
        doctype, {"connected_account": connected_account_name}, "name"
    )

    if existing:
        doc = frappe.get_doc(doctype, existing)
        for key, value in data.items():
            if value is not None:
                setattr(doc, key, value)
        doc.save(ignore_permissions=True)
        return doc.name

    doc = frappe.new_doc(doctype)
    doc.connected_account = connected_account_name
    for key, value in data.items():
        if value is not None:
            setattr(doc, key, value)
    doc.insert(ignore_permissions=True)
    return doc.name


# ── Push to client ───────────────────────────────────────────────────────


def push_account_to_client(
    client_name: str,
    platform: str,
    push_data: dict,
) -> None:
    """
    Push connected account data to a client instance.

    The push_data should contain all metadata plus the decrypted access_token.
    App secrets and refresh tokens must NEVER be included.

    Args:
        client_name: MSuite Client document name
        platform: Platform name
        push_data: Complete data dict (metadata + token)
    """
    try:
        client_doc = frappe.get_doc("MSuite Client", client_name)
        if client_doc.status != "Active":
            return

        from msuite.services.client_service import push_credentials_to_client
        push_credentials_to_client(client_doc, platform, push_data)
    except Exception as e:
        logger.error(f"Failed to push {platform} credentials to {client_name}: {e}")
