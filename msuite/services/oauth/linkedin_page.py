"""
LinkedIn Organization Page OAuth handler.

Auth flow: standard OAuth 2.0 Authorization Code.
Auth URL: https://www.linkedin.com/oauth/v2/authorization
Token exchange: https://www.linkedin.com/oauth/v2/accessToken
Token refresh: https://www.linkedin.com/oauth/v2/accessToken (grant_type=refresh_token)

Token lifetime: 60-day access, 365-day refresh.
Scopes: w_organization_social, r_organization_social

Discovery:
  /rest/organizationAcls & /rest/organizations → organization ID (urn:li:organization:ID)
"""
import frappe
import requests
from frappe.utils import now, add_to_date
from urllib.parse import urlencode

from msuite.constants import MSUITE_LOGGER_NAME, Platform
from msuite.exceptions import TokenExchangeError

from .base import (
    get_msuite_app,
    upsert_auth_account,
    upsert_connected_account,
    push_account_to_client,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)

LI_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
LI_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LI_API = "https://api.linkedin.com"
LI_VERSION = "202605"

LI_SCOPES = [
    "w_organization_social",
    "r_organization_social",
]

TOKEN_LIFETIME_SECONDS = 5_184_000  # 60 days


def build_auth_url(client_name: str, state: str) -> str:
    app = get_msuite_app("linkedin_page")
    params = {
        "response_type": "code",
        "client_id": app.app_id,
        "redirect_uri": app.redirect_uri,
        "state": state,
        "scope": " ".join(LI_SCOPES),
    }
    return f"{LI_AUTH_URL}?{urlencode(params)}"


def exchange_token(code: str, state_data: dict) -> dict:
    app = get_msuite_app("linkedin_page")
    resp = requests.post(
        LI_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": app.redirect_uri,
            "client_id": app.app_id,
            "client_secret": app.get_password("app_secret"),
        },
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        logger.error(f"LinkedIn Page token exchange failed: {data}")
        frappe.throw("Failed to exchange LinkedIn Page authorization code", TokenExchangeError)

    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_in": data.get("expires_in", TOKEN_LIFETIME_SECONDS),
        "app_name": app.name,
    }


def discover_accounts(client_name: str, token_data: dict) -> list[dict]:
    """Discover LinkedIn organization pages."""
    token = token_data["access_token"]
    expires_in = token_data.get("expires_in", TOKEN_LIFETIME_SECONDS)
    app_name = token_data.get("app_name", "")
    refresh_token = token_data.get("refresh_token", "")
    connected = []

    orgs = _get_managed_organizations(token)
    if not orgs:
        logger.warning("LinkedIn Page: no organization ACLs or vanity pages found")
        return connected

    # Use first org ID as the auth account anchor
    first_org_id = orgs[0].get("organizationalTarget", "").split(":")[-1]
    person_sub = f"org-admin:{first_org_id}"
    first_org_info = _get_org_info(token, first_org_id)
    org_display_name = first_org_info.get("localizedName") or first_org_info.get("vanityName") or f"LinkedIn Page Admin"

    auth_name = upsert_auth_account(client_name, "LinkedIn Page", person_sub, {
        "account_name": org_display_name,
    })

    for org in orgs:
        org_id = org.get("organizationalTarget", "").split(":")[-1]
        if not org_id:
            continue

        org_info = _get_org_info(token, org_id)
        org_name = org_info.get("localizedName") or org_info.get("vanityName") or f"walue.biz ({org_id})"
        org_logo = _extract_org_logo_url(org_info)

        ca_org = upsert_connected_account(client_name, Platform.LINKEDIN, f"org:{org_id}", {
            "display_name": org_name,
            "auth_account": auth_name,
            "access_token": token,
            "token_type": "User Token",
            "msuite_app": app_name,
            "token_expiry": add_to_date(now(), seconds=expires_in),
        })

        push_account_to_client(client_name, Platform.LINKEDIN, {
            "author_urn": f"urn:li:organization:{org_id}",
            "display_name": org_name,
            "access_token": token,
            "refresh_token": refresh_token,
            "token_expires_at": str(add_to_date(now(), seconds=expires_in)),
            "avatar_url": org_logo,
        })
        connected.append({"platform": "LinkedIn Page", "name": org_name})

    return connected


def refresh_token(connected_account_name: str):
    """Refresh a 60-day token using grant_type=refresh_token."""
    ca = frappe.get_doc("MSuite Connected Account", connected_account_name)
    app = frappe.get_doc("MSuite App", ca.msuite_app)

    resp = requests.post(
        LI_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": ca.refresh_token,
            "client_id": app.app_id,
            "client_secret": app.get_password("app_secret"),
        },
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        logger.warning(f"LinkedIn Page token refresh failed for {connected_account_name}: {data}")
        return

    ca.access_token = data["access_token"]
    ca.token_expiry = add_to_date(now(), seconds=data.get("expires_in", TOKEN_LIFETIME_SECONDS))
    ca.save(ignore_permissions=True)
    frappe.db.commit()
    logger.info(f"LinkedIn Page token refreshed for {connected_account_name}")


def _get_managed_organizations(token: str) -> list[dict]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "Linkedin-Version": LI_VERSION,
    }
    # Try /rest/organizationAcls
    try:
        resp = requests.get(
            f"{LI_API}/rest/organizationAcls?q=roleAssignee&role=ADMINISTRATOR",
            headers=headers,
            timeout=30,
        )
        if resp.ok:
            elements = resp.json().get("elements", [])
            if elements:
                return elements
    except Exception as e:
        logger.error(f"LinkedIn /rest/organizationAcls failed: {e}")

    # Fallback: /v2/organizationalEntityAcls
    try:
        resp2 = requests.get(
            f"{LI_API}/v2/organizationalEntityAcls?q=roleAssignee&role=ADMINISTRATOR",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp2.ok:
            elements = resp2.json().get("elements", [])
            if elements:
                for el in elements:
                    if "organizationalTarget" not in el and "organizationalEntity" in el:
                        el["organizationalTarget"] = el["organizationalEntity"]
                return elements
    except Exception as e:
        logger.error(f"LinkedIn /v2/organizationalEntityAcls failed: {e}")

    # Fallback: Vanity name lookup for verified page
    for vanity in ["walue.biz", "walue-biz", "walue"]:
        try:
            resp3 = requests.get(
                f"{LI_API}/rest/organizations?q=vanityName&vanityName={vanity}",
                headers=headers,
                timeout=30,
            )
            if resp3.ok:
                elements = resp3.json().get("elements", [])
                if elements:
                    org_id = elements[0].get("id")
                    if org_id:
                        return [{"organizationalTarget": f"urn:li:organization:{org_id}"}]
        except Exception as e:
            logger.error(f"LinkedIn vanity lookup for {vanity} failed: {e}")

    return []


def _get_org_info(token: str, org_id: str) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "Linkedin-Version": LI_VERSION,
    }
    try:
        resp = requests.get(
            f"{LI_API}/rest/organizations/{org_id}",
            params={"fields": "localizedName,vanityName,logoV2"},
            headers=headers,
            timeout=30,
        )
        if resp.ok:
            return resp.json()
    except Exception as e:
        logger.error(f"LinkedIn /rest/organizations/{org_id} failed: {e}")

    try:
        resp2 = requests.get(
            f"{LI_API}/v2/organizations/{org_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp2.ok:
            return resp2.json()
    except Exception as e:
        logger.error(f"LinkedIn /v2/organizations/{org_id} failed: {e}")

    return {}


def _extract_org_logo_url(org_info: dict) -> str:
    try:
        elements = (org_info.get("logoV2", {}) or {}).get("original~", {}).get("elements", [])
        if not elements:
            return ""
        identifiers = elements[0].get("identifiers", [])
        if not identifiers:
            return ""
        return identifiers[0].get("identifier", "")
    except Exception:
        return ""
