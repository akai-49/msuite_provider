"""
LinkedIn OAuth handler — one app, one platform, two entity types.

Auth flow: standard OAuth 2.0 Authorization Code.
Auth URL: https://www.linkedin.com/oauth/v2/authorization
Token exchange: https://www.linkedin.com/oauth/v2/accessToken
Token refresh: https://www.linkedin.com/oauth/v2/accessToken (grant_type=refresh_token)

Token lifetime: 60-day access, 365-day refresh.

A single MSuite App (platform="LinkedIn") backs both entity types. The one
authorization requests the union of member and organization scopes, so the
resulting token can act as the person AND as any org they administer.

`mode` (threaded from the OAuth state) decides what happens after the token
lands:

  "profile" → resolve /v2/userinfo, create + push one account immediately.
  "page"    → enumerate admin-managed orgs and return them as *candidates*
              WITHOUT creating anything. The user picks which pages to
              connect in the client UI; `finalize_pages()` then creates and
              pushes only the selected ones.

Organization scopes (rw_organization_admin, w_organization_social) require
LinkedIn's Community Management API product. When that approval is missing
the ACL lookup 403s — page discovery degrades to an empty candidate list
rather than failing the whole connect, so the profile branch still works.
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
LI_VERSION = "202605"  # matches the client app's platform_capabilities.API_VERSION

LI_SCOPES = [
    "openid",
    "profile",
    "email",
    "w_member_social",
    "r_organization_social",
    "w_organization_social",
    "r_organization_admin",
    "rw_organization_admin",
    "r_ads",
    "rw_ads",
    "r_ads_reporting",
    "r_basicprofile",
    "r_1st_connections_size",
    "r_marketing_leadgen_automation",
    "r_ads_leadgen_automation",
    "rw_events",
    "r_events",
]

TOKEN_LIFETIME_SECONDS = 5_184_000  # 60 days

MODE_PROFILE = "profile"
MODE_PAGE = "page"
MODE_ADS = "ads"


def _mode_from_state(state_data: dict) -> str:
    """Map the requested platform key to an entity mode.

    The registry keeps four keys (`linkedin`, `linkedin_profile`,
    `linkedin_page`, `linkedin_ads`) purely to carry this intent — they all resolve to this
    module and read the same MSuite App.
    """
    platform = state_data.get("platform")
    if platform == "linkedin_page":
        return MODE_PAGE
    if platform in ("linkedin_ads", "ads"):
        return MODE_ADS
    return MODE_PROFILE


def _rest_headers(token: str) -> dict:
    """Headers for /rest/ endpoints, which reject calls without a version."""
    return {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": LI_VERSION,
    }


# ── OAuth handshake ──────────────────────────────────────────────────────


def build_auth_url(client_name: str, state: str) -> str:
    app = get_msuite_app("linkedin")
    params = {
        "response_type": "code",
        "client_id": app.app_id,
        "redirect_uri": app.redirect_uri,
        "state": state,
        "scope": " ".join(LI_SCOPES),
    }
    return f"{LI_AUTH_URL}?{urlencode(params)}"


def exchange_token(code: str, state_data: dict) -> dict:
    app = get_msuite_app("linkedin")
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
        "mode": _mode_from_state(state_data),
    }


# ── Discovery ────────────────────────────────────────────────────────────


def discover_accounts(client_name: str, token_data: dict):
    """Profile/Ads mode connects immediately; page mode defers to a user choice.

    Returns a list of connected accounts (profile/ads mode) or a dict with
    `pending=True` and the candidate pages (page mode). `process_oauth_callback`
    branches on the shape.
    """
    mode = token_data.get("mode")
    if mode == MODE_PAGE:
        return _discover_page_candidates(token_data)
    if mode == MODE_ADS:
        return _connect_ads(client_name, token_data)
    return _connect_profile(client_name, token_data)


def _connect_profile(client_name: str, token_data: dict) -> list[dict]:
    token = token_data["access_token"]
    expires_in = token_data.get("expires_in", TOKEN_LIFETIME_SECONDS)

    user_info = _get_user_info(token)
    person_sub = user_info.get("sub", "")
    if not person_sub:
        logger.warning("LinkedIn: could not resolve userinfo; nothing to connect")
        return []

    person_name = user_info.get("name", "")
    person_picture = user_info.get("picture", "")

    auth_name = upsert_auth_account(client_name, Platform.LINKEDIN, person_sub, {
        "account_name": person_name,
        "profile_picture_url": person_picture,
    })

    _store_account(
        client_name=client_name,
        auth_name=auth_name,
        account_id=f"urn:li:person:{person_sub}",
        display_name=person_name,
        avatar_url=person_picture,
        account_type="Profile",
        token_data=token_data,
        expires_in=expires_in,
    )
    return [{"platform": Platform.LINKEDIN, "name": person_name}]


def _discover_page_candidates(token_data: dict) -> dict:
    """Enumerate admin-managed orgs. Creates nothing — the user picks."""
    token = token_data["access_token"]
    candidates = []

    for org in _get_managed_organizations(token):
        org_id = org.get("organizationalTarget", "").split(":")[-1]
        if not org_id:
            continue
        info = _get_org_info(token, org_id)
        candidates.append({
            "id": f"urn:li:organization:{org_id}",
            "name": info.get("localizedName") or info.get("vanityName") or f"Organization {org_id}",
            "avatar_url": _extract_org_logo_url(info),
        })

    if not candidates:
        logger.warning(
            "LinkedIn: no admin-managed organizations found. If the user does "
            "administer pages, check that the app has Community Management API access."
        )
    return {"pending": True, "candidates": candidates}


def finalize_pages(
    client_name: str,
    token_data: dict,
    candidates: list[dict],
    selected: list[str],
) -> list[dict]:
    """Create + push the org pages the user ticked in the client UI."""
    token = token_data["access_token"]
    expires_in = token_data.get("expires_in", TOKEN_LIFETIME_SECONDS)
    wanted = set(selected)
    chosen = [c for c in candidates if c.get("id") in wanted]
    if not chosen:
        return []

    # Anchor pages to the authorizing person, so a profile and the pages they
    # administer share one Auth Account instead of an "org-admin:" pseudo-id.
    user_info = _get_user_info(token)
    anchor_id = user_info.get("sub") or chosen[0]["id"].split(":")[-1]
    auth_name = upsert_auth_account(client_name, Platform.LINKEDIN, anchor_id, {
        "account_name": user_info.get("name") or "LinkedIn",
        "profile_picture_url": user_info.get("picture", ""),
    })

    connected = []
    for cand in chosen:
        _store_account(
            client_name=client_name,
            auth_name=auth_name,
            account_id=cand["id"],
            display_name=cand.get("name", ""),
            avatar_url=cand.get("avatar_url", ""),
            account_type="Page",
            token_data=token_data,
            expires_in=expires_in,
        )
        connected.append({"platform": Platform.LINKEDIN, "name": cand.get("name", "")})
    return connected


def _store_account(
    client_name: str,
    auth_name: str,
    account_id: str,
    display_name: str,
    avatar_url: str,
    account_type: str,
    token_data: dict,
    expires_in: int,
):
    """Persist the Connected Account and push it to the client site.

    account_id is the full URN for both types, so the client can tell a page
    from a profile by shape alone (as publish/insights already do).
    """
    token = token_data["access_token"]
    refresh_value = token_data.get("refresh_token", "")
    expiry = add_to_date(now(), seconds=expires_in)

    upsert_connected_account(client_name, Platform.LINKEDIN, account_id, {
        "display_name": display_name,
        "auth_account": auth_name,
        "access_token": token,
        "refresh_token": refresh_value,
        "token_type": "User Token",
        "msuite_app": token_data.get("app_name", ""),
        "token_expiry": expiry,
    })

    push_account_to_client(client_name, Platform.LINKEDIN, {
        "author_urn": account_id,
        "display_name": display_name,
        "account_type": account_type,
        "access_token": token,
        "refresh_token": refresh_value,
        "token_expires_at": str(expiry),
        "avatar_url": avatar_url,
    })


def _connect_ads(client_name: str, token_data: dict) -> list[dict]:
    token = token_data["access_token"]
    expires_in = token_data.get("expires_in", TOKEN_LIFETIME_SECONDS)
    refresh_value = token_data.get("refresh_token", "")
    expiry = add_to_date(now(), seconds=expires_in)

    user_info = _get_user_info(token)
    person_sub = user_info.get("sub", "")
    person_name = user_info.get("name", "")
    person_picture = user_info.get("picture", "")

    auth_name = upsert_auth_account(client_name, Platform.LINKEDIN, person_sub or "linkedin_ads", {
        "account_name": person_name or "LinkedIn Ads",
        "profile_picture_url": person_picture,
    })

    ad_accounts = _get_ad_accounts(token)
    connected = []
    if ad_accounts:
        for acc in ad_accounts:
            raw_id = str(acc.get("id") or acc.get("account_id") or "")
            acc_id = raw_id.replace("urn:li:sponsoredAccount:", "")
            acc_name = acc.get("name") or acc.get("account_name") or f"LinkedIn Ads ({acc_id})"
            currency = acc.get("currency") or "USD"

            upsert_connected_account(client_name, Platform.LINKEDIN, f"urn:li:sponsoredAccount:{acc_id}", {
                "display_name": acc_name,
                "auth_account": auth_name,
                "access_token": token,
                "refresh_token": refresh_value,
                "token_type": "User Token",
                "msuite_app": token_data.get("app_name", ""),
                "token_expiry": expiry,
            })

            push_account_to_client(client_name, "LinkedIn Ads", {
                "ad_account_id": acc_id,
                "ad_account_name": acc_name,
                "currency": currency,
                "access_token": token,
                "refresh_token": refresh_value,
                "token_expires_at": str(expiry),
            })
            connected.append({"platform": "LinkedIn Ads", "name": acc_name})
    else:
        acc_id = person_sub or "default"
        acc_name = f"{person_name}'s LinkedIn Ad Account" if person_name else "LinkedIn Ad Account"
        upsert_connected_account(client_name, Platform.LINKEDIN, f"urn:li:sponsoredAccount:{acc_id}", {
            "display_name": acc_name,
            "auth_account": auth_name,
            "access_token": token,
            "refresh_token": refresh_value,
            "token_type": "User Token",
            "msuite_app": token_data.get("app_name", ""),
            "token_expiry": expiry,
        })
        push_account_to_client(client_name, "LinkedIn Ads", {
            "ad_account_id": acc_id,
            "ad_account_name": acc_name,
            "currency": "USD",
            "access_token": token,
            "refresh_token": refresh_value,
            "token_expires_at": str(expiry),
        })
        connected.append({"platform": "LinkedIn Ads", "name": acc_name})

    return connected


def _get_ad_accounts(token: str) -> list[dict]:
    headers = _rest_headers(token)

    # 1. Try /rest/adAccounts?q=search
    try:
        resp = requests.get(
            f"{LI_API}/rest/adAccounts?q=search",
            headers=headers,
            timeout=30,
        )
        if resp.ok:
            data = resp.json()
            elements = data.get("elements", [])
            if elements:
                return elements
    except Exception as e:
        logger.error(f"LinkedIn /rest/adAccounts failed: {e}")

    # 2. Try /rest/adAccountUsers?q=authenticatedUser (accounts user has roles in)
    try:
        resp = requests.get(
            f"{LI_API}/rest/adAccountUsers?q=authenticatedUser",
            headers=headers,
            timeout=30,
        )
        if resp.ok:
            data = resp.json()
            account_users = data.get("elements", [])
            accounts = []
            for au in account_users:
                urn = au.get("account", "")
                acc_id = urn.split(":")[-1] if ":" in urn else urn
                if not acc_id:
                    continue
                info = _get_single_ad_account(token, acc_id)
                if info:
                    accounts.append(info)
                else:
                    accounts.append({"id": acc_id, "name": f"LinkedIn Ad Account ({acc_id})"})
            if accounts:
                return accounts
    except Exception as e:
        logger.error(f"LinkedIn /rest/adAccountUsers failed: {e}")

    # 3. Try /v2/adAccountsV2?q=search
    try:
        resp2 = requests.get(
            f"{LI_API}/v2/adAccountsV2?q=search",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp2.ok:
            data = resp2.json()
            elements = data.get("elements", [])
            if elements:
                return elements
    except Exception as e:
        logger.error(f"LinkedIn /v2/adAccountsV2 failed: {e}")

    return []


def _get_single_ad_account(token: str, acc_id: str) -> dict:
    headers = _rest_headers(token)
    try:
        resp = requests.get(
            f"{LI_API}/rest/adAccounts/{acc_id}",
            headers=headers,
            timeout=30,
        )
        if resp.ok:
            return resp.json()
    except Exception:
        pass
    try:
        resp = requests.get(
            f"{LI_API}/v2/adAccountsV2/{acc_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp.ok:
            return resp.json()
    except Exception:
        pass
    return {}


# ── Token refresh ────────────────────────────────────────────────────────


def refresh_token(connected_account_name: str):
    """Refresh a 60-day token using grant_type=refresh_token."""
    ca = frappe.get_doc("MSuite Connected Account", connected_account_name)
    stored_refresh = ca.get_password("refresh_token", raise_exception=False)
    if not stored_refresh:
        logger.warning(f"LinkedIn: no refresh token stored for {connected_account_name}")
        return

    app = frappe.get_doc("MSuite App", ca.msuite_app)
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
    if data.get("refresh_token"):
        ca.refresh_token = data["refresh_token"]
    ca.token_expiry = add_to_date(now(), seconds=data.get("expires_in", TOKEN_LIFETIME_SECONDS))
    ca.save(ignore_permissions=True)
    frappe.db.commit()
    logger.info(f"LinkedIn token refreshed for {connected_account_name}")


# ── LinkedIn API helpers ─────────────────────────────────────────────────


def _get_user_info(token: str) -> dict:
    try:
        resp = requests.get(
            f"{LI_API}/v2/userinfo",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp.ok:
            return resp.json()
        logger.warning(f"LinkedIn userinfo status={resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"LinkedIn userinfo failed: {e}")
    return {}


def _person_urn(token: str) -> str:
    sub = _get_user_info(token).get("sub")
    if sub:
        return f"urn:li:person:{sub}"

    # /v2/me is the pre-OIDC fallback for apps without the `openid` scope.
    try:
        resp = requests.get(
            f"{LI_API}/v2/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.ok and resp.json().get("id"):
            return f"urn:li:person:{resp.json()['id']}"
    except Exception as e:
        logger.debug(f"LinkedIn /v2/me fallback failed: {e}")
    return ""


def _get_managed_organizations(token: str) -> list[dict]:
    """Orgs where the authorizing member holds an administrator role.

    Tries the versioned /rest/ ACL endpoint first and falls back to the
    legacy /v2/ one, with and without an explicit roleAssignee — older apps
    accept only some of these combinations.
    """
    rest_headers = _rest_headers(token)
    v2_headers = {"Authorization": f"Bearer {token}", "X-Restli-Protocol-Version": "2.0.0"}

    person_urn = _person_urn(token)
    urls = []
    if person_urn:
        urls += [
            f"{LI_API}/rest/organizationAcls?q=roleAssignee&roleAssignee={person_urn}",
            f"{LI_API}/v2/organizationalEntityAcls?q=roleAssignee&roleAssignee={person_urn}",
        ]
    urls += [
        f"{LI_API}/rest/organizationAcls?q=roleAssignee",
        f"{LI_API}/v2/organizationalEntityAcls?q=roleAssignee",
    ]

    for url in urls:
        headers = rest_headers if "/rest/" in url else v2_headers
        try:
            resp = requests.get(url, headers=headers, timeout=20)
        except Exception as e:
            logger.error(f"LinkedIn ACL lookup {url} failed: {e}")
            continue

        if not resp.ok:
            logger.warning(f"LinkedIn ACL {url} status={resp.status_code}: {resp.text[:200]}")
            continue

        normalized = []
        for el in resp.json().get("elements", []):
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

    return []


def _get_org_info(token: str, org_id: str) -> dict:
    try:
        resp = requests.get(
            f"{LI_API}/rest/organizations/{org_id}",
            params={"fields": "localizedName,vanityName,logoV2"},
            headers=_rest_headers(token),
            timeout=30,
        )
        if resp.ok:
            return resp.json()
    except Exception as e:
        logger.error(f"LinkedIn /rest/organizations/{org_id} failed: {e}")

    try:
        resp = requests.get(
            f"{LI_API}/v2/organizations/{org_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp.ok:
            return resp.json()
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
