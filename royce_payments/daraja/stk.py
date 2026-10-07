"""STK Push (Mpesa Express): prompt a customer's phone to pay an invoice or Sales Order.

Two ways a confirmed payment is settled (the request's `settlement`):
- Payment Entry: for a submitted Sales Invoice or Sales Order. Posted here.
- Invoice: for an unsubmitted POS invoice. It becomes Received, and the invoice's own M-Pesa
  payment row records it when the invoice is submitted (see invoice.py).
"""

import math
import re

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, now_datetime

from royce_payments.daraja import client, invoice, protocol
from royce_payments.daraja.posting import check_sales_invoice, outstanding_for, post_or_review
from royce_payments.daraja.protocol import DarajaError

SUPPORTED_DOCTYPES = ("Sales Invoice", "POS Invoice", "Sales Order")
# One open prompt per phone at a time; a second one within this window is refused.
PHONE_COOLDOWN_SECONDS = 120
MAX_VERIFY_ATTEMPTS = 10
FINAL_STATUSES = ("Received", "Paid", "Failed", "Cancelled", "Needs Review")


def active_account(company: str, name: str | None = None):
	filters = {"company": company, "enabled": 1}
	if name:
		filters["name"] = name
	found = frappe.get_all("Daraja Account", filters=filters, pluck="name", order_by="creation asc", limit=1)
	if not found:
		frappe.throw(_("No enabled Daraja Account for {0}. Set one up first.").format(company))
	return frappe.get_doc("Daraja Account", found[0])


@frappe.whitelist(methods=["POST"])
def request_payment(reference_doctype: str, reference_name: str, phone: str, amount, daraja_account=None):
	if reference_doctype not in SUPPORTED_DOCTYPES:
		frappe.throw(_("M-Pesa requests are supported on {0} only").format(", ".join(SUPPORTED_DOCTYPES)))

	doc = frappe.get_doc(reference_doctype, reference_name)
	if doc.docstatus == 0 and invoice.is_paid_at_sale(doc):
		settlement = "Invoice"
		doc.check_permission("write")
	elif doc.docstatus == 1 and reference_doctype != "POS Invoice":
		settlement = "Payment Entry"
		doc.check_permission("read")
		frappe.has_permission("Payment Entry", "create", throw=True)
	else:
		frappe.throw(_("Submit the {0} before requesting payment").format(_(reference_doctype)))
	if doc.currency != "KES":
		frappe.throw(_("M-Pesa payments must be in KES"))

	try:
		phone = protocol.normalize_phone(phone)
	except ValueError:
		frappe.throw(_("Enter a Kenyan mobile number, for example 0712 345 678"))

	amount = cint(amount)
	if settlement == "Invoice":
		ceiling = math.ceil(invoice.unreserved_amount(doc))
	else:
		ceiling = math.ceil(outstanding_for(reference_doctype, reference_name))
	if amount < 1 or amount > ceiling:
		frappe.throw(_("Amount must be a whole number between 1 and {0}").format(ceiling))

	if frappe.db.exists(
		"Daraja STK Request",
		{
			"phone": phone,
			"status": "Pending",
			"creation": [">", add_to_date(now_datetime(), seconds=-PHONE_COOLDOWN_SECONDS)],
		},
	):
		frappe.throw(_("A payment prompt was just sent to this number. Wait for it to finish."))

	account = active_account(doc.company, daraja_account)
	request = frappe.get_doc(
		{
			"doctype": "Daraja STK Request",
			"daraja_account": account.name,
			"status": "Pending",
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"settlement": settlement,
			"invoice_doctype": reference_doctype if settlement == "Invoice" else None,
			"invoice_name": reference_name if settlement == "Invoice" else None,
			"customer": doc.customer,
			"phone": phone,
			"amount": amount,
		}
	).insert(ignore_permissions=True)

	ts = protocol.timestamp()
	till = account.shortcode_type == "Till"
	body = {
		"BusinessShortCode": account.business_shortcode,
		"Password": protocol.stk_password(account.business_shortcode, account.get_password("passkey"), ts),
		"Timestamp": ts,
		"TransactionType": "CustomerBuyGoodsOnline" if till else "CustomerPayBillOnline",
		"Amount": amount,
		"PartyA": phone,
		"PartyB": account.till_number if till else account.business_shortcode,
		"PhoneNumber": phone,
		"CallBackURL": client.callback_url(account, "stk_result"),
		"AccountReference": protocol.account_reference(reference_name),
		"TransactionDesc": protocol.transaction_desc(doc.company),
	}

	try:
		response = client.post(account, protocol.STK_PUSH_PATH, body)
	except DarajaError as e:
		request.db_set({"status": "Failed", "result_desc": str(e)[:1000]})
		frappe.db.commit()
		frappe.throw(_("M-Pesa did not accept the request: {0}").format(e))

	request.db_set(
		{
			"merchant_request_id": response.get("MerchantRequestID"),
			"checkout_request_id": response.get("CheckoutRequestID"),
			"result_desc": response.get("CustomerMessage") or response.get("ResponseDescription"),
		}
	)
	# Commit now: the customer can finish on their phone before this request would
	# otherwise commit, and the callback must find the CheckoutRequestID.
	frappe.db.commit()
	return {"name": request.name, "message": response.get("CustomerMessage")}


@frappe.whitelist()
def get_status(name: str):
	request = frappe.get_doc("Daraja STK Request", name)
	frappe.get_doc(request.reference_doctype, request.reference_name).check_permission("read")
	return {
		"status": request.status,
		"result_desc": request.result_desc,
		"payment_entry": request.payment_entry,
		"mpesa_receipt": request.mpesa_receipt,
		"invoice_name": request.invoice_name,
	}


def verify(name: str) -> None:
	"""Confirm an STK request with STK Push Query, then post it. Safe to run repeatedly."""
	frappe.set_user("Administrator")
	request = frappe.get_doc("Daraja STK Request", name, for_update=True)
	# In use on Safaricom's result alone (usable_on_callback), and not confirmed yet.
	provisional = request.status in ("Received", "Paid") and not cint(request.verified)
	if not request.checkout_request_id or cint(request.confirmation_failed):
		return
	if request.status in FINAL_STATUSES and not provisional:
		return
	account = frappe.get_doc("Daraja Account", request.daraja_account)

	ts = protocol.timestamp()
	body = {
		"BusinessShortCode": account.business_shortcode,
		"Password": protocol.stk_password(account.business_shortcode, account.get_password("passkey"), ts),
		"Timestamp": ts,
		"CheckoutRequestID": request.checkout_request_id,
	}
	attempts = cint(request.verify_attempts) + 1
	try:
		result = client.post(account, protocol.STK_PUSH_QUERY_PATH, body)
	except DarajaError as e:
		if e.code == protocol.STK_STILL_PROCESSING and attempts < MAX_VERIFY_ATTEMPTS:
			request.db_set("verify_attempts", attempts)
			return
		if provisional:
			if attempts >= MAX_VERIFY_ATTEMPTS:
				_flag_unconfirmed(request, _("Safaricom never confirmed this payment: {0}").format(e))
			else:
				request.db_set({"verify_attempts": attempts, "result_desc": str(e)[:1000]})
			return
		status = "Needs Review" if attempts >= MAX_VERIFY_ATTEMPTS else request.status
		request.db_set({"verify_attempts": attempts, "status": status, "result_desc": str(e)[:1000]})
		return

	code = cint(result.get("ResultCode", -1))
	desc = result.get("ResultDesc") or ""
	if code != 0 and provisional:
		request.db_set({"verify_attempts": attempts, "result_code": code})
		_flag_unconfirmed(request, _("Safaricom's confirmation says this payment did not go through: {0}").format(desc))
		return
	if code != 0:
		status = "Cancelled" if code == protocol.STK_CANCELLED_BY_USER else "Failed"
		request.db_set({"verify_attempts": attempts, "status": status, "result_code": code, "result_desc": desc})
		return

	request.db_set({"verify_attempts": attempts, "result_code": 0, "result_desc": desc, "verified": 1})
	if provisional:
		return  # Already on the invoice; now confirmed too.
	if not request.mpesa_receipt:
		# Paid, but the receipt only comes in the callback, which hasn't arrived. The
		# callback, or a person, finishes this.
		if attempts >= MAX_VERIFY_ATTEMPTS:
			request.db_set("status", "Needs Review")
		return

	_post(request, account)


def usable_on_callback(account, request, result) -> bool:
	"""Opt-in per Daraja Account (pos_use_callback): may a paid POS prompt count at the till on
	Safaricom's result alone, before verify() confirms it?

	Only STK, only for POS invoices, and only a result that names a receipt and exactly the
	amount we asked for. Forging one needs the callback token *and* the CheckoutRequestID
	Safaricom issued only to us, for a prompt that is open right now. Till/Paybill payments
	have no such second secret, so they always wait for confirmation."""
	return bool(
		cint(account.pos_use_callback)
		and request.settlement == "Invoice"
		and result.result_code == 0
		and result.receipt
		and result.amount is not None
		and flt(result.amount) == flt(request.amount)
	)


def _flag_unconfirmed(request, reason: str) -> None:
	"""A payment already used at the till that Safaricom did not confirm: keep it where it is
	(the sale may be done), but make sure a person looks at it."""
	request.db_set({"confirmation_failed": 1, "result_desc": reason[:1000]})
	subject = _("M-Pesa {0} ({1}) not confirmed by Safaricom").format(
		request.mpesa_receipt or request.name, frappe.format(request.amount, "Currency")
	)
	if request.invoice_name:
		frappe.get_doc(request.invoice_doctype, request.invoice_name).add_comment(
			"Comment", _("{0}. {1} Check the M-Pesa statement.").format(subject, reason)
		)
	company = frappe.db.get_value("Daraja Account", request.daraja_account, "company")
	for user in accounts_managers(company):
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"type": "Alert",
				"subject": subject,
				"email_content": reason,
				"document_type": "Daraja STK Request",
				"document_name": request.name,
			}
		).insert(ignore_permissions=True)


def accounts_managers(company: str) -> list[str]:
	users = frappe.get_all(
		"Has Role", filters={"role": "Accounts Manager", "parenttype": "User"}, pluck="parent", distinct=True
	)
	return [
		u
		for u in users
		if u not in ("Administrator", "Guest")
		and frappe.db.get_value("User", u, "enabled")
		and frappe.has_permission("Company", "read", company, user=u)
	] or ["Administrator"]


def _post(request, account) -> None:
	if request.callback_amount and float(request.callback_amount) != float(request.amount):
		request.db_set(
			{
				"status": "Needs Review",
				"result_desc": _("Callback amount {0} differs from requested {1}").format(
					request.callback_amount, request.amount
				),
			}
		)
		return
	if request.settlement == "Invoice":
		# The invoice records the money when it is submitted. If the cashier gave up on it
		# (submitted without this, or deleted it), the link is already gone and this
		# payment is free to apply to another invoice or to post.
		request.db_set("status", "Received")
		return
	pe, problem = post_or_review(
		account,
		amount=request.amount,
		receipt=request.mpesa_receipt,
		payment_date=request.transaction_time or now_datetime(),
		customer=request.customer,
		against_doctype=request.reference_doctype,
		against_name=request.reference_name,
		remarks=_("M-Pesa STK payment {0} from {1}").format(request.mpesa_receipt, request.phone),
	)
	if problem:
		request.db_set({"status": "Needs Review", "result_desc": problem})
		return
	request.db_set({"status": "Paid", "payment_entry": pe})


@frappe.whitelist(methods=["POST"])
def post_reviewed(
	name: str,
	customer: str,
	sales_invoice: str | None = None,
	mpesa_receipt: str | None = None,
	confirmed_on_statement=0,
):
	"""A person posts an STK payment as a Payment Entry: one in Needs Review, or one Received
	for a POS invoice that was never completed."""
	frappe.has_permission("Payment Entry", "create", throw=True)
	request = frappe.get_doc("Daraja STK Request", name, for_update=True)
	request.check_permission("write")
	if request.status not in ("Needs Review", "Received") or request.payment_entry:
		frappe.throw(_("Only unposted requests in Needs Review or Received can be posted"))
	if request.invoice_name:
		frappe.throw(_("This payment is applied to {0} {1}").format(_(request.invoice_doctype), request.invoice_name))
	if not cint(request.verified) and not cint(confirmed_on_statement):
		frappe.throw(_("Safaricom never confirmed this payment. Confirm it on the M-Pesa statement first."))

	receipt = request.mpesa_receipt or (mpesa_receipt or "").strip().upper()
	if not re.fullmatch(r"[A-Z0-9]{6,20}", receipt):
		frappe.throw(_("Enter the M-Pesa receipt number from the statement"))

	account = frappe.get_doc("Daraja Account", request.daraja_account)
	if sales_invoice:
		check_sales_invoice(account, sales_invoice, customer)

	pe, problem = post_or_review(
		account,
		amount=request.callback_amount or request.amount,
		receipt=receipt,
		payment_date=request.transaction_time or request.creation,
		customer=customer,
		against_doctype="Sales Invoice" if sales_invoice else None,
		against_name=sales_invoice,
		remarks=_("M-Pesa STK payment {0} from {1}").format(receipt, request.phone),
	)
	if problem:
		frappe.throw(problem)
	request.db_set(
		{"status": "Paid", "payment_entry": pe, "mpesa_receipt": receipt, "customer": customer}
	)
	if not cint(request.verified):
		request.db_set("verified", 1)
	request.add_comment("Info", _("Posted from review by {0}").format(frappe.session.user))
	return pe


def retry_after_seconds(attempts: int) -> int:
	"""How long to leave a request alone after its last check: 30s, 1, 2, 4, then every 5 min.

	Safaricom's STK Push Query often says "still processing" for a while after the callback,
	and rate-limits hard (HTTP 429), so asking again at once only gets refused."""
	return min(30 * 2 ** max(cint(attempts), 0), 300)


def recover_pending() -> None:
	"""Scheduler (every minute): confirm STK requests that are still open, each on its own back-off."""
	now = now_datetime()
	stale = frappe.get_all(
		"Daraja STK Request",
		filters={
			"status": ["in", ["Pending", "Verifying"]],
			"checkout_request_id": ["is", "set"],
			"modified": ["<", add_to_date(now, seconds=-retry_after_seconds(0))],
			"creation": [">", add_to_date(now, days=-2)],
		},
		fields=["name", "modified", "verify_attempts"],
	)
	# Used at the till on Safaricom's result alone, still to be confirmed.
	stale += frappe.get_all(
		"Daraja STK Request",
		filters={
			"status": ["in", ["Received", "Paid"]],
			"verified": 0,
			"confirmation_failed": 0,
			"checkout_request_id": ["is", "set"],
			"modified": ["<", add_to_date(now, seconds=-retry_after_seconds(0))],
			"creation": [">", add_to_date(now, days=-2)],
		},
		fields=["name", "modified", "verify_attempts"],
	)
	for row in stale:
		if row.modified > add_to_date(now, seconds=-retry_after_seconds(row.verify_attempts)):
			continue
		frappe.enqueue(
			verify, queue="short", name=row.name, job_id=f"daraja-stk-verify-{row.name}", deduplicate=True
		)
