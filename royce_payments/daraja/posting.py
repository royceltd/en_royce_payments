"""Turning a verified M-Pesa payment into a Payment Entry, and matching C2B payments."""

import frappe
from erpnext.accounts.party import get_party_account
from frappe import _
from frappe.utils import flt, getdate

CURRENCY = "KES"


class NeedsReview(Exception):
	"""The payment is real but can't be posted automatically. The message says why."""


def match_c2b(account, bill_ref: str) -> tuple[str | None, str | None, str | None]:
	"""(against_doctype, against_name, customer) for the account number the payer typed.

	Only exact matches, in order: an unpaid submitted Sales Invoice, an unbilled submitted
	Sales Order, a Customer ID. Returns (None, None, None) when nothing matches; never guesses.
	"""
	ref = (bill_ref or "").strip()
	if not ref:
		return None, None, None

	invoice = frappe.db.get_value(
		"Sales Invoice",
		{"name": ref, "docstatus": 1, "company": account.company, "outstanding_amount": [">", 0]},
		["name", "customer"],
		as_dict=True,
	)
	if invoice:
		return "Sales Invoice", invoice.name, invoice.customer

	order = frappe.db.get_value(
		"Sales Order",
		{"name": ref, "docstatus": 1, "company": account.company, "per_billed": ["<", 100]},
		["name", "customer", "status"],
		as_dict=True,
	)
	if order and order.status not in ("Closed", "On Hold", "Completed"):
		return "Sales Order", order.name, order.customer

	customer = frappe.db.get_value("Customer", {"name": ref, "disabled": 0}, "name")
	if customer:
		return None, None, customer

	return None, None, None


def outstanding_for(doctype: str, name: str) -> float:
	doc = frappe.get_doc(doctype, name)
	if doctype == "Sales Invoice":
		return flt(doc.outstanding_amount)
	total = flt(doc.rounded_total) or flt(doc.grand_total)
	return max(total - flt(doc.advance_paid), 0)


def post_or_review(account, **kwargs) -> tuple[str | None, str | None]:
	"""post_payment(), but any failure becomes a review note instead of an exception.

	Returns (payment_entry, None) or (None, reason). A failed attempt is rolled back to a
	savepoint, so it never leaves a draft Payment Entry behind.
	"""
	frappe.db.savepoint("royce_payments_post")
	try:
		return post_payment(account, **kwargs), None
	except NeedsReview as e:
		frappe.db.rollback(save_point="royce_payments_post")
		return None, str(e)
	except Exception as e:
		frappe.db.rollback(save_point="royce_payments_post")
		frappe.log_error("Daraja payment could not be posted", reference_doctype="Daraja Account",
			reference_name=account.name)
		return None, _("Could not post: {0}").format(e)[:1000]


def post_payment(
	account,
	*,
	amount,
	receipt: str,
	payment_date,
	customer: str,
	against_doctype: str | None = None,
	against_name: str | None = None,
	remarks: str = "",
) -> str:
	"""Create and submit a Receive Payment Entry. Idempotent on the M-Pesa receipt.

	Takes a row lock on the Daraja Account so two postings of the same receipt (an STK
	callback and the matching C2B confirmation) can't both pass the duplicate check.
	"""
	frappe.db.get_value("Daraja Account", account.name, "name", for_update=True)

	existing = frappe.db.get_value(
		"Payment Entry",
		{
			"reference_no": receipt,
			"paid_to": account.receiving_account,
			"payment_type": "Receive",
			"docstatus": 1,
		},
		"name",
	)
	if existing:
		return existing

	company_currency = frappe.get_cached_value("Company", account.company, "default_currency")
	if company_currency != CURRENCY:
		raise NeedsReview(_("Company currency is {0}, not {1}").format(company_currency, CURRENCY))

	party_account = get_party_account("Customer", customer, account.company)
	if frappe.get_cached_value("Account", party_account, "account_currency") != CURRENCY:
		raise NeedsReview(_("Customer {0}'s receivable account is not in {1}").format(customer, CURRENCY))

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Receive"
	pe.company = account.company
	pe.posting_date = getdate(payment_date)
	pe.mode_of_payment = account.mode_of_payment
	pe.party_type = "Customer"
	pe.party = customer
	pe.paid_from = party_account
	pe.paid_to = account.receiving_account
	pe.paid_amount = flt(amount)
	pe.received_amount = flt(amount)
	pe.reference_no = receipt
	pe.reference_date = getdate(payment_date)
	pe.remarks = remarks

	if against_doctype and against_name:
		if frappe.db.get_value(against_doctype, against_name, "currency") != CURRENCY:
			raise NeedsReview(_("{0} {1} is not in {2}").format(against_doctype, against_name, CURRENCY))
		allocated = min(flt(amount), outstanding_for(against_doctype, against_name))
		if allocated > 0:
			pe.append(
				"references",
				{
					"reference_doctype": against_doctype,
					"reference_name": against_name,
					"allocated_amount": allocated,
				},
			)

	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()
	return pe.name
