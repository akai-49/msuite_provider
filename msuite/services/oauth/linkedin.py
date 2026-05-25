"""
LinkedIn OAuth handler.

Auth flow: standard OAuth 2.0 Authorization Code.
Auth URL: https://www.linkedin.com/oauth/v2/authorization
Token exchange: https://www.linkedin.com/oauth/v2/accessToken
Token refresh: https://www.linkedin.com/oauth/v2/accessToken (grant_type=refresh_token)

Token lifetime: 60-day access, 365-day refresh.
Scopes: openid, profile, email, w_member_social, w_organization_social, r_organization_social

Discovery:
  /v2/userinfo          → person name, picture
  /rest/organizationAcls → organizations the user administers
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

LI_SCOPES = [
    "openid",
    "profile",
    "email",
    "w_member_social",
    "w_organization_social",
    "r_organization_social",
]

TOKEN_LIFETIME_SECONDS = 5_184_000  # 60 days


def build_auth_url(client_name: str, state: str) -> str:
    app = get_msuite_app("LinkedIn")
    params = {
        "response_type": "code",
        "client_id": app.app_id,
        "redirect_uri": app.redirect_uri,
        "state": state,
        "scope": " ".join(LI_SCOPES),
    }
    return f"{LI_AUTH_URL}?{urlencode(params)}"


def exchange_token(code: str, state_data: dict) -> dict:
    app = get_msuite_app("LinkedIn")
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
        logger.error(f"LinkedIn token exchange failed: {data}")
        frappe.throw("Failed to exchange LinkedIn authorization code", TokenExchangeError)

    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_in": data.get("expires_in", TOKEN_LIFETIME_SECONDS),
        "app_name": app.name,
    }


def discover_accounts(client_name: str, token_data: dict) -> list[dict]:
    token = token_data["access_token"]
    expires_in = token_data.get("expires_in", TOKEN_LIFETIME_SECONDS)
    app_name = token_data.get("app_name", "")
    refresh_token = token_data.get("refresh_token", "")
    connected = []

    user_info = _get_user_info(token)
    if not user_info:
        return connected

    person_sub = user_info.get("sub", "")
    person_name = user_info.get("name", "")
    person_picture = user_info.get("picture", "")

    auth_name = upsert_auth_account(client_name, "LinkedIn", person_sub, {
        "account_name": person_name,
        "profile_picture_url": person_picture,
    })

    ca_name = upsert_connected_account(client_name, Platform.LINKEDIN, person_sub, {
        "display_name": person_name,
        "auth_account": auth_name,
        "access_token": token,
        "token_type": "User Token",
        "msuite_app": app_name,
        "token_expiry": add_to_date(now(), seconds=expires_in),
    })

    push_account_to_client(client_name, Platform.LINKEDIN, {
        "author_urn": f"urn:li:person:{person_sub}",
        "display_name": person_name,
        "access_token": token,
        "refresh_token": refresh_token,
        "token_expires_at": str(add_to_date(now(), seconds=expires_in)),
    })
    connected.append({"platform": "LinkedIn", "name": person_name})

    orgs = _get_managed_organizations(token)
    for org in orgs:
        org_id = org.get("organizationalTarget", "").split(":")[-1]
        if not org_id:
            continue

        org_info = _get_org_info(token, org_id)
        org_name = org_info.get("localizedName", f"Org {org_id}")

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
        })
        connected.append({"platform": "LinkedIn", "name": org_name})

    return connected


def refresh_token(connected_account_name: str) -> None:
    """Refresh a LinkedIn access token using the refresh token."""
    ca = frappe.get_doc("MSuite Connected Account", connected_account_name)
    stored_refresh = ca.get_password("refresh_token") if hasattr(ca, "refresh_token") and ca.refresh_token else ""
    if not stored_refresh:
        return

    app = get_msuite_app("LinkedIn")
    resp = requests.post(
        LI_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": stored_refresh,
            "client_id": app.app_id,
            "client_secret": app.get_password("app_secret"),
        },
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        logger.warning(f"LinkedIn token refresh failed for {connected_account_name}: {data}")
        return

    ca.access_token = data["access_token"]
    ca.token_expiry = add_to_date(now(), seconds=data.get("expires_in", TOKEN_LIFETIME_SECONDS))
    ca.save(ignore_permissions=True)
    frappe.db.commit()
    logger.info(f"LinkedIn token refreshed for {connected_account_name}")


def _get_user_info(token: str) -> dict:
    try:
        resp = requests.get(
            f"{LI_API}/v2/userinfo",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        return resp.json()
    except Exception as e:
        logger.error(f"LinkedIn userinfo failed: {e}")
        return {}


def _get_managed_organizations(token: str) -> list[dict]:
    try:
        resp = requests.get(
            f"{LI_API}/v2/organizationAcls?q=roleAssignee&role=ADMINISTRATOR&projection=(elements*(organizationalTarget))",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        return resp.json().get("elements", [])
    except Exception as e:
        logger.error(f"LinkedIn org discovery failed: {e}")
        return []


def _get_org_info(token: str, org_id: str) -> dict:
    try:
        resp = requests.get(
            f"{LI_API}/v2/organizations/{org_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        return resp.json()
    except Exception as e:
        logger.error(f"LinkedIn org info failed for {org_id}: {e}")
        return {}
