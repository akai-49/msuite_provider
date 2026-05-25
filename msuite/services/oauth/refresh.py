"""
Provider-side token refresh — keeps customer tokens alive proactively.

Runs daily via `hooks.py::scheduler_events.daily`. For each Active
Connected Account that is either approaching `token_expiry` OR hasn't
been refreshed in the platform's safety window, we attempt a refresh
via the registered platform refresher (`services.oauth._TOKEN_REFRESHERS`).

Outcome per account (recorded on the doctype + pushed to the client):

  • Refresh succeeded — `last_refreshed = now`,
                        `last_refresh_attempt_at = now`,
                        client receives the new access_token via push.
  • Refresh failed     — `last_refresh_attempt_at = now`,
                        `last_refresh_error = <reason>`. After 24h of
                        repeated failures the account is flipped to
                        `status="Expired"` and `needs_reauth=1`, and
                        the client site is notified once via the
                        existing push channel so its `Social Account`
                        rows can surface a "Reconnect" prompt.
  • No refresher       — non-refreshable platforms (e.g., legacy
                        LinkedIn apps without refresh tokens) get an
                        admin notification 7 days before expiry; the
                        account flips to needs-reauth when the token
                        actually expires.

Throttling: `last_refresh_attempt_at` gates re-attempts so we don't
hammer a broken integration every cron tick.
"""
from __future__ import annotations

import frappe
from frappe.utils import add_days, get_datetime, now, now_datetime

from msuite.constants import (
    ConnectedAccountStatus,
    MSUITE_LOGGER_NAME,
)

logger = frappe.logger(MSUITE_LOGGER_NAME)


# How close to expiry before we try to refresh. 24h is enough buffer for
# transient failures; Meta long-lived tokens with 60-day lifetime get
# touched ~50 days in (when `token_expiry` falls inside this window).
REFRESH_WINDOW_HOURS = 24

# Stale-token safety net: even if `token_expiry` is unset (e.g. some
# providers don't return it), force a refresh after this many days
# since the last successful refresh. Meta's documented practice.
MAX_DAYS_SINCE_REFRESH = 50

# Re-attempt throttle. Daily cron makes this effectively a no-op, but
# we keep it as a guard for sites that bump the cron to hourly later
# OR for manual re-runs after fixing a broken integration.
MIN_HOURS_BETWEEN_ATTEMPTS = 12

# Give-up window: how long a refresh can keep failing before we declare
# the integration broken and ask the customer to reconnect. At daily
# cron cadence, 72h ≈ 3 retries — enough headroom for transient outages
# (Meta maintenance, DNS hiccups) without leaving a dead token alive
# for a whole week.
HOURS_BEFORE_MARKING_REAUTH = 72

# Admin-notification window for non-refreshable platforms.
NOTIFICATION_WINDOW_DAYS = 7


def refresh_all_tokens() -> dict:
    """Hourly cron entry point. Returns a summary for log aggregation."""
    from msuite.services.oauth import _TOKEN_REFRESHERS

    candidates = _find_candidates()
    summary = {
        "refreshed": 0,
        "failed":    0,
        "notified":  0,
        "skipped":   0,
        "reauth":    0,
    }

    for account in candidates:
        # Throttle: if we tried recently and failed, wait it out.
        if _attempted_recently(account):
            summary["skipped"] += 1
            continue

        refresher = _TOKEN_REFRESHERS.get(account.platform)
        if not refresher:
            _notify_admin_if_near_expiry(account)
            summary["notified"] += 1
            continue

        try:
            refresher(account.name)
            _record_success(account)
            _push_refreshed_token(account)
            summary["refreshed"] += 1
        except Exception as e:
            outcome = _record_failure(account, str(e)[:500])
            summary["failed"] += 1
            if outcome == "marked_reauth":
                summary["reauth"] += 1

    frappe.db.commit()
    logger.info(f"Token refresh summary: {summary}")
    return summary


# ── Candidate selection ──────────────────────────────────────────────


def _find_candidates() -> list:
    """All Active accounts that either expire soon or haven't been
    refreshed in a long time. Both criteria catch different failure
    modes — Meta hands out 60-day tokens with a known expiry, while
    some providers leave `token_expiry` blank and we have to infer
    staleness from `last_refreshed`."""
    expiry_cutoff = add_days(now(), 1)               # tokens within 24h of expiry
    stale_cutoff  = add_days(now(), -MAX_DAYS_SINCE_REFRESH)

    return frappe.db.sql(
        """
        SELECT name, client, platform, account_id, display_name,
               token_expiry, last_refreshed, last_refresh_attempt_at,
               needs_reauth
        FROM `tabMSuite Connected Account`
        WHERE status = %(active)s
          AND IFNULL(needs_reauth, 0) = 0
          AND (
              (token_expiry IS NOT NULL AND token_expiry <= %(expiry_cutoff)s)
              OR (token_expiry IS NULL AND
                  (last_refreshed IS NULL OR last_refreshed <= %(stale_cutoff)s))
          )
        """,
        {
            "active":        ConnectedAccountStatus.ACTIVE,
            "expiry_cutoff": expiry_cutoff,
            "stale_cutoff":  stale_cutoff,
        },
        as_dict=True,
    )


def _attempted_recently(account) -> bool:
    if not account.last_refresh_attempt_at:
        return False
    age = now_datetime() - get_datetime(account.last_refresh_attempt_at)
    return age.total_seconds() < MIN_HOURS_BETWEEN_ATTEMPTS * 3600


# ── Outcome recording ────────────────────────────────────────────────


def _record_success(account) -> None:
    """The platform refresher has already updated access_token +
    token_expiry. We just clear error state + bookkeeping fields."""
    frappe.db.set_value(
        "MSuite Connected Account", account.name,
        {
            "last_refreshed":            now_datetime(),
            "last_refresh_attempt_at":   now_datetime(),
            "last_refresh_error":        "",
            "needs_reauth":              0,
        },
        update_modified=False,
    )
    logger.info(f"Refreshed {account.platform} token for {account.display_name}")


def _record_failure(account, error: str) -> str:
    """Record the attempt; flip to `needs_reauth` once we've been
    failing for long enough that human action is the only path forward.

    Returns:
        "recorded"      — attempt logged, still in the retry window.
        "marked_reauth" — failure window exceeded; account is now
                          marked for re-auth and the client is notified.
    """
    updates = {
        "last_refresh_attempt_at": now_datetime(),
        "last_refresh_error":      error,
    }

    if _failure_window_exceeded(account):
        updates["status"]       = ConnectedAccountStatus.EXPIRED
        updates["needs_reauth"] = 1
        outcome = "marked_reauth"
    else:
        outcome = "recorded"

    frappe.db.set_value(
        "MSuite Connected Account", account.name, updates,
        update_modified=False,
    )

    if outcome == "marked_reauth":
        # Pull a fresh doc so the push payload reflects the new state.
        ca = frappe.get_doc("MSuite Connected Account", account.name)
        _notify_client_of_reauth(ca)
        logger.warning(
            f"Marked {account.platform} account {account.display_name} "
            f"for re-auth after sustained refresh failure: {error}"
        )
    else:
        logger.error(
            f"Refresh failed for {account.name} (will retry): {error}"
        )

    return outcome


def _failure_window_exceeded(account) -> bool:
    """True when the FIRST failed attempt is older than the give-up
    window. We use `last_refreshed` as a proxy for "last known good";
    if the account has never been refreshed, fall back to
    `last_refresh_attempt_at` which means this is at least the second
    failed attempt."""
    last_good = account.last_refreshed or account.last_refresh_attempt_at
    if not last_good:
        return False
    age = now_datetime() - get_datetime(last_good)
    return age.total_seconds() >= HOURS_BEFORE_MARKING_REAUTH * 3600


# ── Notifications ────────────────────────────────────────────────────


def _push_refreshed_token(account) -> None:
    """Push the new access_token to the customer's site so its
    `Social Account` / `WhatsApp Account` etc. rows get the fresh
    credentials without the customer having to do anything."""
    from msuite.services.oauth.base import push_account_to_client
    from msuite.msuite_client.doctype.msuite_client.msuite_client import (
        _PUSH_PAYLOAD_BUILDERS,
    )

    builder = _PUSH_PAYLOAD_BUILDERS.get(account.platform)
    if not builder:
        return

    ca = frappe.get_doc("MSuite Connected Account", account.name)
    token = ca.get_password("access_token") if ca.access_token else ""
    payload = builder(ca, token)
    if payload:
        push_account_to_client(account.client, account.platform, payload)


def _notify_client_of_reauth(ca) -> None:
    """Tell the customer site that this account needs human attention.

    Goes through the dedicated state-change channel (not the
    credentials push) so the receiver can update flags without
    expecting a token. The customer site flips its local
    `Social Account.needs_reauth` and surfaces a Reconnect CTA.
    """
    from msuite.services.client_service import push_account_state_to_client

    client_doc = frappe.get_doc("MSuite Client", ca.client)
    if client_doc.status != "Active":
        return

    push_account_state_to_client(
        client_doc,
        platform=ca.platform,
        account_id=ca.account_id,
        state="needs_reauth",
        error=ca.last_refresh_error or "",
    )


def _notify_admin_if_near_expiry(account) -> None:
    """Email System Managers once when a non-refreshable token is
    about to expire. Cache key dedupes so the cron doesn't spam every
    hour for 7 days straight."""
    if not account.token_expiry:
        return
    days_left = (get_datetime(account.token_expiry) - now_datetime()).days
    if days_left > NOTIFICATION_WINDOW_DAYS or days_left < 0:
        return

    cache_key = f"msuite:token_notify:{account.name}"
    if frappe.cache.get_value(cache_key):
        return

    recipients = _get_admin_emails()
    if not recipients:
        return

    frappe.sendmail(
        recipients=recipients,
        subject=f"MSuite: {account.platform} token expiring in {days_left} days",
        message=(
            f"The {account.platform} token for <b>{account.display_name}</b> "
            f"(client: {account.client}) expires in {days_left} day(s).<br><br>"
            f"This platform does not support automatic refresh. "
            f"Please ask the customer to reconnect via the Connections page."
        ),
    )
    frappe.cache.set_value(cache_key, "1", expires_in_sec=86400 * 3)
    logger.info(
        f"Sent token-expiry email for {account.name} ({days_left} days left)"
    )


def _get_admin_emails() -> list[str]:
    return frappe.get_all(
        "Has Role",
        filters={"role": "System Manager", "parenttype": "User"},
        pluck="parent",
        limit=5,
    )
