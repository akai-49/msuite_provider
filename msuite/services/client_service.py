"""
Client communication service.

All provider-to-client HTTP calls go through this module.
Handles credential generation, plan data building, and push operations.
"""
import json
import secrets

import frappe
import requests
from frappe.utils import now
from frappe.utils.password import get_decrypted_password

from msuite.constants import (
    ClientStatus,
    SyncStatus,
    CLIENT_CONNECT_TIMEOUT,
    CLIENT_PUSH_TIMEOUT,
    CLIENT_SYNC_MAX_FAILURES,
    MSUITE_LOGGER_NAME,
)
from msuite.exceptions import ClientConnectionError, ClientActivationError

logger = frappe.logger(MSUITE_LOGGER_NAME)


# ======================================================================
# Credential generation
# ======================================================================


def generate_credentials() -> tuple[str, str]:
    """
    Generate an api_key + api_secret pair for client authentication.

    Returns:
        Tuple of (api_key, api_secret)
    """
    api_key = secrets.token_urlsafe(32)
    api_secret = secrets.token_urlsafe(64)
    return api_key, api_secret


# ======================================================================
# Plan data builder
# ======================================================================


def build_client_plan_data(customer: str) -> dict:
    """
    Build the flat plan data payload to push to a client.

    Reads entitlements from the msuite entitlement service, then enriches
    each feature with its human-readable label from MSuite Product Feature.

    Args:
        customer: ERPNext Customer name

    Returns:
        Dict with plan_name, products, features (flat)

    `products` is the list of MSuite Product codes the customer holds. The
    client hides/locks whole modules on it. Note the asymmetry with
    `features`: an ABSENT `products` key means "pre-Phase-1 payload, don't
    gate", an EMPTY list means "gate everything" — see the client's
    `plan_enforcer.get_products`.
    """
    from msuite.services.entitlement_service import get_customer_entitlements

    entitlements = get_customer_entitlements(customer)

    # Determine primary plan name from active plans
    active_plans = entitlements.get("active_plans", [])
    plan_name = ", ".join(active_plans) if active_plans else "No active plan"

    # Enrich features with labels from MSuite Product Feature
    raw_features = entitlements.get("features", {})
    enriched_features = {}

    for feature_key, feature_data in raw_features.items():
        # Look up the human-readable label
        label = frappe.db.get_value(
            "MSuite Product Feature",
            {"feature_key": feature_key},
            "feature_label",
        ) or feature_key.replace("_", " ").title()

        enriched_features[feature_key] = {
            "enabled": feature_data.get("enabled", False),
            "limit": feature_data.get("limit"),
            "label": label,
        }

    payload = {
        "plan_name": plan_name,
        "features": enriched_features,
    }

    # Entitlements are Redis-cached with a TTL, so right after a deploy we can
    # still be handed a pre-Phase-1 dict with no `active_products`. Emitting
    # `products: []` there would lock every module on the client; omitting the
    # key instead makes the client fall back to "don't gate" until the cache
    # rolls over. Never turn this into a `.get(..., [])`.
    if "active_products" in entitlements:
        payload["products"] = entitlements["active_products"]

    return payload


# ======================================================================
# HTTP communication with client instances
# ======================================================================


def test_client_connection(client_url: str) -> dict:
    """
    Ping the client's health_check endpoint to verify connectivity.

    Args:
        client_url: Client's base URL

    Returns:
        Dict with status, app, app_version (or error info)
    """
    endpoint = f"{client_url}/api/method/msuite_workspace.api.v1.connect.sync.health_check"

    try:
        resp = requests.get(endpoint, timeout=CLIENT_CONNECT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        # Frappe wraps responses in {"message": {...}}
        message = data.get("message", data)
        return {
            "status": message.get("status", "ok"),
            "app": message.get("app", "unknown"),
            "app_version": message.get("app_version", "unknown"),
            "message": "Connection successful",
        }
    except requests.ConnectionError:
        return {"status": "error", "message": f"Cannot connect to {client_url}"}
    except requests.Timeout:
        return {"status": "error", "message": f"Connection timed out ({CLIENT_CONNECT_TIMEOUT}s)"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def activate_client_instance(
    client_doc,
    api_key: str,
    api_secret: str,
    plan_data: dict,
) -> dict:
    """
    Push activation payload to a client instance.

    Args:
        client_doc: MSuite Client document
        api_key: Generated API key
        api_secret: Generated API secret (plaintext, will be encrypted on client)
        plan_data: Plan data dict to push

    Returns:
        Response dict from client

    Raises:
        ClientActivationError: if client rejects activation
    """
    provider_url = frappe.utils.get_url()

    payload = {
        "api_key": api_key,
        "api_secret": api_secret,
        "provider_url": provider_url,
        "customer_name": client_doc.customer,
        "client_code": client_doc.client_code,
        "plan_data": json.dumps(plan_data),
    }

    result = _post_to_client(
        client_doc.client_url,
        "msuite_workspace.api.v1.connect.sync.activate",
        payload,
        headers={},  # No auth headers yet (first call)
    )

    if result.get("status") != "success":
        frappe.throw(
            f"Client rejected activation: {result.get('message', 'Unknown error')}",
            ClientActivationError,
        )

    return result


def push_plan_to_client(client_doc, plan_data: dict) -> dict:
    """
    Push plan/entitlement data to an active client. Idempotent.

    Args:
        client_doc: MSuite Client document
        plan_data: Plan data dict

    Returns:
        Response dict from client
    """
    headers = make_auth_headers(client_doc)

    return _post_to_client(
        client_doc.client_url,
        "msuite_workspace.api.v1.connect.sync.receive_plan_update",
        {"plan_data": json.dumps(plan_data)},
        headers=headers,
    )


def push_credentials_to_client(
    client_doc,
    product: str,
    credentials: dict,
) -> dict:
    """
    Push stripped credentials to a client. Meta app secrets are never included.

    Args:
        client_doc: MSuite Client document
        product: Product identifier (e.g., "whatsapp", "facebook")
        credentials: Credential dict (app secrets already stripped)

    Returns:
        Response dict from client
    """
    headers = make_auth_headers(client_doc)

    return _post_to_client(
        client_doc.client_url,
        "msuite_workspace.api.v1.connect.credentials.receive_credentials",
        {"product": product, "credentials": json.dumps(credentials)},
        headers=headers,
    )


def push_account_state_to_client(
    client_doc,
    platform: str,
    account_id: str,
    state: str,
    error: str | None = None,
) -> dict:
    """Notify a client that a Connected Account's authoritative state
    changed — typically `needs_reauth` after a refresh-failure window
    elapses. Separate channel from `push_credentials_to_client` so we
    never carry tokens on a no-token state-change message.

    Args:
        client_doc: MSuite Client document
        platform: Platform string (matches MSuite Connected Account.platform)
        account_id: Platform's external account id (page_id / ig_user_id /
                    channel_id / etc.) — the customer site uses this to
                    locate the matching Social Account row.
        state: One of "needs_reauth" | "active" | "revoked".
        error: Diagnostic the customer site can show. Trimmed to 500 chars.

    Returns:
        Whatever the client receiver returned (status dict).
    """
    headers = make_auth_headers(client_doc)

    return _post_to_client(
        client_doc.client_url,
        "msuite_workspace.api.v1.connect.credentials.receive_account_state",
        {
            "platform":   platform,
            "account_id": account_id,
            "state":      state,
            "error":      (error or "")[:500],
        },
        headers=headers,
    )


def suspend_client_instance(client_doc) -> dict:
    """
    Tell a client to suspend (keeps credentials, clears plan data).

    Args:
        client_doc: MSuite Client document

    Returns:
        Response dict from client
    """
    headers = make_auth_headers(client_doc)

    return _post_to_client(
        client_doc.client_url,
        "msuite_workspace.api.v1.connect.sync.suspend",
        {},
        headers=headers,
    )


# ======================================================================
# Daily sync (called from scheduled_tasks/daily.py)
# ======================================================================


def sync_all_active_clients() -> dict:
    """
    Push fresh plan data to all active clients.
    Updates sync health tracking on each MSuite Client doc.

    Returns:
        Summary dict with synced, failed, disconnected counts
    """
    clients = frappe.get_all(
        "MSuite Client",
        filters={"status": ClientStatus.ACTIVE},
        fields=["name", "customer", "client_url", "sync_fail_count"],
    )

    summary = {"synced": 0, "failed": 0, "disconnected": 0}

    for client in clients:
        try:
            doc = frappe.get_doc("MSuite Client", client.name)
            plan_data = build_client_plan_data(doc.customer)
            push_plan_to_client(doc, plan_data)

            doc.last_sync = now()
            doc.last_sync_status = SyncStatus.SUCCESS
            doc.sync_fail_count = 0
            doc.save(ignore_permissions=True)
            summary["synced"] += 1

        except Exception as e:
            logger.warning(
                f"Sync failed for client {client.name}: {e}"
            )
            doc = frappe.get_doc("MSuite Client", client.name)
            doc.last_sync = now()
            doc.last_sync_status = SyncStatus.FAILED
            doc.sync_fail_count = (doc.sync_fail_count or 0) + 1

            # Auto-disconnect after too many failures
            if doc.sync_fail_count >= CLIENT_SYNC_MAX_FAILURES:
                doc.status = ClientStatus.DISCONNECTED
                summary["disconnected"] += 1
                logger.error(
                    f"Client {client.name} disconnected after "
                    f"{CLIENT_SYNC_MAX_FAILURES} consecutive failures"
                )

            doc.save(ignore_permissions=True)
            summary["failed"] += 1

    frappe.db.commit()
    logger.info(
        f"Client sync complete: {summary['synced']} synced, "
        f"{summary['failed']} failed, {summary['disconnected']} disconnected"
    )
    return summary


# ======================================================================
# Internal helpers
# ======================================================================


def make_auth_headers(client_doc) -> dict:
    """Build authentication headers for provider→client requests."""
    api_secret = get_decrypted_password(
        "MSuite Client", client_doc.name, "api_secret"
    )
    return {
        "X-MSuite-Provider-Key": client_doc.api_key or "",
        "X-MSuite-Provider-Secret": api_secret or "",
    }


def _post_to_client(
    client_url: str,
    endpoint: str,
    payload: dict,
    headers: dict,
    timeout: int = CLIENT_PUSH_TIMEOUT,
) -> dict:
    """
    POST to a client's API endpoint.

    Args:
        client_url: Client's base URL
        endpoint: Dotted method path (e.g., "msuite_workspace.api.v1.connect.sync.activate")
        payload: POST body dict
        headers: Additional HTTP headers (auth)
        timeout: Request timeout in seconds

    Returns:
        Parsed response dict

    Raises:
        ClientConnectionError: on network/HTTP errors
    """
    url = f"{client_url}/api/method/{endpoint}"

    try:
        resp = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # Frappe wraps responses in {"message": {...}}
        return data.get("message", data)

    except requests.ConnectionError:
        frappe.throw(
            f"Cannot connect to client at {client_url}",
            ClientConnectionError,
        )
    except requests.Timeout:
        frappe.throw(
            f"Client at {client_url} timed out ({timeout}s)",
            ClientConnectionError,
        )
    except requests.HTTPError as e:
        frappe.throw(
            f"Client returned HTTP {e.response.status_code}: {e.response.text[:200]}",
            ClientConnectionError,
        )
    except Exception as e:
        frappe.throw(
            f"Client communication error: {str(e)}",
            ClientConnectionError,
        )


# Headers a gateway signs with. Forwarded to the client unchanged; everything
# else — including anything AWS or this site added — is dropped.
RAW_PASS_THROUGH_HEADERS = (
    "x-hub-signature",
    "x-hub-signature-256",
    "x-twilio-signature",
    "x-msuite-signature",
    "content-type",
)


def post_raw_to_client(
    client_doc,
    endpoint: str,
    raw_body: bytes,
    source_headers: dict,
    query: str = "",
    timeout: int = 15,
) -> tuple[bool, str]:
    """Forward bytes to a client endpoint WITHOUT re-serialising them.

    The counterpart to `_post_to_client` for one specific case: relaying a
    third-party webhook whose body the client must authenticate itself.

    `_post_to_client` sends `json=payload`, which is correct for payloads this
    site composes. It is wrong here — the client recomputes an HMAC over the
    exact bytes the gateway signed, and re-serialising a dict reorders keys and
    changes whitespace, so the signature would never match and every relayed
    receipt would 401.

    Durability note: unlike the `MSuite Webhook Delivery` path (README §10),
    this is not backed by a delivery row. The caller is the AWS webhook
    consumer, where SQS already provides the retry and the dead-letter queue —
    a second retry ledger here would double-deliver receipts that the client's
    own terminal-state guards would then have to absorb.

    Returns `(ok, error_summary)`. Never logs auth headers or bodies — a
    webhook body carries recipient phone numbers and message ids.
    """
    forward = {k: v for k, v in (source_headers or {}).items() if k.lower() in RAW_PASS_THROUGH_HEADERS}
    forward.update(make_auth_headers(client_doc))

    url = f"{client_doc.client_url.rstrip('/')}/api/method/{endpoint}"
    if query:
        url += f"?{query}"

    try:
        resp = requests.post(url, headers=forward, data=raw_body, timeout=timeout)
    except Exception as e:
        logger.error(f"Raw forward to {client_doc.name} {endpoint} failed: {e}")
        return False, str(e)[:300]

    if resp.status_code != 200:
        logger.warning(
            f"Raw forward to {endpoint} returned {resp.status_code} ({client_doc.name})"
        )
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    return True, ""
