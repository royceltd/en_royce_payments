// M-Pesa buttons on Sales Invoice, POS Invoice and Sales Order forms. Dialogs live in mpesa.js.
//
// - Submitted Sales Invoice / Sales Order: send a prompt; the payment is posted as a Payment
//   Entry. On a Sales Invoice, a payment that already arrived can be posted against it too.
// - Unsubmitted POS-type invoice: send a prompt or pick a payment that arrived; it goes on the
//   invoice's M-Pesa payment row and is recorded when the invoice is submitted.
//
// hooks.py loads this file for three doctypes, so it can run more than once in a session:
// the guard keeps it from registering the handlers twice.

if (!window.royce_payments_stk) {
window.royce_payments_stk = {
	modes_cache: {},

	async modes(company) {
		if (!(company in this.modes_cache)) {
			const { message } = await frappe.call({
				method: "royce_payments.daraja.invoice.get_modes",
				args: { company },
			});
			this.modes_cache[company] = message || {};
		}
		return this.modes_cache[company];
	},

	outstanding(frm) {
		if (frm.doctype === "Sales Invoice") return flt(frm.doc.outstanding_amount);
		const total = flt(frm.doc.rounded_total) || flt(frm.doc.grand_total);
		return Math.max(total - flt(frm.doc.advance_paid), 0);
	},

	paid_at_sale(frm) {
		return (frm.doctype === "POS Invoice" || cint(frm.doc.is_pos)) && !cint(frm.doc.is_return);
	},

	async setup(frm) {
		if (frm.is_new() || frm.doc.currency !== "KES") return;
		const modes = await royce_payments_stk.modes(frm.doc.company);
		if (!Object.keys(modes).length) return;

		if (frm.doc.docstatus === 0 && royce_payments_stk.paid_at_sale(frm)) {
			royce_payments_stk.setup_draft(frm);
		} else if (frm.doc.docstatus === 1 && frm.doctype !== "POS Invoice") {
			royce_payments_stk.setup_submitted(frm);
		}
	},

	setup_submitted(frm) {
		if (frm.doctype === "Sales Order" && ["Closed", "On Hold", "Completed"].includes(frm.doc.status)) return;
		if (royce_payments_stk.outstanding(frm) < 1) return;
		const mpesa = royce_payments.mpesa;

		frm.add_custom_button(
			__("Request M-Pesa Payment"),
			async () => {
				const name = await mpesa.send_prompt({
					doctype: frm.doctype,
					name: frm.doc.name,
					phone: frm.doc.contact_mobile,
					amount: royce_payments_stk.outstanding(frm),
				});
				mpesa.watch(name, (status) => status?.status === "Paid" && frm.reload_doc());
			},
			__("Create")
		);

		if (frm.doctype !== "Sales Invoice") return;
		frm.add_custom_button(
			__("Receive M-Pesa Payment"),
			async () => {
				const [row] = await mpesa.pick({
					doctype: frm.doctype,
					name: frm.doc.name,
					multiple: false,
					title: __("Post a received M-Pesa payment against {0}", [frm.doc.name]),
				});
				const method =
					row.doctype === "Daraja STK Request"
						? "royce_payments.daraja.stk.post_reviewed"
						: "royce_payments.daraja.c2b.post_reviewed";
				frappe.call({
					method,
					args: { name: row.name, customer: frm.doc.customer, sales_invoice: frm.doc.name },
					freeze: true,
					callback: () => {
						frappe.show_alert({ message: __("M-Pesa payment posted"), indicator: "green" });
						frm.reload_doc();
					},
				});
			},
			__("Create")
		);
	},

	async setup_draft(frm) {
		const mpesa = royce_payments.mpesa;
		const group = __("M-Pesa");

		const sync = async (summary) => {
			summary = summary || (await mpesa.summary(frm.doctype, frm.doc.name));
			if (await mpesa.set_payment_row(frm, summary)) {
				frappe.show_alert({
					message: __("M-Pesa received: {0}. Save or submit to record it.", [
						format_currency(summary.total, "KES"),
					]),
					indicator: "green",
				});
			}
		};
		const saved = async () => {
			if (frm.is_dirty()) await frm.save();
		};

		frm.add_custom_button(
			__("Request Payment"),
			async () => {
				await saved();
				const summary = await mpesa.summary(frm.doctype, frm.doc.name);
				const total = flt(frm.doc.rounded_total) || flt(frm.doc.grand_total);
				const waiting = summary.pending.reduce((sum, p) => sum + flt(p.amount), 0);
				const name = await mpesa.send_prompt({
					doctype: frm.doctype,
					name: frm.doc.name,
					phone: frm.doc.contact_mobile,
					amount: Math.max(total - summary.total - waiting, 0),
				});
				mpesa.watch(name, (status) => status?.status === "Received" && sync());
			},
			group
		);

		frm.add_custom_button(
			__("Add Received Payment"),
			async () => {
				await saved();
				const rows = await mpesa.pick({ doctype: frm.doctype, name: frm.doc.name });
				sync(await mpesa.apply(frm.doctype, frm.doc.name, rows));
			},
			group
		);

		const summary = await mpesa.summary(frm.doctype, frm.doc.name);
		// Confirmed after the form stopped watching (reloaded, or Safaricom took a while):
		// put it on the M-Pesa row now. Only when something was received, so an amount
		// typed in by hand is never wiped.
		const modes = Object.keys(summary.modes || {});
		const row = (frm.doc.payments || []).find((p) => modes.includes(p.mode_of_payment));
		if (row && summary.total && flt(row.amount) !== flt(summary.total)) {
			await sync(summary);
		}
		if (summary.applied.length || summary.pending.length) {
			const parts = summary.applied.map((r) => `${r.receipt} (${format_currency(r.amount, "KES")})`);
			if (summary.pending.length) parts.push(__("{0} prompt(s) waiting", [summary.pending.length]));
			frm.dashboard.set_headline(
				__("M-Pesa on this invoice: {0}", [frappe.utils.escape_html(parts.join(", "))]),
				"green"
			);
		}
	},
};

frappe.ui.form.on("Sales Invoice", { refresh: (frm) => royce_payments_stk.setup(frm) });
frappe.ui.form.on("POS Invoice", { refresh: (frm) => royce_payments_stk.setup(frm) });
frappe.ui.form.on("Sales Order", { refresh: (frm) => royce_payments_stk.setup(frm) });
}
