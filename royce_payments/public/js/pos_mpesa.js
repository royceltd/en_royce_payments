// M-Pesa on the POS screen's payment step (hooks.py: page_js for "point-of-sale").
//
// Under each M-Pesa mode of payment, adds "Send prompt" (STK Push to the customer's phone)
// and "Find payment" (one the customer paid to the Till/Paybill). Either way the received
// amount is put on the invoice's M-Pesa payment row; the server checks it on submit.
//
// ERPNext's POS loads its classes from point-of-sale.bundle.js after this file runs, so the
// patch is applied once that bundle is in. Patching the prototype also covers a Payment
// component that already exists.

frappe.provide("royce_payments");

royce_payments.pos_mpesa = {
	modes_by_company: {},
	summary: null, // for the invoice named in summary_for
	summary_for: null,

	patch() {
		const Payment = erpnext.PointOfSale && erpnext.PointOfSale.Payment;
		if (!Payment || Payment.prototype.royce_mpesa_patched) return;
		const render = Payment.prototype.render_payment_mode_dom;
		Payment.prototype.render_payment_mode_dom = function (...args) {
			const result = render.apply(this, args);
			royce_payments.pos_mpesa.decorate(this);
			return result;
		};
		Payment.prototype.royce_mpesa_patched = true;
	},

	modes(company) {
		if (!this.modes_by_company[company]) {
			this.modes_by_company[company] = frappe
				.call({ method: "royce_payments.daraja.invoice.get_modes", args: { company } })
				.then((r) => r.message || {});
		}
		return this.modes_by_company[company];
	},

	async refresh_summary(frm) {
		this.summary = await royce_payments.mpesa.summary(frm.doctype, frm.doc.name);
		this.summary_for = frm.doc.name;
		return this.summary;
	},

	async decorate(payment) {
		const frm = payment.events.get_frm();
		if (!frm || frm.doc.__islocal || frm.doc.docstatus !== 0 || frm.doc.currency !== "KES") return;
		const modes = await this.modes(frm.doc.company);
		if (!Object.keys(modes).length) return;
		if (this.summary_for !== frm.doc.name) await this.refresh_summary(frm);

		(frm.doc.payments || [])
			.filter((p) => p.mode_of_payment in modes)
			.forEach((p) => {
				const mode = payment.sanitize_mode_of_payment(p.mode_of_payment);
				const $mode = payment.$payment_modes.find(`.mode-of-payment[data-mode="${mode}"]`);
				if (!$mode.length) return;
				$mode.find(".royce-mpesa").remove();
				$mode.append(this.actions_html());
				const $actions = $mode.find(".royce-mpesa");
				$actions.on("click", (e) => e.stopPropagation());
				$actions.find(".royce-mpesa-prompt").on("click", () => this.send_prompt(payment, frm));
				$actions.find(".royce-mpesa-pick").on("click", () => this.pick(payment, frm));
				$actions.find(".royce-mpesa-release").on("click", (e) =>
					this.release(payment, frm, $(e.currentTarget).data())
				);
			});
	},

	actions_html() {
		const s = this.summary || { applied: [], pending: [] };
		const esc = frappe.utils.escape_html;
		const applied = s.applied
			.map(
				(r) => `<span class="indicator-pill green" style="margin: 2px">
					${esc(r.receipt || "")} · ${format_currency(r.amount, "KES")}
					<a class="royce-mpesa-release" data-doctype="${esc(r.doctype)}" data-name="${esc(
					r.name
				)}" title="${__("Remove")}" style="margin-left: 4px">&times;</a>
				</span>`
			)
			.join("");
		const pending = s.pending
			.map(
				(r) => `<span class="indicator-pill orange" style="margin: 2px">
					${__("Waiting for PIN")} · ${esc(r.phone || "")} · ${format_currency(r.amount, "KES")}
					<a class="royce-mpesa-release" data-doctype="${esc(r.doctype)}" data-name="${esc(
					r.name
				)}" title="${__("Stop waiting")}" style="margin-left: 4px">&times;</a>
				</span>`
			)
			.join("");
		return `<div class="royce-mpesa" style="margin-top: var(--margin-sm); cursor: default">
			<div style="display: flex; gap: var(--margin-xs); flex-wrap: wrap">
				<button class="btn btn-xs btn-primary royce-mpesa-prompt">${__("Send prompt")}</button>
				<button class="btn btn-xs btn-default royce-mpesa-pick">${__("Find payment")}</button>
			</div>
			<div style="margin-top: var(--margin-xs)">${applied}${pending}</div>
		</div>`;
	},

	// Typing in our dialogs must not land on the POS number pad, which listens to every key
	// while a payment mode is selected.
	deselect(payment) {
		payment.selected_mode = "";
		payment.$payment_modes.find(".mode-of-payment").removeClass("border-primary");
		payment.$payment_modes.find(".mode-of-payment-control").css("display", "none");
	},

	async apply_to_row(payment, frm, summary) {
		this.summary = summary;
		this.summary_for = frm.doc.name;
		await royce_payments.mpesa.set_payment_row(frm, summary);
		payment.render_payment_mode_dom();
		payment.update_totals_section(frm.doc);
	},

	async send_prompt(payment, frm) {
		this.deselect(payment);
		if (frm.is_dirty()) await frm.save();
		const summary = await this.refresh_summary(frm);
		const total = cint(frappe.sys_defaults.disable_rounded_total)
			? flt(frm.doc.grand_total)
			: flt(frm.doc.rounded_total) || flt(frm.doc.grand_total);
		const waiting = summary.pending.reduce((sum, p) => sum + flt(p.amount), 0);
		const name = await royce_payments.mpesa.send_prompt({
			doctype: frm.doctype,
			name: frm.doc.name,
			phone: frm.doc.contact_mobile,
			amount: Math.max(total - summary.total - waiting, 0),
		});
		await this.refresh_summary(frm);
		payment.render_payment_mode_dom();
		royce_payments.mpesa.watch(name, async () => {
			// The cashier may have moved on to another sale meanwhile.
			if (payment.events.get_frm().doc.name !== frm.doc.name) return;
			await this.apply_to_row(payment, frm, await this.refresh_summary(frm));
		});
	},

	async pick(payment, frm) {
		this.deselect(payment);
		if (frm.is_dirty()) await frm.save();
		const rows = await royce_payments.mpesa.pick({ doctype: frm.doctype, name: frm.doc.name });
		await this.apply_to_row(payment, frm, await royce_payments.mpesa.apply(frm.doctype, frm.doc.name, rows));
	},

	async release(payment, frm, { doctype, name }) {
		this.deselect(payment);
		const { message } = await frappe.call({
			method: "royce_payments.daraja.invoice.release",
			args: { invoice_doctype: frm.doctype, invoice_name: frm.doc.name, doctype, name },
			freeze: true,
		});
		await this.apply_to_row(payment, frm, message);
	},
};

frappe.require("point-of-sale.bundle.js", () => royce_payments.pos_mpesa.patch());
