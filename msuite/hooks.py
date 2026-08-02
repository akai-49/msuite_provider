app_name = "msuite"
app_title = "MSuite"
app_publisher = "MSuite"
app_description = "SaaS subscription and entitlement management"
app_email = "dev@msuite.com"
app_license = "MIT"
app_version = "1.0.0"

required_apps = ["erpnext"]

doc_events = {
    "Subscription": {
        "after_insert": [
            "msuite.hooks_handlers.subscription_hooks.on_subscription_created"
        ],
        "on_update": [
            "msuite.hooks_handlers.subscription_hooks.on_subscription_update"
        ],
    },
    "Sales Invoice": {
        "on_submit": [
            "msuite.hooks_handlers.invoice_hooks.on_invoice_submit"
        ],
    },
}

scheduler_events = {
    "cron": {
        # Webhook forwards that failed transiently (client down / 5xx)
        # retry on exponential backoff recorded on MSuite Webhook Delivery.
        "*/5 * * * *": [
            "msuite.api.v1.webhook.retry_pending_webhook_deliveries",
        ],
    },
    "hourly": [
        # Re-run failed per-Page webhook subscriptions (e.g. after a
        # token refresh) so DM/comment events resume without re-OAuth.
        "msuite.services.oauth.meta_social.retry_failed_page_subscriptions",
    ],
    "daily": [
        "msuite.scheduled_tasks.daily.expire_trial_grants",
        "msuite.scheduled_tasks.daily.reconcile_customer_groups",
        "msuite.scheduled_tasks.daily.sync_active_clients",
        "msuite.scheduled_tasks.daily.refresh_expiring_tokens",
        # Gmail users.watch registrations expire after 7 days — renew daily
        # so Pub/Sub push ingestion keeps flowing (no-op without
        # gmail_pubsub_topic in site config).
        "msuite.api.v1.gmail_relay.renew_gmail_watches",
    ]
}

fixtures = [
    {
        "doctype": "MSuite Product",
        "filters": [["is_active", "=", 1]],
    },
    {
        "doctype": "MSuite Plan",
        "filters": [["is_active", "=", 1]],
    },
    {
        "doctype": "MSuite Bundle",
        "filters": [["is_active", "=", 1]],
    },
]

after_install = "msuite.install.after_install"

has_permission = {
    "MSuite Customer Grant": "msuite.permissions.has_permission"
}

doctype_js = {
    "Customer": "public/js/customer.js"
}

before_request = ["msuite.hooks_handlers.proxy.before_request"]

