"""
Microsoft Graph change-notification subscriptions for Outlook mailboxes.

Push, layered on top of delta — never replacing it. A notification carries
no message content here: it only tells us "something changed in this
mailbox", and we respond by running the ordinary delta sync. That means

  * we never trust notification payloads as a data source,
  * a missed, duplicated, or out-of-order notification costs nothing —
    delta is idempotent and the poll cron is still running underneath,
  * the entire ingest path stays single-implementation.

The only thing subscriptions buy is latency: seconds instead of one cron
interval. That is also why every failure here is logged and swallowed
rather than raised — losing push must degrade to polling, not break
ingestion.

Graph caps mail subscriptions at 4230 minutes (under 3 days), so
`renew_graph_subscriptions()` runs daily and re-ups anything expiring
inside the next 24h.
"""
import secrets

import frappe
from frappe.utils import add_to_date, now_datetime

from msuite.constants import MSUITE_LOGGER_NAME, Platform
from msuite.services.mail.graph import GRAPH, GraphError, _request

logger = frappe.logger(MSUITE_LOGGER_NAME)

# Graph's documented ceiling for message resources is 4230 minutes. Ask for
# slightly less so a slow round-trip can't land past the limit and 400.
SUBSCRIPTION_MINUTES = 4200

# Re-up anything expiring within this window on the daily pass. Comfortably
# wider than one cron interval so a single missed run isn't fatal.
RENEW_WITHIN_HOURS = 24

# Only inbox. Sent Items still arrives via the delta sweep the notification
# triggers — subscribing to both would double the notification volume to
# trigger the exact same sync.
SUBSCRIPTION_RESOURCE = "/me/mailFolders('inbox')/messages"

_NOTIFICATION_PATH = "/api/method/msuite.api.v1.graph_webhook.receive_graph_notification"
_LIFECYCLE_PATH = "/api/method/msuite.api.v1.graph_webhook.receive_graph_lifecycle"


def _public_base_url() -> str:
    """Public HTTPS base Graph can reach us on.

    Graph validates the notification URL synchronously at subscription time
    and refuses anything it cannot reach over HTTPS — localhost included.
    Set explicitly when the site's own host isn't the public one:
        bench --site <site> set-config graph_notification_base_url https://...
    """
    configured = (frappe.conf.get("graph_notification_base_url") or "").strip().rstrip("/")
    if configured:
        return configured
    return (frappe.utils.get_url() or "").strip().rstrip("/")


def _access_token(ca):
    """Fresh token for a connected account, refreshing if near expiry."""
    from msuite.services.oauth.microsoft import refresh_token_fn

    if not ca.access_token or not ca.token_expiry or ca.token_expiry <= add_to_date(
        now_datetime(), seconds=60
    ):
        refresh_token_fn(ca.name)
        ca = frappe.get_doc("MSuite Connected Account", ca.name)
    return ca, ca.get_password("access_token")


def _client_state(ca_name: str) -> str:
    """Shared secret echoed back in every notification for this mailbox.

    The notification endpoint is public and unauthenticated by necessity, so
    this is the only thing distinguishing a real Graph callback from anyone
    who guesses the URL. Derived from the site's secret + the account name so
    it is stable across restarts without needing storage of its own.
    """
    import hashlib

    seed = f"{frappe.local.conf.get('encryption_key') or ''}:{ca_name}"
    return hashlib.sha256(seed.encode()).hexdigest()


def verify_client_state(ca_name: str, received: str) -> bool:
    import hmac

    return hmac.compare_digest(_client_state(ca_name), received or "")


def create_subscription(connected_account_name: str) -> dict | None:
    """Create (or replace) the Graph subscription for one mailbox.

    Returns the subscription dict, or None when skipped/failed — callers
    must treat None as "push unavailable, polling continues".
    """
    base = _public_base_url()
    if not base.startswith("https://"):
        logger.info(
            f"Graph subscription skipped for {connected_account_name}: "
            f"notification URL is not public HTTPS ({base or 'unset'})"
        )
        return None

    ca = frappe.get_doc("MSuite Connected Account", connected_account_name)
    if ca.platform != Platform.OUTLOOK or ca.status != "Active":
        return None

    try:
        ca, token = _access_token(ca)
    except Exception as e:
        logger.warning(f"Graph subscription token refresh failed for {ca.name}: {e}")
        return None

    expiry = add_to_date(now_datetime(), minutes=SUBSCRIPTION_MINUTES)
    body = {
        "changeType": "created",
        "notificationUrl": f"{base}{_NOTIFICATION_PATH}",
        "lifecycleNotificationUrl": f"{base}{_LIFECYCLE_PATH}",
        "resource": SUBSCRIPTION_RESOURCE,
        "expirationDateTime": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "clientState": _client_state(ca.name),
    }

    try:
        resp = _request("POST", f"{GRAPH}/subscriptions", token, json=body)
    except GraphError as e:
        logger.warning(f"Graph subscription request failed for {ca.name}: {e}")
        return None

    if resp.status_code not in (200, 201):
        # The overwhelmingly common cause is an unreachable notificationUrl.
        frappe.log_error(
            title=f"Graph subscription failed ({ca.account_id})",
            message=f"HTTP {resp.status_code}: {resp.text[:500]}\nnotificationUrl={body['notificationUrl']}",
        )
        return None

    data = resp.json()
    ca.db_set(
        {
            "graph_subscription_id": data.get("id"),
            "graph_subscription_expiry": _parse_graph_datetime(data.get("expirationDateTime")) or expiry,
            "subscribed_at": now_datetime(),
        },
        update_modified=False,
    )
    logger.info(f"Graph subscription {data.get('id')} created for {ca.account_id}")
    return data


def renew_subscription(connected_account_name: str) -> bool:
    """PATCH the expiry forward. Falls back to creating a fresh subscription
    when Graph has already dropped it (404)."""
    ca = frappe.get_doc("MSuite Connected Account", connected_account_name)
    if not ca.graph_subscription_id:
        return bool(create_subscription(connected_account_name))

    try:
        ca, token = _access_token(ca)
    except Exception as e:
        logger.warning(f"Graph renewal token refresh failed for {ca.name}: {e}")
        return False

    expiry = add_to_date(now_datetime(), minutes=SUBSCRIPTION_MINUTES)
    try:
        resp = _request(
            "PATCH",
            f"{GRAPH}/subscriptions/{ca.graph_subscription_id}",
            token,
            json={"expirationDateTime": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")},
        )
    except GraphError as e:
        logger.warning(f"Graph renewal failed for {ca.name}: {e}")
        return False

    if resp.status_code == 404:
        # Graph forgot it (expired past the grace window, or was deleted).
        ca.db_set({"graph_subscription_id": None, "graph_subscription_expiry": None}, update_modified=False)
        return bool(create_subscription(connected_account_name))

    if resp.status_code != 200:
        frappe.log_error(
            title=f"Graph subscription renewal failed ({ca.account_id})",
            message=f"HTTP {resp.status_code}: {resp.text[:500]}",
        )
        return False

    data = resp.json()
    ca.db_set(
        "graph_subscription_expiry",
        _parse_graph_datetime(data.get("expirationDateTime")) or expiry,
        update_modified=False,
    )
    return True


def delete_subscription(connected_account_name: str) -> None:
    """Best-effort teardown, so a disconnected mailbox stops notifying."""
    ca = frappe.get_doc("MSuite Connected Account", connected_account_name)
    if not ca.graph_subscription_id:
        return
    try:
        ca, token = _access_token(ca)
        _request("DELETE", f"{GRAPH}/subscriptions/{ca.graph_subscription_id}", token)
    except Exception as e:
        logger.info(f"Graph subscription delete failed for {ca.name} (ignored): {e}")
    ca.db_set(
        {"graph_subscription_id": None, "graph_subscription_expiry": None}, update_modified=False
    )


def renew_graph_subscriptions() -> None:
    """Daily cron. Renews anything expiring inside RENEW_WITHIN_HOURS and
    registers push for any active Outlook mailbox that has none yet."""
    cutoff = add_to_date(now_datetime(), hours=RENEW_WITHIN_HOURS)
    accounts = frappe.get_all(
        "MSuite Connected Account",
        filters={"platform": Platform.OUTLOOK, "status": "Active"},
        fields=["name", "graph_subscription_id", "graph_subscription_expiry"],
    )
    for acc in accounts:
        try:
            if not acc.graph_subscription_id:
                create_subscription(acc.name)
            elif not acc.graph_subscription_expiry or acc.graph_subscription_expiry <= cutoff:
                renew_subscription(acc.name)
        except Exception:
            frappe.log_error(
                title=f"Graph subscription maintenance failed ({acc.name})",
                message=frappe.get_traceback(),
            )


def _parse_graph_datetime(value: str | None):
    if not value:
        return None
    from datetime import datetime

    try:
        cleaned = value.replace("Z", "+00:00")
        # Graph returns sub-second precision that Frappe's Datetime rejects.
        return datetime.fromisoformat(cleaned).replace(tzinfo=None, microsecond=0)
    except (ValueError, TypeError):
        return None
