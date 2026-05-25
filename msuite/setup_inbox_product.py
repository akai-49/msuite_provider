"""
Inbox product setup — idempotent.

Creates the MSuite Product, its feature catalog, and 4 tiered plans
(Trial / Starter / Pro / Business). Then attaches Inbox-Pro to the
E2E test customer's active subscription so live testing can proceed.

Run with:
    bench --site msuite.provider.v2 execute path/to/setup_inbox_product.run

The script is safe to re-run — every create is guarded by an existence
check, every feature row is upserted by (parent, product_feature) pair.
"""
import frappe
from frappe.utils import today


# ── Product + feature catalog ────────────────────────────────────────────

PRODUCT_CODE = "INBOX"
PRODUCT_NAME = "Inbox"
PRODUCT_DESCRIPTION = (
    "Unified omnichannel inbox — reply, assign, and resolve customer "
    "conversations from WhatsApp, Instagram, and Facebook in one place."
)

# (feature_key, label, type, description, display_order)
FEATURES = [
    ("inbox", "Inbox", "Boolean",
     "Master switch — enables the Inbox UI and dispatcher.", 10),
    ("inbox_whatsapp", "WhatsApp Channel", "Boolean",
     "Route inbound WhatsApp messages into the Inbox.", 20),
    ("inbox_instagram", "Instagram DM Channel", "Boolean",
     "Route inbound Instagram DMs into the Inbox.", 30),
    ("inbox_facebook", "Facebook DM Channel", "Boolean",
     "Route inbound Facebook page DMs into the Inbox.", 40),
    ("inbox_email", "Email Channel", "Boolean",
     "Reserved — v2 release.", 50),
    ("inbox_website", "Website Widget Channel", "Boolean",
     "Reserved — v2 release.", 60),
    ("inbox_ai_assist", "AI Reply Assist", "Boolean",
     "AI-generated reply suggestions for agents.", 70),
]


# ── Plan tiers ───────────────────────────────────────────────────────────
# Each tier picks which features turn on. Pricing is monthly in INR.

PLANS = [
    {
        "plan_code": "INBOX-TRIAL",
        "plan_name": "Inbox Trial",
        "tier": "Trial",
        "monthly_rate": 0,
        "description": "14-day evaluation — all v1 channels + AI Assist unlocked.",
        "enabled": ["inbox", "inbox_whatsapp", "inbox_instagram",
                    "inbox_facebook", "inbox_ai_assist"],
    },
    {
        "plan_code": "INBOX-STARTER",
        "plan_name": "Inbox Starter",
        "tier": "Basic",
        "monthly_rate": 999,
        "description": "Single-channel inbox for WhatsApp. Manual routing.",
        "enabled": ["inbox", "inbox_whatsapp"],
    },
    {
        "plan_code": "INBOX-PRO",
        "plan_name": "Inbox Pro",
        "tier": "Pro",
        "monthly_rate": 2999,
        "description": "All v1 channels (WhatsApp, IG DM, FB DM) with rules and routing.",
        "enabled": ["inbox", "inbox_whatsapp", "inbox_instagram", "inbox_facebook"],
    },
    {
        "plan_code": "INBOX-BIZ",
        "plan_name": "Inbox Business",
        "tier": "Business",
        "monthly_rate": 6999,
        "description": "Top tier — adds AI Reply Assist on top of Pro.",
        "enabled": ["inbox", "inbox_whatsapp", "inbox_instagram",
                    "inbox_facebook", "inbox_ai_assist"],
    },
]


# ── Test wiring ──────────────────────────────────────────────────────────

TEST_CUSTOMER = "E2E Client Co"
TEST_PLAN_TO_ATTACH = "INBOX-PRO"  # Gives E2E all v1 channels for testing.


# ── Entrypoints ──────────────────────────────────────────────────────────


def run():
    """Full setup — product, features, plans, test attach. Idempotent."""
    print(f"\n=== Inbox product setup ({today()}) ===\n")
    product = ensure_product()
    feature_map = ensure_product_features(product)
    plans = [ensure_plan(p, feature_map) for p in PLANS]
    attach_test_plan(plans)
    push_to_test_client()
    print("\nDone.")


# ── Steps ────────────────────────────────────────────────────────────────


def ensure_product() -> str:
    """Create or update the MSuite Product 'INBOX'."""
    if frappe.db.exists("MSuite Product", PRODUCT_CODE):
        doc = frappe.get_doc("MSuite Product", PRODUCT_CODE)
        changed = False
        if doc.product_name != PRODUCT_NAME:
            doc.product_name = PRODUCT_NAME
            changed = True
        if doc.description != PRODUCT_DESCRIPTION:
            doc.description = PRODUCT_DESCRIPTION
            changed = True
        if not doc.is_active:
            doc.is_active = 1
            changed = True
        if changed:
            doc.save(ignore_permissions=True)
            print(f"  Updated MSuite Product {PRODUCT_CODE}")
        else:
            print(f"  MSuite Product {PRODUCT_CODE} already up-to-date")
        return doc.name

    doc = frappe.get_doc({
        "doctype": "MSuite Product",
        "product_code": PRODUCT_CODE,
        "product_name": PRODUCT_NAME,
        "description": PRODUCT_DESCRIPTION,
        "is_active": 1,
    })
    doc.insert(ignore_permissions=True)
    print(f"  Created MSuite Product {PRODUCT_CODE}")
    return doc.name


def ensure_product_features(product_name: str) -> dict[str, str]:
    """Ensure all FEATURES rows exist on the product. Returns key → row name."""
    product = frappe.get_doc("MSuite Product", product_name)
    existing_by_key = {}
    for row in product.features:
        existing_by_key[row.feature_key] = row.name

    seen_keys = set(existing_by_key.keys())
    appended = 0
    for key, label, ftype, desc, order in FEATURES:
        if key in seen_keys:
            # Update label/desc if they drifted.
            row = next(r for r in product.features if r.feature_key == key)
            if row.feature_label != label or row.description != desc or row.display_order != order:
                row.feature_label = label
                row.description = desc
                row.display_order = order
        else:
            product.append("features", {
                "feature_key": key,
                "feature_label": label,
                "feature_type": ftype,
                "description": desc,
                "display_order": order,
            })
            appended += 1

    product.save(ignore_permissions=True)
    if appended:
        print(f"  Added {appended} feature(s) to {product_name}")

    # Rebuild the key → row name map after save (new rows now have names).
    product.reload()
    return {row.feature_key: row.name for row in product.features}


def ensure_plan(spec: dict, feature_map: dict[str, str]) -> str:
    """Create or update one MSuite Plan with its features + pricing row."""
    plan_code = spec["plan_code"]
    enabled_keys = set(spec["enabled"])

    if frappe.db.exists("MSuite Plan", plan_code):
        doc = frappe.get_doc("MSuite Plan", plan_code)
        action = "Updated"
    else:
        doc = frappe.get_doc({"doctype": "MSuite Plan", "plan_code": plan_code})
        action = "Created"

    doc.plan_name = spec["plan_name"]
    doc.product = PRODUCT_CODE
    doc.tier = spec["tier"]
    doc.description = spec["description"]
    doc.is_active = 1

    # Rebuild features deterministically — drop existing, add per spec.
    doc.features = []
    for feature_key, row_name in feature_map.items():
        doc.append("features", {
            "product_feature": row_name,
            "is_enabled": 1 if feature_key in enabled_keys else 0,
        })

    # Pricing — add Monthly row if missing.
    has_monthly = any(p.billing_interval == "Monthly" for p in (doc.pricing or []))
    if not has_monthly:
        doc.append("pricing", {
            "billing_interval": "Monthly",
            "rate": spec["monthly_rate"],
        })
    else:
        for p in doc.pricing:
            if p.billing_interval == "Monthly" and p.rate != spec["monthly_rate"]:
                p.rate = spec["monthly_rate"]

    doc.save(ignore_permissions=True)

    # Auto-create ERPNext Item + Subscription Plan for paid tiers.
    # Trial-tier plans skip billing artifacts (free evaluation).
    if doc.tier != "Trial":
        from msuite.msuite_plan.doctype.msuite_plan.plan_activation import (
            create_items_and_subscription_plans,
        )
        create_items_and_subscription_plans(doc)
        doc.save(ignore_permissions=True)

    print(f"  {action} MSuite Plan {plan_code} "
          f"(₹{spec['monthly_rate']}/mo, {len(enabled_keys)} feature(s) on)")
    return doc.name


def attach_test_plan(plans: list[str]) -> None:
    """Add INBOX-PRO to the test customer's active subscription."""
    if not frappe.db.exists("Customer", TEST_CUSTOMER):
        print(f"  ! Test customer {TEST_CUSTOMER!r} not found — skipping attach")
        return

    # The MSuite Plan's pricing row auto-creates an ERPNext Subscription Plan.
    # Fetch it via the pricing child table.
    sub_plan = frappe.db.get_value(
        "MSuite Plan Pricing",
        {"parent": TEST_PLAN_TO_ATTACH, "billing_interval": "Monthly"},
        "subscription_plan",
    )
    if not sub_plan:
        print(f"  ! No Subscription Plan auto-created for {TEST_PLAN_TO_ATTACH} — "
              f"check plan_activation.sync_pricing_rules")
        return

    subs = frappe.get_all(
        "Subscription",
        filters={"party_type": "Customer", "party": TEST_CUSTOMER, "status": "Active"},
        pluck="name",
    )
    if not subs:
        print(f"  ! No active Subscription for {TEST_CUSTOMER} — skipping attach")
        return

    sub_doc = frappe.get_doc("Subscription", subs[0])
    if any(row.plan == sub_plan for row in sub_doc.plans):
        print(f"  Subscription {sub_doc.name} already has {sub_plan}")
        return

    sub_doc.append("plans", {"plan": sub_plan, "qty": 1})
    sub_doc.save(ignore_permissions=True)
    print(f"  Attached {sub_plan} to {sub_doc.name} for {TEST_CUSTOMER}")


def push_to_test_client() -> None:
    """Invalidate cache and push fresh plan data to E2E-CLIENT."""
    from msuite.services.client_service import build_client_plan_data, push_plan_to_client
    from msuite.utils.cache import invalidate_entitlement_cache

    if not frappe.db.exists("Customer", TEST_CUSTOMER):
        return

    invalidate_entitlement_cache(TEST_CUSTOMER)
    plan_data = build_client_plan_data(TEST_CUSTOMER)
    print(f"\n  Built plan_data for {TEST_CUSTOMER}: plan={plan_data['plan_name']!r}, "
          f"{len(plan_data['features'])} feature(s)")
    inbox_features = {k: v for k, v in plan_data["features"].items() if k.startswith("inbox")}
    for k, v in sorted(inbox_features.items()):
        print(f"    {k:20s} enabled={v['enabled']} limit={v.get('limit')}")

    client = frappe.db.get_value("MSuite Client", {"customer": TEST_CUSTOMER}, "name")
    if not client:
        print(f"  ! No MSuite Client for {TEST_CUSTOMER}")
        return

    client_doc = frappe.get_doc("MSuite Client", client)
    if client_doc.status != "Active":
        print(f"  ! Client {client} is {client_doc.status} — skipping push")
        return

    result = push_plan_to_client(client_doc, plan_data)
    print(f"  Pushed plan to {client}: {result}")
