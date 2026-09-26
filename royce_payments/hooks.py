app_name = "royce_payments"
app_title = "Mobile Payments"
app_publisher = "Royce Technologies LTD"
app_description = "M-Pesa (Daraja) and other payment integrations for ERPNext"
app_email = "josphatkips@gmail.com"
app_license = "Proprietary"

required_apps = ["erpnext"]

doctype_js = {
	"Sales Invoice": "public/js/daraja_stk.js",
	"Sales Order": "public/js/daraja_stk.js",
}

# Public names for the Daraja callbacks. These URLs get registered at Safaricom (C2B can be
# registered only once per shortcode in production), so they must never change, even if the
# code behind them moves. They also must not contain "mpesa"/"safaricom"/"query"/etc.,
# which Daraja rejects. See ADR-020 in royce_ip.
override_whitelisted_methods = {
	"royce_payments.cb.stk_result": "royce_payments.daraja.callbacks.stk_result",
	"royce_payments.cb.c2b_confirm": "royce_payments.daraja.callbacks.c2b_confirm",
	"royce_payments.cb.c2b_validate": "royce_payments.daraja.callbacks.c2b_validate",
	"royce_payments.cb.status_result": "royce_payments.daraja.callbacks.status_result",
	"royce_payments.cb.status_timeout": "royce_payments.daraja.callbacks.status_timeout",
}

scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"royce_payments.daraja.tasks.recover_pending",
		],
	},
}
