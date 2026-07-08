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
    "daily": [
        "msuite.scheduled_tasks.daily.expire_trial_grants",
        "msuite.scheduled_tasks.daily.reconcile_customer_groups",
        "msuite.scheduled_tasks.daily.sync_active_clients",
        "msuite.scheduled_tasks.daily.refresh_expiring_tokens",
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

