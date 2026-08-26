"""
ai_calling.py — Provider-side AI Calling API endpoints.

Exposes configuration to authenticated client sites so they can
retrieve the AI backend URL without relying on a prior push.

Also acts as the single routing hub between the AI Calling FastAPI
backend and every client site: the backend only knows the PROVIDER url;
each callback carries a DID (or call_id) that the provider resolves to
the owning client site via the MSuite AI DID registry, then forwards
with that client's stored credentials. Client sites self-register their
DIDs (``register_did``) whenever an AI Calling config is saved, so
onboarding a new client requires no URL configuration anywhere.
"""
import requests

import frappe
from frappe.utils import now_datetime

from msuite.utils.validators import (
    require_msuite_client_auth,
    success_response,
    error_response,
)
from msuite.constants import MSUITE_LOGGER_NAME

logger = frappe.logger(MSUITE_LOGGER_NAME)


@frappe.whitelist(allow_guest=True)
def get_ai_calling_config(client_identifier: str) -> dict:
    """
    Return the AI Calling backend URL to an authenticated client.

    The client sends its ``X-MSuite-Provider-Key`` / ``X-MSuite-Provider-Secret``
    headers (the same credentials stored on MSuite Settings on the client side).

    Args:
        client_identifier: MSuite Client doc name or client_code.

    Returns:
        {"status": "success", "data": {"ai_backend_url": "https://..."}}
    """
    try:
        require_msuite_client_auth(client_identifier)
    except frappe.AuthenticationError as exc:
        return error_response("AUTH_FAILED", str(exc))

    # Look up the AI Calling MSuite App record
    app_name = frappe.db.get_value(
        "MSuite App",
        {"platform": "AI Calling", "is_active": 1},
        "name",
    )
    if not app_name:
        return error_response("NOT_CONFIGURED", "AI Calling is not configured on this provider.")

    ai_backend_url = frappe.db.get_value("MSuite App", app_name, "ai_backend_url") or ""

    logger.info(
        f"[AI Calling] get_ai_calling_config called by client={client_identifier}, "
        f"returning ai_backend_url={'<set>' if ai_backend_url else '<empty>'}"
    )

    return success_response({"ai_backend_url": ai_backend_url})


# ═════════════════════════ Presigned S3 access ════════════════════════════════
#
# Clients never hold AWS credentials: they request short-lived presigned
# URLs from the provider (authenticated with their MSuite credentials) and
# upload/download directly against S3.

def _s3_client_and_bucket():
    import boto3
    from botocore.client import Config

    doc = frappe.get_doc("S3 File Attachment", "S3 File Attachment")
    client = boto3.client(
        "s3",
        aws_access_key_id=doc.aws_key,
        aws_secret_access_key=doc.get_password("aws_secret"),
        region_name=doc.region_name,
        config=Config(signature_version="s3v4"),
    )
    return client, doc.bucket_name


@frappe.whitelist(allow_guest=True, methods=["POST"])
def get_s3_presigned_put(
    client_identifier: str,
    key: str,
    content_type: str = "application/pdf",
    expires_in: int = 600,
) -> dict:
    """
    Return a presigned PUT URL for an authenticated client. Keys are
    namespaced under the client's code so tenants cannot write into each
    other's prefixes.
    """
    try:
        client_doc = require_msuite_client_auth(client_identifier)
    except frappe.AuthenticationError as exc:
        return error_response("AUTH_FAILED", str(exc))

    key = (key or "").strip().lstrip("/")
    if not key or ".." in key:
        return error_response("INVALID", "Invalid S3 key.")

    namespace = client_doc.client_code or client_doc.name
    full_key = f"clients/{namespace}/{key}"

    try:
        s3, bucket = _s3_client_and_bucket()
        url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": full_key, "ContentType": content_type},
            ExpiresIn=min(int(expires_in or 600), 3600),
        )
        return success_response({"url": url, "s3_key": full_key, "bucket": bucket})
    except Exception as exc:
        logger.error(f"[AI Calling] Presigned PUT failed for {client_doc.name}: {exc}")
        return error_response("ERROR", str(exc))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def get_s3_presigned_get(
    client_identifier: str,
    key: str,
    expires_in: int = 600,
) -> dict:
    """
    Return a presigned GET URL. Clients may only read their own namespace
    (``clients/<code>/…``) or call recordings (``recordings/…``).
    """
    try:
        client_doc = require_msuite_client_auth(client_identifier)
    except frappe.AuthenticationError as exc:
        return error_response("AUTH_FAILED", str(exc))

    key = (key or "").strip().lstrip("/")
    namespace = client_doc.client_code or client_doc.name
    allowed = key.startswith(f"clients/{namespace}/") or key.startswith("recordings/")
    if not key or ".." in key or not allowed:
        return error_response("FORBIDDEN", "Key outside your namespace.")

    try:
        s3, bucket = _s3_client_and_bucket()
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=min(int(expires_in or 600), 3600),
        )
        return success_response({"url": url, "s3_key": key, "bucket": bucket})
    except Exception as exc:
        logger.error(f"[AI Calling] Presigned GET failed for {client_doc.name}: {exc}")
        return error_response("ERROR", str(exc))


# ═════════════════════════════ DID registry ═══════════════════════════════════

def _did_variants(did: str) -> list[str]:
    """Lookup variants for a DID: as-is, with and without a leading +."""
    did = (did or "").strip()
    if not did:
        return []
    variants = [did]
    if did.startswith("+"):
        variants.append(did[1:])
    else:
        variants.append(f"+{did}")
    return variants


def _resolve_client_by_did(did: str):
    """Return the active MSuite Client doc owning a DID, or None."""
    variants = _did_variants(did)
    if not variants:
        return None
    row = frappe.db.get_value(
        "MSuite AI DID",
        {"did": ["in", variants], "is_active": 1},
        "client",
    )
    if not row:
        return None
    return frappe.get_doc("MSuite Client", row)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def register_did(client_identifier: str, did: str, organization: str = None) -> dict:
    """
    Called by client sites (with their MSuite credentials) whenever an
    AI Calling config is saved. Upserts the DID → client mapping so the
    provider can route backend callbacks without manual configuration.
    """
    try:
        client_doc = require_msuite_client_auth(client_identifier)
    except frappe.AuthenticationError as exc:
        return error_response("AUTH_FAILED", str(exc))

    did = (did or "").strip()
    if not did:
        return error_response("INVALID", "DID is required.")

    existing = frappe.db.get_value(
        "MSuite AI DID", {"did": ["in", _did_variants(did)]}, ["name", "client"], as_dict=True
    )
    if existing:
        # Tenant isolation: a DID already owned by another client must not be
        # re-pointed by this caller, or Client A could hijack Client B's
        # inbound-call routing / org-config lookups.
        if existing.client and existing.client != client_doc.name:
            return error_response(
                "CONFLICT",
                f"DID {did} is already registered to another client.",
            )
        frappe.db.set_value("MSuite AI DID", existing.name, {
            "client": client_doc.name,
            "organization": organization,
            "is_active": 1,
            "last_registered_at": now_datetime(),
        })
    else:
        frappe.get_doc({
            "doctype": "MSuite AI DID",
            "did": did,
            "client": client_doc.name,
            "organization": organization,
            "is_active": 1,
            "last_registered_at": now_datetime(),
        }).insert(ignore_permissions=True)
    frappe.db.commit()

    logger.info(f"[AI Calling] Registered DID {did} → client {client_doc.name}")
    return success_response({"did": did, "client": client_doc.name})


# ══════════════════ Backend → client routed proxy endpoints ═══════════════════
#
# The FastAPI backend calls THESE endpoints (provider URL only). Each one
# resolves the owning client site and forwards the request with that
# client's provider-auth credentials. Responses include ``client_code`` so
# the backend can echo it on follow-up callbacks (recording upload,
# campaign lead updates) for exact routing.

_CLIENT_APP_PREFIX = "ai_calling.ai_calling"
_CALL_ROUTE_CACHE_TTL = 14 * 24 * 3600  # call_id → client, survives late recording uploads


def _require_backend_key():
    """
    Authenticate the FastAPI backend on proxy endpoints. The backend must
    send ``ai_backend_shared_key`` (from the provider's site_config) as the
    ``X-AI-Backend-Key`` header. Fails closed: if the key is not configured
    on the provider, every proxy endpoint is rejected — these endpoints
    forward to client sites and must never be reachable unauthenticated.
    """
    expected = frappe.conf.get("ai_backend_shared_key")
    if not expected:
        logger.error(
            "[AI Calling] Proxy endpoint called but ai_backend_shared_key is not "
            "configured — rejecting. Set it in site_config and on the backend."
        )
        frappe.throw(
            "AI backend authentication is not configured on this provider.",
            frappe.AuthenticationError,
        )
    import hmac
    sent = (frappe.request.headers.get("X-AI-Backend-Key") or "").strip()
    if not (sent and hmac.compare_digest(sent, str(expected))):
        frappe.throw("Invalid or missing X-AI-Backend-Key.", frappe.AuthenticationError)


def _forward_to_client(client_doc, endpoint: str, payload: dict) -> dict:
    """POST to a client's whitelisted endpoint and return the parsed message."""
    from msuite.services.client_service import make_auth_headers
    resp = requests.post(
        f"{client_doc.client_url}/api/method/{endpoint}",
        headers=make_auth_headers(client_doc),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("message")


def _cache_call_route(call_id: str, client_name: str):
    if call_id:
        frappe.cache().set_value(
            f"ai_call_route:{call_id}", client_name,
            expires_in_sec=_CALL_ROUTE_CACHE_TTL,
        )


def _client_candidates(client_code: str = None, call_id: str = None):
    """
    Resolve target client(s) for a callback that has no DID:
    explicit client_code → cached call route → every client with a
    registered DID (broadcast fallback; the wrong sites simply report
    'not found').
    """
    if client_code:
        name = frappe.db.get_value(
            "MSuite Client", {"client_code": client_code}, "name"
        ) or (client_code if frappe.db.exists("MSuite Client", client_code) else None)
        if name:
            return [frappe.get_doc("MSuite Client", name)]

    if call_id:
        cached = frappe.cache().get_value(f"ai_call_route:{call_id}")
        if cached and frappe.db.exists("MSuite Client", cached):
            return [frappe.get_doc("MSuite Client", cached)]

    names = frappe.db.get_all("MSuite AI DID", filters={"is_active": 1}, pluck="client", distinct=True)
    return [frappe.get_doc("MSuite Client", n) for n in names]


@frappe.whitelist(allow_guest=True)
def get_inbound_config(phone_number: str) -> dict:
    """Route the backend's inbound-config lookup to the client owning the DID."""
    _require_backend_key()
    client_doc = _resolve_client_by_did(phone_number)
    if not client_doc:
        return error_response("UNKNOWN_DID", f"No client registered for DID {phone_number}")

    config = _forward_to_client(
        client_doc,
        f"{_CLIENT_APP_PREFIX}.doctype.ai_calling_inbound_config.ai_calling_inbound_config.get_inbound_config",
        {"phone_number": phone_number},
    )
    if isinstance(config, dict):
        config["client_code"] = client_doc.client_code or client_doc.name
    return success_response({"config": config, "client_code": client_doc.client_code or client_doc.name})


@frappe.whitelist(allow_guest=True)
def get_config_by_did(did: str, call_type: str = "Inbound", **kwargs) -> dict:
    """Route the backend's org-config lookup to the client owning the DID."""
    _require_backend_key()
    client_doc = _resolve_client_by_did(did)
    if not client_doc:
        return error_response("UNKNOWN_DID", f"No client registered for DID {did}")

    config = _forward_to_client(
        client_doc,
        f"{_CLIENT_APP_PREFIX}.doctype.ai_calling_number_setup"
        ".ai_calling_number_setup.get_config_by_did",
        {"did": did, "call_type": call_type},
    )
    if isinstance(config, dict):
        config["client_code"] = client_doc.client_code or client_doc.name
    return success_response({"config": config, "client_code": client_doc.client_code or client_doc.name})


@frappe.whitelist(allow_guest=True, methods=["POST"])
def create_call_log(**kwargs) -> dict:
    """
    Route the backend's post-call log to the owning client, resolved from
    ``did_number`` (or explicit ``client_code``). Remembers call_id → client
    so later recording callbacks route exactly.
    """
    _require_backend_key()
    kwargs.pop("cmd", None)
    client_code = kwargs.pop("client_code", None)
    did = kwargs.get("did_number")

    client_doc = _resolve_client_by_did(did) if did else None
    if not client_doc:
        candidates = _client_candidates(client_code=client_code)
        if len(candidates) != 1:
            return error_response(
                "UNKNOWN_DID",
                f"Cannot route call log: no client registered for DID {did!r} "
                "and no unambiguous fallback. Pass did_number or client_code.",
            )
        client_doc = candidates[0]

    result = _forward_to_client(
        client_doc,
        f"{_CLIENT_APP_PREFIX}.doctype.ai_calling_call_log.ai_calling_call_log.create_call_log",
        kwargs,
    )
    _cache_call_route(kwargs.get("call_id"), client_doc.name)
    return success_response({
        "call_log": result,
        "client_code": client_doc.client_code or client_doc.name,
    })


@frappe.whitelist(allow_guest=True, methods=["POST"])
def update_call_log_recording(**kwargs) -> dict:
    """
    Route the backend's recording-uploaded callback. Resolution order:
    explicit client_code → cached call route → broadcast to all clients
    with registered DIDs (harmless on non-owners: they report not-found).
    """
    _require_backend_key()
    kwargs.pop("cmd", None)
    client_code = kwargs.pop("client_code", None)
    call_id = kwargs.get("call_id")

    last_error = None
    for client_doc in _client_candidates(client_code=client_code, call_id=call_id):
        try:
            result = _forward_to_client(
                client_doc,
                f"{_CLIENT_APP_PREFIX}.doctype.ai_calling_call_log.ai_calling_call_log.update_call_log_recording",
                kwargs,
            )
            _cache_call_route(call_id, client_doc.name)
            return success_response({
                "call_log": result,
                "client_code": client_doc.client_code or client_doc.name,
            })
        except Exception as exc:
            last_error = exc
            continue

    return error_response(
        "NOT_FOUND",
        f"No client site accepted recording update for call_id={call_id}: {last_error}",
    )


@frappe.whitelist(allow_guest=True, methods=["POST"])
def update_broadcast_recipient(**kwargs) -> dict:
    """
    Route the backend's campaign-lead update. Resolution order: explicit
    client_code → cached route via call_uuid → broadcast fallback.
    """
    _require_backend_key()
    kwargs.pop("cmd", None)
    client_code = kwargs.pop("client_code", None)
    call_uuid = kwargs.get("call_uuid")

    last_error = None
    for client_doc in _client_candidates(client_code=client_code, call_id=call_uuid):
        try:
            result = _forward_to_client(
                client_doc,
                f"{_CLIENT_APP_PREFIX}.doctype.ai_calling_broadcast.ai_calling_broadcast.update_broadcast_recipient",
                kwargs,
            )
            if isinstance(result, dict) and result.get("success") is False:
                last_error = result.get("error")
                continue
            return success_response({
                "result": result,
                "client_code": client_doc.client_code or client_doc.name,
            })
        except Exception as exc:
            last_error = exc
            continue

    return error_response(
        "NOT_FOUND",
        f"No client site accepted campaign lead update: {last_error}",
    )


@frappe.whitelist(allow_guest=True)
def get_assistant_config_full(assistant: str = None, did: str = None, client_code: str = None, **kwargs) -> dict:
    """
    Route get_assistant_config_full to the owning client site.
    Supports routing by:
      1. did (phone number)
      2. explicit client_code
      3. fallback iteration through active clients
    """
    _require_backend_key()
    assistant = assistant or kwargs.get("assistant") or kwargs.get("name")
    if not assistant:
        return error_response("INVALID", "Assistant identifier is required.")

    client_doc = None
    if did:
        client_doc = _resolve_client_by_did(did)
    if not client_doc and client_code:
        candidates = _client_candidates(client_code=client_code)
        if candidates:
            client_doc = candidates[0]

    if not client_doc:
        for c in _client_candidates():
            try:
                res = _forward_to_client(
                    c,
                    f"{_CLIENT_APP_PREFIX}.api.get_assistant_config_full",
                    {"assistant": assistant},
                )
                if res and isinstance(res, dict) and res.get("name"):
                    return success_response({"config": res, "client_code": c.client_code or c.name})
            except Exception:
                continue
        return error_response("NOT_FOUND", f"Assistant {assistant} not found on any client site.")

    res = _forward_to_client(
        client_doc,
        f"{_CLIENT_APP_PREFIX}.api.get_assistant_config_full",
        {"assistant": assistant},
    )
    return success_response({"config": res, "client_code": client_doc.client_code or client_doc.name})
