// Shared M-Pesa dialogs for desk forms (daraja_stk.js) and the POS screen (pos_mpesa.js).
// Loaded on every desk page through app_include_js, so keep it small and side-effect free.

frappe.provide("royce_payments");

royce_payments.mpesa = {
	// Our own server is polled, never Safaricom. Fast while the customer is entering their PIN,
	// then slower: Safaricom can take minutes to confirm, and the sale should still pick it up.
	POLL_MS: 4000,
	SLOW_POLL_MS: 15000,
	FAST_POLLS: 30, // two minutes
	POLL_LIMIT: 60, // then ~7.5 more minutes; after that the scheduler still finishes the request
	PICKER_REFRESH_MS: 5000,

	// Ask Safaricom to prompt the customer's phone. Resolves with the request name.
	send_prompt({ doctype, name, phone, amount }) {
		return new Promise((resolve) => {
			const dialog = new frappe.ui.Dialog({
				title: __("Request M-Pesa payment"),
				fields: [
					{
						fieldname: "phone",
						fieldtype: "Data",
						options: "Phone",
						label: __("Customer's M-Pesa number"),
						default: phone || "",
						reqd: 1,
					},
					{
						fieldname: "amount",
						fieldtype: "Int",
						label: __("Amount (KES)"),
						default: Math.ceil(amount || 0),
						reqd: 1,
					},
				],
				primary_action_label: __("Send prompt"),
				primary_action(values) {
					frappe.call({
						method: "royce_payments.daraja.stk.request_payment",
						args: { reference_doctype: doctype, reference_name: name, ...values },
						freeze: true,
						freeze_message: __("Sending prompt..."),
						callback: (r) => {
							dialog.hide();
							frappe.show_alert({
								message: __("Prompt sent. Waiting for the customer to enter their PIN..."),
								indicator: "blue",
							});
							resolve(r.message.name);
						},
					});
				},
			});
			dialog.show();
		});
	},

	// Poll an STK request until it settles. on_change(status) gets the final status payload,
	// or null if it is still unfinished after POLL_LIMIT tries. on_progress(status), if given,
	// is called once when the customer has paid but Safaricom hasn't confirmed yet.
	watch(request_name, on_change, on_progress = null, count = 0) {
		const me = royce_payments.mpesa;
		if (count >= me.POLL_LIMIT) {
			frappe.show_alert({
				message: __("Still waiting for M-Pesa. The payment will be recorded automatically when it arrives."),
				indicator: "orange",
			});
			on_change(null);
			return;
		}
		if (count === me.FAST_POLLS) {
			frappe.show_alert({
				message: __("Waiting for Safaricom to confirm the M-Pesa payment..."),
				indicator: "orange",
			});
		}
		setTimeout(() => {
			frappe.call({
				method: "royce_payments.daraja.stk.get_status",
				args: { name: request_name },
				callback: ({ message }) => {
					if (["Pending", "Verifying"].includes(message.status)) {
						if (on_progress && message.mpesa_receipt) {
							on_progress(message);
							on_progress = null;
						}
						me.watch(request_name, on_change, on_progress, count + 1);
						return;
					}
					if (["Paid", "Received"].includes(message.status)) {
						frappe.show_alert({
							message: __("M-Pesa payment received: {0}", [message.mpesa_receipt || ""]),
							indicator: "green",
						});
					} else {
						frappe.msgprint({
							title: __("M-Pesa payment {0}", [__(message.status)]),
							message: frappe.utils.escape_html(message.result_desc || ""),
							indicator: message.status === "Needs Review" ? "orange" : "red",
						});
					}
					on_change(message);
				},
			});
		}, count < me.FAST_POLLS ? me.POLL_MS : me.SLOW_POLL_MS);
	},

	// Let a person pick payments that arrived (Till/Paybill, or prompts nobody used).
	// Resolves with the chosen rows ([{doctype, name, amount, receipt}]).
	pick({ doctype, name, multiple = true, title }) {
		return new Promise((resolve) => {
			let rows = [];
			let timer = null;
			const selected = new Set();

			const dialog = new frappe.ui.Dialog({
				title: title || __("Payments received"),
				size: "large",
				fields: [
					{
						fieldname: "search",
						fieldtype: "Data",
						label: __("Search"),
						description: __("Receipt, phone, payer name or exact amount"),
						onchange: () => load(),
					},
					{ fieldname: "list", fieldtype: "HTML" },
				],
				primary_action_label: __("Use selected"),
				primary_action() {
					const chosen = rows.filter((r) => selected.has(key(r)));
					if (!chosen.length) {
						frappe.show_alert({ message: __("Select a payment"), indicator: "orange" });
						return;
					}
					dialog.hide();
					resolve(chosen);
				},
				secondary_action_label: __("Refresh"),
				secondary_action: () => load(),
			});
			dialog.onhide = () => clearInterval(timer);

			const key = (r) => `${r.doctype}::${r.name}`;
			const usable = (r) => !["Unverified", "Verifying"].includes(r.status);
			const $list = dialog.fields_dict.list.$wrapper;

			const render = () => {
				if (!rows.length) {
					$list.html(
						`<div class="text-muted text-center" style="padding: var(--padding-lg)">${__(
							"No M-Pesa payments waiting. New ones appear here as they arrive."
						)}</div>`
					);
					return;
				}
				$list.html(`
					<table class="table table-sm table-hover" style="margin: 0">
						<thead><tr>
							<th></th><th>${__("Receipt")}</th><th>${__("Payer")}</th>
							<th>${__("Time")}</th><th class="text-right">${__("Amount")}</th>
						</tr></thead>
						<tbody>${rows
							.map((r) => {
								const k = key(r);
								const payer = [r.payer, r.phone].filter(Boolean).join(" · ");
								const state = usable(r)
									? `<input type="checkbox" data-key="${frappe.utils.escape_html(k)}" ${
											selected.has(k) ? "checked" : ""
									  }>`
									: `<span class="indicator-pill orange">${__("Confirming")}</span>`;
								return `<tr style="${usable(r) ? "cursor: pointer" : "opacity: .6"}" data-key="${frappe.utils.escape_html(k)}">
									<td>${state}</td>
									<td>${frappe.utils.escape_html(r.receipt || "")}</td>
									<td>${frappe.utils.escape_html(payer)}</td>
									<td>${r.time ? frappe.datetime.comment_when(r.time) : ""}</td>
									<td class="text-right">${format_currency(r.amount, "KES")}</td>
								</tr>`;
							})
							.join("")}</tbody>
					</table>`);
			};

			$list.on("click", "tr[data-key]", function () {
				const k = $(this).attr("data-key");
				const row = rows.find((r) => key(r) === k);
				if (!row || !usable(row)) return;
				if (selected.has(k)) {
					selected.delete(k);
				} else {
					if (!multiple) selected.clear();
					selected.add(k);
				}
				render();
			});

			const load = () =>
				frappe.call({
					method: "royce_payments.daraja.invoice.find_received",
					args: { invoice_doctype: doctype, invoice_name: name, search: dialog.get_value("search") },
					callback: ({ message }) => {
						rows = message || [];
						render();
					},
				});

			dialog.show();
			load();
			timer = setInterval(load, royce_payments.mpesa.PICKER_REFRESH_MS);
		});
	},

	apply(doctype, name, rows) {
		return frappe
			.call({
				method: "royce_payments.daraja.invoice.apply",
				args: {
					invoice_doctype: doctype,
					invoice_name: name,
					payments: rows.map((r) => ({ doctype: r.doctype, name: r.name })),
				},
				freeze: true,
			})
			.then((r) => r.message);
	},

	summary(doctype, name) {
		return frappe
			.call({
				method: "royce_payments.daraja.invoice.get_summary",
				args: { invoice_doctype: doctype, invoice_name: name },
			})
			.then((r) => r.message);
	},

	// Put the received total on the invoice's M-Pesa payment row. Resolves with the row, if any.
	async set_payment_row(frm, summary) {
		const modes = Object.keys(summary.modes || {});
		const row = (frm.doc.payments || []).find((p) => modes.includes(p.mode_of_payment));
		if (!row) {
			frappe.msgprint(
				__("This invoice has no M-Pesa payment row. Add the M-Pesa mode of payment to the POS Profile.")
			);
			return null;
		}
		await frappe.model.set_value(row.doctype, row.name, "amount", flt(summary.total));
		await royce_payments.mpesa.rebalance_default_mode(frm, row);
		return row;
	},

	// The POS fills the profile's default mode (usually Cash) with the whole total at checkout.
	// Once M-Pesa covers part of the bill, that row must only carry what is still owed, or
	// the invoice shows money twice and the till pays out the difference as "change".
	// Mirrors ERPNext's own rule for a second mode: it gets the remaining amount, never less
	// than zero. Rows the cashier typed into (not the default) are left alone, so splits work.
	async rebalance_default_mode(frm, mpesa_row) {
		const default_row = (frm.doc.payments || []).find((p) => cint(p.default) && p.name !== mpesa_row.name);
		if (!default_row) return;
		const total = cint(frappe.sys_defaults.disable_rounded_total)
			? flt(frm.doc.grand_total)
			: flt(frm.doc.rounded_total) || flt(frm.doc.grand_total);
		const others = (frm.doc.payments || [])
			.filter((p) => p.name !== default_row.name)
			.reduce((sum, p) => sum + flt(p.amount), 0);
		const remaining = Math.max(flt(total - others, precision("amount", default_row)), 0);
		if (flt(default_row.amount) !== remaining) {
			await frappe.model.set_value(default_row.doctype, default_row.name, "amount", remaining);
		}
	},
};
