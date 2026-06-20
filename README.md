# MSuite Provider

MSuite is a Frappe/ERPNext application that serves as the central provider for SaaS subscription, entitlement management, and multi-tenant integrations. It manages product catalogs, subscription plans, customer grants, and OAuth/webhook connections for downstream client workspaces.

## Features

* **Product & Plan Management**: Define products (e.g., WhatsApp, Social Post, Email, Ads, Inbox) and tiered subscription plans (Trial, Basic, Pro, Business, Enterprise) with granular feature toggles and quantitative limits.
* **Bundling**: Package multiple plans into a single purchasable bundle (e.g., Marketing Combo).
* **Entitlement Resolution**: Real-time, Redis-cached resolution of customer access rights, handling multi-plan merging, limits, and complimentary plan grants.
* **Subscription Lifecycle**: Tight integration with ERPNext Subscriptions. Automates trial provisioning, paid grants, plan upgrades/downgrades, and cancellation revocations.
* **OAuth & Connections**: Centralized OAuth 2.0 flow and Meta Embedded Signup management for WhatsApp, Facebook, Instagram, LinkedIn, X (Twitter), TikTok, and Google/YouTube.
* **Multi-Tenant Sync**: Automatically pushes updated plan data, credentials, and state changes to downstream Frappe client workspaces.
* **Payment Orchestration**: Creates Payment Requests and handles webhook callbacks (Stripe, Razorpay, Hitpay) with HMAC verification.
* **Coupons**: Usage limits and single-use enforcement for promotional discounts.

## Architecture

The system revolves around separating billing (ERPNext) from access rights (MSuite Grants):

1. **Definitions**: `MSuite Product` defines features. `MSuite Plan` maps those features to a tier and pricing.
2. **Subscriptions**: An ERPNext `Subscription` is created for a customer.
3. **Grants**: On subscription activation, `MSuite Customer Grant` records are generated. These act as the source of truth for entitlements.
4. **Entitlements**: `entitlement_service.py` computes the active features by merging all active grants, resolving limits (max wins) and toggles.
5. **Clients**: The resolved plan data and OAuth credentials are pushed to the customer's remote workspace (`MSuite Client`).

## Directory Structure

```text
msuite/
├── api/                    # Public Whitelisted APIs (v1)
│   ├── v1/auth.py          # OAuth and Embedded Signup endpoints
│   ├── v1/bundle.py        # Bundle queries
│   ├── v1/coupon.py        # Coupon validation
│   ├── v1/entitlement.py   # Feature access checks
│   ├── v1/payment.py       # Payment processing & webhooks
│   ├── v1/plan.py          # Plan catalog and comparison
│   ├── v1/subscription.py  # Subscription lifecycle API
│   └── v1/webhook.py       # Meta/Platform webhook ingestion and routing
├── hooks_handlers/         # ERPNext Event Hooks (Invoices, Subscriptions)
├── msuite_*/               # DocTypes (App, Bundle, Client, Grant, Plan, Product, etc.)
├── scheduled_tasks/        # Daily CRON jobs (Sync, Expiry, Refresh, Reconciliation)
├── seeds/                  # Seed data for Products (Ads, Email, Inbox, Social, WhatsApp)
├── services/               # Core Business Logic
│   ├── bundle_service.py
│   ├── client_service.py   # Downstream workspace sync
│   ├── entitlement_service.py
│   ├── grant_service.py
│   ├── oauth/              # Platform OAuth handlers (Google, LinkedIn, Meta, Twitter)
│   ├── payment_service.py
│   ├── subscription_service.py
│   └── trial_service.py
├── tests/                  # Unit and E2E Tests
├── utils/                  # Redis Cache and Validation Utilities
└── www/                    # Public Web Pages (e.g., MSuite Connect UI)
```

## Installation

MSuite requires **Frappe** and **ERPNext** (v15/v16).

```bash
# Get the app
bench get-app msuite <repository-url>

# Install on a specific site
bench --site <site-name> install-app msuite
```

Upon installation, `msuite/install.py` will automatically seed the initial products, plans, item groups, and roles.

## Configuration

1. **Products & Plans**: Configure through the Frappe Desk under the **MSuite** workspace.
2. **Platform Integrations**: Create `MSuite App` records for each supported platform (Meta WhatsApp, Meta Social, Google, etc.) with the appropriate App IDs and App Secrets.
3. **Payment Gateways**: Configure standard ERPNext Payment Gateways (Stripe, Razorpay) and ensure webhook secrets are set.

## Environment Variables / Site Config

Payment webhook secrets can be defined in the `site_config.json`:

```json
{
  "razorpay_webhook_secret": "your_secret",
  "stripe_webhook_secret": "your_secret",
  "hitpay_webhook_secret": "your_secret"
}
```
*(Alternatively, they are read from the ERPNext Payment Gateway Account document).*

## API Integrations

### Webhooks
* **Meta**: Receives WhatsApp, Facebook, and Instagram webhooks, verifies signatures, and dispatches payloads to the respective downstream client workspaces based on the WABA/Page ID.
* **Payments**: Handles callbacks to allocate payments to ERPNext Sales Invoices and reactivate grace-period subscriptions.

### OAuth
* **Meta WhatsApp**: Custom Embedded Signup flow.
* **Meta Social/Ads**: Facebook Login for Business.
* **LinkedIn, Twitter, Google**: Standard OAuth 2.0 PKCE / Auth Code flows.
* Automatically refreshes long-lived tokens and pushes updates to client instances.

## Development Workflow

### Testing
MSuite includes an extensive test suite.

Run the unit tests:
```bash
bench --site <site-name> run-tests --app msuite
```

Run the full End-to-End lifecycle test:
```bash
bench --site <site-name> execute msuite.tests.test_full_e2e.run
# or
bench --site <site-name> execute msuite.run_full_tests.run_all
```

### Seeding Data
Seed files are located in `msuite/seeds/`. If you need to reseed or patch an existing environment, you can run the patch files or the setup scripts:
```bash
bench --site <site-name> execute msuite.setup_inbox_product.run
```

## Deployment

Standard Frappe app deployment:
```bash
bench update --patch
```
The system uses Frappe patches (e.g., `msuite.patches.v1_0.reseed_social_post_product`) to safely migrate catalog definitions without breaking existing grants.
