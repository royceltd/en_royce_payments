// Copyright (c) 2026, Royce Technologies LTD and contributors
// For license information, please see license.txt

frappe.ui.form.on("Daraja C2B Payment", {
	refresh(frm) {
		if (frm.doc.status !== "Needs Review" || frm.doc.payment_entry) return;

		frm.add_custom_button(__("Post Payment"), () => {
			const fields = [
				{ fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("Customer"), reqd: 1 },
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
			if (!frm.doc.verified) {
				fields.push({
					fieldname: "confirmed_on_statement",
					fieldtype: "Check",
					label: __("I have confirmed receipt {0} of {1} on the M-Pesa statement", [
						frm.doc.trans_id,
						format_currency(frm.doc.amount, "KES"),
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
						method: "royce_payments.daraja.c2b.post_reviewed",
						args: { name: frm.doc.name, ...values },
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
