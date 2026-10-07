// Copyright (c) 2026, Royce Technologies LTD and contributors
// For license information, please see license.txt

frappe.ui.form.on("Daraja STK Request", {
	refresh(frm) {
		const doc = frm.doc;
		if (!["Needs Review", "Received"].includes(doc.status) || doc.payment_entry || doc.invoice_name) return;

		frm.add_custom_button(__("Post Payment"), () => {
			const fields = [
				{
					fieldname: "customer",
					fieldtype: "Link",
					options: "Customer",
					label: __("Customer"),
					default: doc.customer,
					reqd: 1,
				},
				{
					fieldname: "sales_invoice",
					fieldtype: "Link",
					options: "Sales Invoice",
					label: __("Against Sales Invoice (optional)"),
					get_query: () => ({
						filters: { docstatus: 1, outstanding_amount: [">", 0], customer: dialog.get_value("customer") },
					}),
				},
			];
			if (!doc.mpesa_receipt) {
				fields.push({
					fieldname: "mpesa_receipt",
					fieldtype: "Data",
					label: __("M-Pesa receipt number (from the statement)"),
					reqd: 1,
				});
			}
			if (!doc.verified) {
				fields.push({
					fieldname: "confirmed_on_statement",
					fieldtype: "Check",
					label: __("I have confirmed this payment of {0} from {1} on the M-Pesa statement", [
						format_currency(doc.callback_amount || doc.amount, "KES"),
						doc.phone,
					]),
					reqd: 1,
				});
			}
			const dialog = new frappe.ui.Dialog({
				title: __("Post M-Pesa payment"),
				fields,
				primary_action_label: __("Post"),
				primary_action(values) {
					frappe.call({
						method: "royce_payments.daraja.stk.post_reviewed",
						args: { name: doc.name, ...values },
						freeze: true,
						callback: () => {
							dialog.hide();
							frm.reload_doc();
						},
					});
				},
			});
			dialog.show();
		});
	},
});
