"""M-Pesa on invoices paid at the sale: POS Invoices, and Sales Invoices with "Include Payment
(POS)" ticked, which is what the POS screen makes.

On these, the money belongs on the invoice's own M-Pesa payment row, never on a separate
Payment Entry: the invoice posts it to the ledger when submitted. So a received payment (an
STK request Safaricom confirmed, or a verified Paybill/Till payment) is applied to one draft
invoice. Submitting the invoice settles it; cancelling or deleting the invoice frees it again.

A payment is applied whole, to one invoice. If the customer paid more, the excess is change,
as with cash.
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, now_datetime

STK = "Daraja STK Request"
C2B = "Daraja C2B Payment"
INVOICE_DOCTYPES = ("Sales Invoice", "POS Invoice")
REFERENCE_NO_MAX = 140


def is_paid_at_sale(doc) -> bool:
	return (
		doc.doctype in INVOICE_DOCTYPES
		and (doc.doctype == "POS Invoice" or cint(doc.get("is_pos")))
		and not cint(doc.get("is_return"))
	)


def invoice_total(doc) -> float:
	if cint(doc.get("disable_rounded_total")):
		return flt(doc.grand_total)
	return flt(doc.rounded_total) or flt(doc.grand_total)


def company_accounts(company: str) -> list[str]:
	return frappe.get_all("Daraja Account", filters={"company": company}, pluck="name")


def mpesa_modes(company: str) -> dict[str, bool]:
	"""{mode of payment: must the amount match payments received} for the company's enabled accounts."""
	modes = {}
	for row in frappe.get_all(
		"Daraja Account",
		filters={"company": company, "enabled": 1},
		fields=["mode_of_payment", "pos_requires_received_payment"],
	):
		modes[row.mode_of_payment] = modes.get(row.mode_of_payment, False) or bool(
			row.pos_requires_received_payment
		)
	return modes


def _stk_row(r) -> dict:
	return {
		"doctype": STK,
		"name": r.name,
		"receipt": r.mpesa_receipt,
		"amount": flt(r.amount),
		"phone": r.phone,
		"payer": "",
		"time": r.transaction_time or r.creation,
		"status": r.status,
	}


def _c2b_row(r) -> dict:
	return {
		"doctype": C2B,
		"name": r.name,
		"receipt": r.trans_id,
		"amount": flt(r.amount),
		"phone": r.msisdn,
		"payer": r.payer_name,
		"time": r.trans_time,
		"status": r.status,
	}


STK_FIELDS = ["name", "mpesa_receipt", "amount", "phone", "transaction_time", "creation", "status"]
C2B_FIELDS = ["name", "trans_id", "amount", "msisdn", "payer_name", "trans_time", "status"]


def applied(doctype: str, name: str) -> list[dict]:
	"""Received payments applied to this invoice. These are what its M-Pesa amount stands for."""
	linked = {"invoice_doctype": doctype, "invoice_name": name}
	stk = frappe.get_all(STK, filters={**linked, "status": ["in", ["Received", "Paid"]]}, fields=STK_FIELDS)
	c2b = frappe.get_all(
		C2B,
		filters={**linked, "verified": 1, "status": ["in", ["Needs Review", "Posted"]]},
		fields=C2B_FIELDS,
	)
	return [_stk_row(r) for r in stk] + [_c2b_row(r) for r in c2b]


def pending(doctype: str, name: str) -> list[dict]:
	"""Prompts sent for this invoice that the customer hasn't finished yet."""
	rows = frappe.get_all(
		STK,
		filters={
			"invoice_doctype": doctype,
			"invoice_name": name,
			"status": ["in", ["Pending", "Verifying"]],
		},
		fields=STK_FIELDS,
	)
	return [_stk_row(r) for r in rows]


def unreserved_amount(doc) -> float:
	"""How much more can be asked for by M-Pesa on this draft invoice."""
	taken = sum(r["amount"] for r in applied(doc.doctype, doc.name) + pending(doc.doctype, doc.name))
	return max(invoice_total(doc) - taken, 0)


def _free(rec) -> bool:
	"""Received, confirmed by Safaricom, and not yet used anywhere."""
	if rec.invoice_name or rec.payment_entry:
		return False
	if rec.doctype == STK:
		return rec.status == "Received"
	return rec.status == "Needs Review" and cint(rec.verified)


def _draft_invoice(invoice_doctype: str, invoice_name: str):
	if invoice_doctype not in INVOICE_DOCTYPES:
		frappe.throw(_("M-Pesa payments can be applied to {0} only").format(" / ".join(INVOICE_DOCTYPES)))
	doc = frappe.get_doc(invoice_doctype, invoice_name)
	doc.check_permission("write")
	if doc.docstatus != 0 or not is_paid_at_sale(doc):
		frappe.throw(_("{0} {1} is not an unsubmitted POS invoice").format(_(invoice_doctype), invoice_name))
	return doc


def summary(doc) -> dict:
	rows = applied(doc.doctype, doc.name)
	return {
		"applied": rows,
		"pending": pending(doc.doctype, doc.name),
		"total": sum(r["amount"] for r in rows),
		"modes": mpesa_modes(doc.company),
	}


@frappe.whitelist()
def get_summary(invoice_doctype: str, invoice_name: str):
	doc = frappe.get_doc(invoice_doctype, invoice_name)
	doc.check_permission("read")
	return summary(doc)


@frappe.whitelist()
def get_modes(company: str):
	# Only mode-of-payment names. Not gated on Company read: cashier roles often lack it, and
	# the POS buttons would silently vanish.
	return mpesa_modes(company)


@frappe.whitelist()
def find_received(invoice_doctype: str, invoice_name: str, search: str | None = None, days: int = 3):
	"""Payments that arrived and are free to use, newest first, for the payment picker."""
	doc = frappe.get_doc(invoice_doctype, invoice_name)
	doc.check_permission("read")
	accounts = company_accounts(doc.company)
	if not accounts:
		return []
	since = add_to_date(now_datetime(), days=-min(max(cint(days), 1), 90))

	stk = frappe.get_all(
		STK,
		filters={
			"daraja_account": ["in", accounts],
			"status": "Received",
			"invoice_name": ["is", "not set"],
			"creation": [">=", since],
		},
		fields=STK_FIELDS,
		limit=200,
	)
	c2b = frappe.get_all(
		C2B,
		filters={
			"daraja_account": ["in", accounts],
			"status": "Needs Review",
			"verified": 1,
			"invoice_name": ["is", "not set"],
			"payment_entry": ["is", "not set"],
			"creation": [">=", since],
		},
		fields=C2B_FIELDS,
		limit=200,
	)
	rows = [_stk_row(r) for r in stk] + [_c2b_row(r) for r in c2b]

	# Arriving: shown so the cashier knows the customer's payment is on its way. Not selectable.
	arriving = frappe.get_all(
		C2B,
		filters={
			"daraja_account": ["in", accounts],
			"status": ["in", ["Unverified", "Verifying"]],
			"creation": [">=", add_to_date(now_datetime(), hours=-1)],
		},
		fields=C2B_FIELDS,
		limit=50,
	)
	rows += [_c2b_row(r) for r in arriving]

	needle = (search or "").strip().lower()
	if needle:
		rows = [
			r
			for r in rows
			if needle in " ".join(str(r[k] or "") for k in ("receipt", "phone", "payer")).lower()
			or needle == str(cint(r["amount"]))
		]
	rows.sort(key=lambda r: str(r["time"] or ""), reverse=True)
	return rows[:50]


@frappe.whitelist(methods=["POST"])
def apply(invoice_doctype: str, invoice_name: str, payments):
	"""Apply received payments ([{doctype, name}]) to a draft POS invoice."""
	doc = _draft_invoice(invoice_doctype, invoice_name)
	accounts = company_accounts(doc.company)
	for p in frappe.parse_json(payments) or []:
		if p.get("doctype") not in (STK, C2B):
			frappe.throw(_("Not an M-Pesa payment"))
		# Row lock: two tills picking the same payment can't both get it.
		rec = frappe.get_doc(p["doctype"], p["name"], for_update=True)
		if rec.daraja_account not in accounts:
			frappe.throw(_("{0} was not received by {1}").format(rec.name, doc.company))
		if not _free(rec):
			frappe.throw(
				_("M-Pesa payment {0} is already used or not yet confirmed").format(
					rec.get("mpesa_receipt") or rec.name
				)
			)
		rec.db_set({"invoice_doctype": doc.doctype, "invoice_name": doc.name})
	return summary(doc)


@frappe.whitelist(methods=["POST"])
def release(invoice_doctype: str, invoice_name: str, doctype: str, name: str):
	"""Take a payment (or a prompt still waiting) back off a draft invoice."""
	doc = _draft_invoice(invoice_doctype, invoice_name)
	if doctype not in (STK, C2B):
		frappe.throw(_("Not an M-Pesa payment"))
	rec = frappe.get_doc(doctype, name, for_update=True)
	if rec.invoice_doctype == doc.doctype and rec.invoice_name == doc.name:
		rec.db_set({"invoice_doctype": None, "invoice_name": None})
	return summary(doc)


def _release_all(doctype: str, name: str, statuses: list[str] | None = None) -> None:
	for record_doctype in (STK, C2B):
		filters = {"invoice_doctype": doctype, "invoice_name": name}
		if statuses:
			filters["status"] = ["in", statuses]
		for rec_name in frappe.get_all(record_doctype, filters=filters, pluck="name"):
			frappe.db.set_value(record_doctype, rec_name, {"invoice_doctype": None, "invoice_name": None})


# doc_events on Sales Invoice and POS Invoice


def before_submit(doc, method=None):
	if not is_paid_at_sale(doc) or cint(doc.get("is_consolidated")):
		return
	modes = mpesa_modes(doc.company)
	received = applied(doc.doctype, doc.name)
	if not modes and not received:
		return

	rows = [p for p in doc.payments if p.mode_of_payment in modes]
	entered = flt(sum(flt(p.amount) for p in rows), 2)
	total = flt(sum(r["amount"] for r in received), 2)

	if received and not rows:
		frappe.throw(
			_(
				"M-Pesa payments are applied to this invoice, but it has no M-Pesa payment row. Add the M-Pesa mode of payment to the POS Profile."
			)
		)
	if total > entered:
		frappe.throw(
			_("M-Pesa payments of {0} are applied to this invoice, but its M-Pesa amount is {1}.").format(
				frappe.format(total, "Currency"), frappe.format(entered, "Currency")
			)
		)
	if entered > total and any(modes[p.mode_of_payment] for p in rows if flt(p.amount)):
		waiting = _(" A prompt is still waiting for the customer.") if pending(doc.doctype, doc.name) else ""
		frappe.throw(
			_(
				"M-Pesa amount is {0} but only {1} has been received.{2} Send a prompt or pick the customer's payment."
			).format(frappe.format(entered, "Currency"), frappe.format(total, "Currency"), waiting),
			title=_("M-Pesa payment not received"),
		)

	receipts = ", ".join(r["receipt"] for r in received if r["receipt"])
	if receipts:
		row = next((p for p in rows if flt(p.amount)), rows[0])
		row.reference_no = receipts[:REFERENCE_NO_MAX]


def on_submit(doc, method=None):
	if not is_paid_at_sale(doc):
		return
	for r in applied(doc.doctype, doc.name):
		if r["doctype"] == STK:
			frappe.db.set_value(STK, r["name"], "status", "Paid")
		else:
			frappe.db.set_value(
				C2B,
				r["name"],
				{
					"status": "Posted",
					"customer": doc.customer,
					"against_doctype": doc.doctype,
					"against_name": doc.name,
				},
			)
	# Prompts the customer never finished stay theirs: if one completes later it is free to use.
	_release_all(doc.doctype, doc.name, ["Pending", "Verifying", "Needs Review"])


def on_cancel(doc, method=None):
	if not is_paid_at_sale(doc):
		return
	note = _("{0} {1} was cancelled. The payment is free to use again.").format(_(doc.doctype), doc.name)
	for r in applied(doc.doctype, doc.name):
		if r["doctype"] == STK:
			frappe.db.set_value(STK, r["name"], {"status": "Received", "result_desc": note})
		else:
			frappe.db.set_value(
				C2B,
				r["name"],
				{
					"status": "Needs Review",
					"review_note": note,
					"customer": None,
					"against_doctype": None,
					"against_name": None,
				},
			)
	_release_all(doc.doctype, doc.name)


def on_trash(doc, method=None):
	if doc.doctype in INVOICE_DOCTYPES and doc.docstatus == 0:
		_release_all(doc.doctype, doc.name)
