// "Request M-Pesa payment" on submitted Sales Invoices and Sales Orders (STK Push).
// hooks.py loads this file for both doctypes, so it can run twice in one session: the
// guard keeps it from redeclaring itself or registering the handlers twice.

if (!window.royce_payments_stk) {
window.royce_payments_stk = {
	POLL_MS: 4000,
	POLL_LIMIT: 30, // two minutes; after that the scheduler finishes the request

	outstanding(frm) {
		if (frm.doctype === "Sales Invoice") return flt(frm.doc.outstanding_amount);
		const total = flt(frm.doc.rounded_total) || flt(frm.doc.grand_total);
		return Math.max(total - flt(frm.doc.advance_paid), 0);
	},

	async setup(frm) {
		if (frm.doc.docstatus !== 1 || frm.doc.currency !== "KES") return;
		if (frm.doctype === "Sales Order" && ["Closed", "On Hold", "Completed"].includes(frm.doc.status)) return;
		if (royce_payments_stk.outstanding(frm) < 1) return;

		const { message } = await frappe.db.get_value(
			"Daraja Account",
			{ company: frm.doc.company, enabled: 1 },
			"name"
		);
		if (!message || !message.name) return;

		frm.add_custom_button(__("Request M-Pesa Payment"), () => royce_payments_stk.prompt(frm), __("Create"));
	},

	prompt(frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Request M-Pesa payment"),
			fields: [
				{
					fieldname: "phone",
					fieldtype: "Data",
					options: "Phone",
					label: __("Customer's M-Pesa number"),
					default: frm.doc.contact_mobile || "",
					reqd: 1,
				},
				{
					fieldname: "amount",
					fieldtype: "Int",
					label: __("Amount (KES)"),
					default: Math.ceil(royce_payments_stk.outstanding(frm)),
					reqd: 1,
				},
			],
			primary_action_label: __("Send prompt"),
			primary_action(values) {
				frappe.call({
					method: "royce_payments.daraja.stk.request_payment",
					args: {
						reference_doctype: frm.doctype,
						reference_name: frm.doc.name,
						phone: values.phone,
						amount: values.amount,
					},
					freeze: true,
					callback: (r) => {
						dialog.hide();
						frappe.show_alert({
							message: __("Prompt sent. Waiting for the customer to enter their PIN..."),
							indicator: "blue",
						});
						royce_payments_stk.poll(frm, r.message.name, 0);
					},
				});
			},
		});
		dialog.show();
	},

	poll(frm, name, count) {
		if (count >= royce_payments_stk.POLL_LIMIT) {
			frappe.show_alert({
				message: __("Still waiting for M-Pesa. The payment will be recorded automatically when it arrives."),
				indicator: "orange",
			});
			return;
		}
		setTimeout(() => {
			frappe.call({
				method: "royce_payments.daraja.stk.get_status",
				args: { name },
				callback: ({ message }) => {
					if (message.status === "Paid") {
						frappe.show_alert({ message: __("M-Pesa payment received"), indicator: "green" });
						frm.reload_doc();
					} else if (["Failed", "Cancelled", "Needs Review"].includes(message.status)) {
						frappe.msgprint({
							title: __("M-Pesa payment {0}", [__(message.status)]),
							message: message.result_desc || "",
							indicator: message.status === "Needs Review" ? "orange" : "red",
						});
					} else {
						royce_payments_stk.poll(frm, name, count + 1);
					}
				},
			});
		}, royce_payments_stk.POLL_MS);
	},
};

frappe.ui.form.on("Sales Invoice", { refresh: (frm) => royce_payments_stk.setup(frm) });
frappe.ui.form.on("Sales Order", { refresh: (frm) => royce_payments_stk.setup(frm) });
}
