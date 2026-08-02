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
LI_VERSION = "202405"

LI_SCOPES = [
    "w_organization_social",
    "r_organization_social",
    "rw_organization_admin",
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

    auth_name = upsert_auth_account(client_name, "LinkedIn", person_sub, {
        "account_name": org_display_name,
    })

    for org in orgs:
        org_id = org.get("organizationalTarget", "").split(":")[-1]
        if not org_id:
            continue

        org_info = _get_org_info(token, org_id)
        org_name = org_info.get("localizedName") or org_info.get("vanityName") or f"walue.biz ({org_id})"
        org_logo = _extract_org_logo_url(org_info)

        ca_org = upsert_connected_account(client_name, Platform.LINKEDIN_PAGE, f"org:{org_id}", {
            "display_name": org_name,
            "auth_account": auth_name,
            "access_token": token,
            "token_type": "User Token",
            "msuite_app": app_name,
            "token_expiry": add_to_date(now(), seconds=expires_in),
        })

        push_account_to_client(client_name, Platform.LINKEDIN_PAGE, {
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
    }
    simple_headers = {"Authorization": f"Bearer {token}"}

    person_urn = ""
    try:
        u_resp = requests.get(f"{LI_API}/v2/userinfo", headers=simple_headers, timeout=10)
        if u_resp.ok:
            sub = u_resp.json().get("sub")
            if sub:
                person_urn = f"urn:li:person:{sub}"
    except Exception as e:
        logger.debug(f"Userinfo fetch failed: {e}")

    if not person_urn:
        try:
            m_resp = requests.get(f"{LI_API}/v2/me", headers=simple_headers, timeout=10)
            if m_resp.ok:
                pid = m_resp.json().get("id")
                if pid:
                    person_urn = f"urn:li:person:{pid}"
        except Exception as e:
            logger.debug(f"v2/me fetch failed: {e}")

    logger.info(f"[LinkedIn Page Discovery] Resolved person_urn: {person_urn}")

    urls = [
        f"{LI_API}/rest/organizationAcls?q=roleAssignee",
        f"{LI_API}/v2/organizationalEntityAcls?q=roleAssignee",
    ]
    if person_urn:
        urls.insert(0, f"{LI_API}/rest/organizationAcls?q=roleAssignee&roleAssignee={person_urn}")
        urls.insert(1, f"{LI_API}/v2/organizationalEntityAcls?q=roleAssignee&roleAssignee={person_urn}")

    for url in urls:
        h = headers if "/rest/" in url else simple_headers
        try:
            resp = requests.get(url, headers=h, timeout=20)
            if resp.ok:
                elements = resp.json().get("elements", [])
                if elements:
                    normalized = []
                    for el in elements:
                        target = (
                            el.get("organizationalTarget")
                            or el.get("organizationalEntity")
                            or el.get("organization")
                            or ""
                        )
                        if target:
                            el["organizationalTarget"] = target
                            normalized.append(el)
                    if normalized:
                        logger.info(f"LinkedIn ACL found {len(normalized)} organization(s) via {url}")
                        return normalized
            else:
                logger.warning(f"LinkedIn ACL {url} status={resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"LinkedIn ACL lookup {url} failed: {e}")

    # Fallback: Vanity name lookup for verified page
    for vanity in ["walue.biz", "walue-biz", "walue"]:
        try:
            resp3 = requests.get(
                f"{LI_API}/rest/organizations?q=vanityName&vanityName={vanity}",
                headers=headers,
                timeout=20,
            )
            if resp3.ok:
                elements = resp3.json().get("elements", [])
                if elements:
                    org_id = elements[0].get("id")
                    if org_id:
                        logger.info(f"LinkedIn page found via vanity name {vanity}: org_id={org_id}")
                        return [{"organizationalTarget": f"urn:li:organization:{org_id}"}]
            else:
                logger.warning(f"LinkedIn vanity lookup {vanity} status={resp3.status_code}: {resp3.text[:200]}")
        except Exception as e:
            logger.error(f"LinkedIn vanity lookup for {vanity} failed: {e}")

    return []


def _get_org_info(token: str, org_id: str) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
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
