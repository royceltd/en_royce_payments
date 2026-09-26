// Copyright (c) 2026, Royce Technologies LTD and contributors
// For license information, please see license.txt

frappe.ui.form.on("Daraja Account", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Test Connection"), () => {
			frappe.call({
				method: "royce_payments.mobile_payments.doctype.daraja_account.daraja_account.test_connection",
				args: { daraja_account: frm.doc.name },
				freeze: true,
				callback: (r) => frappe.show_alert({ message: r.message, indicator: "green" }),
			});
		});

		frm.add_custom_button(__("Register Paybill/Till URLs"), () => {
			const warning =
				frm.doc.environment === "Production"
					? __(
							"In Production, Safaricom lets you register these URLs only once per shortcode. Changing them later needs Safaricom support. Continue?"
					  )
					: __("Register the callback URLs with the Daraja sandbox?");
			frappe.confirm(warning, () => {
				frappe.call({
					method: "royce_payments.daraja.c2b.register_urls",
					args: { daraja_account: frm.doc.name },
					freeze: true,
					callback: () => {
						frappe.show_alert({ message: __("URLs registered"), indicator: "green" });
						frm.reload_doc();
					},
				});
			});
		});
	},

	company(frm) {
		frm.set_value("receiving_account", null);
	},

	setup(frm) {
		frm.set_query("receiving_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, account_type: ["in", ["Bank", "Cash"]] },
		}));
	},
});
