"""
Live cross-bench demo: send Stephen's WhatsApp inbound from the provider.

  bench --site msuite.provider.v2 execute msuite._demo_stephen.send

Exercises the exact production code path:
  1. Build a Meta-shaped webhook payload (entry → changes → messages).
  2. Call `msuite.api.v1.webhook._forward_to_client(waba_id, payload)` —
     the production forwarder that fans Meta webhooks out to clients.
  3. The client receives at `msuite_workspace.api.v1.whatsapp.webhook.webhook`,
     which validates the X-MSuite-Provider-Key/Secret headers and bridges
     into the inbox dispatcher.

Nothing is mocked. Real HTTP between the two benches, real Channel Handle
+ Conversation + Channel Message rows land on the client side.
"""
from __future__ import annotations

import time

import frappe

from msuite.api.v1.webhook import _forward_to_client


# Resolved from the live Connected Account / WhatsApp Account state.
WABA_ID = "1242963091323644"
PHONE_NUMBER_ID = "1066230483241620"

STEPHEN = {
    "wa_id": "447700900042",   # UK mobile shape so it's clearly demo data
    "name": "Stephen Mitchell",
}


def send():
    """Forward one inbound text from Stephen to the client's inbox."""
    msg_id = f"wamid.STEPHEN_{int(time.time())}"
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": WABA_ID,
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "447700900000",
                        "phone_number_id": PHONE_NUMBER_ID,
                    },
                    "contacts": [{
                        "profile": {"name": STEPHEN["name"]},
                        "wa_id": STEPHEN["wa_id"],
                    }],
                    "messages": [{
                        "from": STEPHEN["wa_id"],
                        "id": msg_id,
                        "timestamp": str(int(time.time())),
                        "type": "text",
                        "text": {
                            "body": (
                                "Hi! I'm Stephen — saw your post about the "
                                "Q3 marketing workshop. Can you share the "
                                "agenda and pricing? Cheers."
                            ),
                        },
                    }],
                },
            }],
        }],
    }

    print(f"\n→ Forwarding to client. WABA={WABA_ID} msg_id={msg_id}")
    _forward_to_client(WABA_ID, payload)
    frappe.db.commit()
    print(f"  Done. Client should have a new Channel Message with "
          f"platform_message_id={msg_id}.")
