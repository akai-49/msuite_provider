app_name = "msuite"
app_title = "MSuite"
app_publisher = "MSuite"
app_description = "SaaS subscription and entitlement management"
app_email = "dev@msuite.com"
app_license = "MIT"
app_version = "1.0.0"
app_logo_url = "/assets/msuite/images/msuite-logo.png"
app_icon_url = "/assets/msuite/images/msuite-logo.png"

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
    "MSuite Client": {
        "on_update": [
            "msuite.hooks_handlers.client_hooks.on_client_update"
        ],
        "on_trash": [
            "msuite.hooks_handlers.client_hooks.on_client_update"
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
        # Graph mail subscriptions expire after ~3 days (4230 min max) —
        # renew daily and register push for any Outlook mailbox that has
        # none yet. No-op unless the notification URL is public HTTPS;
        # delta polling covers ingestion either way.
        "msuite.services.mail.graph_subscriptions.renew_graph_subscriptions",
        # Proactively notify customers and admins when subscriptions approach expiry/renewal
        "msuite.scheduled_tasks.daily.notify_expiring_subscriptions",
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

