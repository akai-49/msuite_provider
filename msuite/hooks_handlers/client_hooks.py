"""MSuite Client document hooks."""

import frappe

from msuite.utils.cache import invalidate_relay_route_cache


def on_client_update(doc, method=None) -> None:
	"""Drop the webhook relay's routing cache for this client.

	Without this a deactivated client keeps receiving relayed webhooks for the
	rest of the TTL, and a renamed `client_code` keeps resolving to the old
	doc. Both are cheap to prevent and awkward to debug.

	The previous `client_code` is invalidated too — renaming it would
	otherwise strand the old key until it expires.
	"""
	invalidate_relay_route_cache(doc.client_code)

	before = doc.get_doc_before_save()
	if before and before.client_code and before.client_code != doc.client_code:
		invalidate_relay_route_cache(before.client_code)
