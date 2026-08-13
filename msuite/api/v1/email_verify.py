"""
Million Verifier relay.

Client sites never hold the MillionVerifier key — it is ONE platform-billed
credential shared across every tenant, so a copy on each customer-controlled
site would leak a platform secret and make rotation an N-site operation.
Same reasoning, same shape as `gmail_relay.py`.

The key lives on an `MSuite App` row with `platform = "MillionVerifier"`,
in the `app_secret` Password field.

Two endpoints, both client-authenticated:

    verify_single {emails: [...]}  -> N calls to MV's v3 single API
    verify_bulk   {emails: [...]}  -> MV's bulk file API (upload/poll/download)

Both return the same envelope so the client doesn't branch:

    {"results": {"<email>": {"result": "ok", ...}}, "credits": <int|None>}

Neither raises on an MV failure — the client treats a missing verdict as
"error" and lets its risk policy decide. A verification outage must degrade a
campaign, never block it.
"""

import csv
import io
import time

import frappe
import requests

from msuite.utils.validators import (
    error_response,
    require_msuite_client_auth,
    success_response,
)

SINGLE_API = "https://api.millionverifier.com/api/v3/"
BULK_UPLOAD = "https://bulkapi.millionverifier.com/bulkapi/v2/upload"
BULK_FILEINFO = "https://bulkapi.millionverifier.com/bulkapi/v2/fileinfo"
BULK_DOWNLOAD = "https://bulkapi.millionverifier.com/bulkapi/v2/download"

APP_PLATFORM = "MillionVerifier"

SINGLE_TIMEOUT = 20
HTTP_TIMEOUT = 30
MAX_EMAILS_PER_CALL = 50_000

# Bulk polling: MV finishes most jobs in well under a minute, but a large
# file on a busy queue can take several. Ceiling is deliberate — an unbounded
# poll would pin a provider worker indefinitely on MV's availability.
POLL_INTERVAL = 15
POLL_CEILING = 30 * 60


def _get_api_key() -> str:
    name = frappe.db.get_value("MSuite App", {"platform": APP_PLATFORM, "is_active": 1}, "name")
    if not name:
        raise ValueError(f"No active MSuite App row for platform '{APP_PLATFORM}'")
    key = frappe.utils.password.get_decrypted_password(
        "MSuite App", name, "app_secret", raise_exception=False
    )
    if not key:
        raise ValueError(f"MSuite App '{name}' has no app_secret set")
    return key


def _read_request(kwargs: dict) -> dict:
    data = kwargs
    if not data and frappe.request:
        data = frappe.request.get_json(silent=True) or {}
    return data or {}


def _clean_emails(raw) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    seen = set()
    out = []
    for e in raw or []:
        e = (str(e) or "").strip().lower()
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out[:MAX_EMAILS_PER_CALL]


@frappe.whitelist(allow_guest=True)
def verify_single(**kwargs):
    """Verify a small batch one address at a time via MV's v3 single API."""
    data = _read_request(kwargs)

    client_name = data.get("client_name")
    if not client_name:
        return error_response("INVALID_REQUEST", "client_name is required")
    try:
        require_msuite_client_auth(client_name)
    except Exception as e:
        return error_response("AUTH_FAILED", str(e))

    emails = _clean_emails(data.get("emails"))
    if not emails:
        return error_response("INVALID_REQUEST", "emails is required and must be a non-empty list")

    try:
        api_key = _get_api_key()
    except ValueError as e:
        return error_response("NOT_CONFIGURED", str(e))

    results = {}
    credits = None
    for email in emails:
        entry = _verify_one(api_key, email)
        results[email] = entry
        if entry.get("credits") is not None:
            credits = entry["credits"]

    return success_response({"results": results, "credits": credits})


def _verify_one(api_key: str, email: str) -> dict:
    """One MV call. Returns MV's payload, or an {"error": ...} stub.

    Retries twice on transport failure and 5xx (2s, 8s). A 4xx is not
    retried — it means the request itself is wrong and repeating it just
    burns time.
    """
    last_error = "unknown"
    for attempt, backoff in enumerate((2, 8, None)):
        try:
            resp = requests.get(
                SINGLE_API,
                params={"api": api_key, "email": email, "timeout": SINGLE_TIMEOUT},
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.json()
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code < 500:
                break
        except requests.RequestException as e:
            last_error = str(e)[:200]

        if backoff is None:
            break
        time.sleep(backoff)

    frappe.log_error(
        title="MillionVerifier single verify failed",
        message=f"{email}: {last_error}",
    )
    return {"email": email, "error": last_error}


@frappe.whitelist(allow_guest=True)
def verify_bulk(**kwargs):
    """Verify a large batch through MV's bulk file API.

    Runs upload -> poll -> download inline. The client's relay call is a
    long-lived request by design: the alternative is a provider-side job
    plus a client-side callback, which is a lot of machinery for something
    that finishes in under a minute in the normal case.
    """
    data = _read_request(kwargs)

    client_name = data.get("client_name")
    if not client_name:
        return error_response("INVALID_REQUEST", "client_name is required")
    try:
        require_msuite_client_auth(client_name)
    except Exception as e:
        return error_response("AUTH_FAILED", str(e))

    emails = _clean_emails(data.get("emails"))
    if not emails:
        return error_response("INVALID_REQUEST", "emails is required and must be a non-empty list")

    try:
        api_key = _get_api_key()
    except ValueError as e:
        return error_response("NOT_CONFIGURED", str(e))

    try:
        file_id = _bulk_upload(api_key, emails)
        info = _bulk_poll(api_key, file_id)
        results = _bulk_download(api_key, file_id)
    except Exception as e:
        frappe.log_error(
            title="MillionVerifier bulk verify failed",
            message=f"{len(emails)} address(es): {e}\n\n{frappe.get_traceback()}",
        )
        return error_response("VERIFY_FAILED", str(e)[:300])

    # Anything MV didn't return a row for still needs a key in the map, so the
    # client doesn't have to distinguish "absent" from "never asked".
    for email in emails:
        results.setdefault(email, {"email": email, "error": "No row returned by MillionVerifier"})

    return success_response({"results": results, "credits": info.get("credit")})


def _bulk_upload(api_key: str, emails: list[str]) -> int:
    payload = io.BytesIO("\n".join(emails).encode("utf-8"))
    resp = requests.post(
        BULK_UPLOAD,
        data={"key": api_key},
        files={"file_contents": ("emails.csv", payload, "text/csv")},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise ValueError(f"upload returned HTTP {resp.status_code}: {resp.text[:200]}")

    body = resp.json()
    file_id = body.get("file_id")
    if not file_id:
        raise ValueError(f"upload returned no file_id: {str(body)[:200]}")
    return file_id


def _bulk_poll(api_key: str, file_id: int) -> dict:
    waited = 0
    while waited < POLL_CEILING:
        resp = requests.get(
            BULK_FILEINFO, params={"key": api_key, "file_id": file_id}, timeout=HTTP_TIMEOUT
        )
        if resp.status_code == 200:
            info = resp.json()
            status = (info.get("status") or "").lower()
            if status == "finished":
                return info
            if status in ("error", "canceled", "cancelled"):
                raise ValueError(f"MillionVerifier reported status '{status}' for file {file_id}")

        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

    raise TimeoutError(f"file {file_id} did not finish within {POLL_CEILING}s")


def _bulk_download(api_key: str, file_id: int) -> dict:
    resp = requests.get(
        BULK_DOWNLOAD,
        params={"key": api_key, "file_id": file_id, "filter": "all"},
        timeout=HTTP_TIMEOUT * 4,
    )
    if resp.status_code != 200:
        raise ValueError(f"download returned HTTP {resp.status_code}: {resp.text[:200]}")

    results = {}
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        # MV's column casing has varied across API versions; normalise rather
        # than depending on one spelling.
        lowered = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        email = lowered.get("email")
        if not email:
            continue
        results[email.lower()] = {
            "email": email.lower(),
            "result": (lowered.get("result") or lowered.get("quality") or "").lower(),
            "quality": lowered.get("quality"),
            "free": lowered.get("free"),
            "role": lowered.get("role"),
        }
    return results
