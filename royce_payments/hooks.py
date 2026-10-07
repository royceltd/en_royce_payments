app_name = "royce_payments"
app_title = "Mobile Payments"
app_publisher = "Royce Technologies LTD"
app_description = "M-Pesa (Daraja) and other payment integrations for ERPNext"
app_email = "josphatkips@gmail.com"
app_license = "Proprietary"

required_apps = ["erpnext"]

# Shared M-Pesa dialogs, used by the form scripts below and by the POS screen.
app_include_js = "/assets/royce_payments/js/mpesa.js"

doctype_js = {
	"Sales Invoice": "public/js/daraja_stk.js",
	"POS Invoice": "public/js/daraja_stk.js",
	"Sales Order": "public/js/daraja_stk.js",
}

# M-Pesa on the POS screen's payment step.
page_js = {"point-of-sale": "public/js/pos_mpesa.js"}

# M-Pesa applied to POS invoices: checked on submit, settled on submit, freed on cancel/delete.
_invoice_events = {
	"before_submit": "royce_payments.daraja.invoice.before_submit",
	"on_submit": "royce_payments.daraja.invoice.on_submit",
	"on_cancel": "royce_payments.daraja.invoice.on_cancel",
	"on_trash": "royce_payments.daraja.invoice.on_trash",
}
doc_events = {
	"Sales Invoice": _invoice_events,
	"POS Invoice": _invoice_events,
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
		# Every minute: a paid prompt should show on the till within a minute, not five.
		# Each request backs off on its own (stk.retry_after_seconds).
		"* * * * *": [
			"royce_payments.daraja.tasks.recover_pending",
		],
	},
}

# A fresh test site needs ERPNext setup completed first.
before_tests = "royce_payments.testing.before_tests"
