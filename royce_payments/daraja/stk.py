"""STK Push (Mpesa Express): prompt a customer's phone to pay a Sales Invoice or Sales Order."""

import math

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from royce_payments.daraja import client, protocol
from royce_payments.daraja.posting import outstanding_for, post_or_review
from royce_payments.daraja.protocol import DarajaError

SUPPORTED_DOCTYPES = ("Sales Invoice", "Sales Order")
# One open prompt per phone at a time; a second one within this window is refused.
PHONE_COOLDOWN_SECONDS = 120
MAX_VERIFY_ATTEMPTS = 10
FINAL_STATUSES = ("Paid", "Failed", "Cancelled", "Needs Review")


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
	doc.check_permission("read")
	frappe.has_permission("Payment Entry", "create", throw=True)
	if doc.docstatus != 1:
		frappe.throw(_("Submit the {0} before requesting payment").format(_(reference_doctype)))
	if doc.currency != "KES":
		frappe.throw(_("M-Pesa payments must be in KES"))

	try:
		phone = protocol.normalize_phone(phone)
	except ValueError:
		frappe.throw(_("Enter a Kenyan mobile number, for example 0712 345 678"))

	amount = cint(amount)
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
	}


def verify(name: str) -> None:
	"""Confirm an STK request with STK Push Query, then post it. Safe to run repeatedly."""
	frappe.set_user("Administrator")
	request = frappe.get_doc("Daraja STK Request", name, for_update=True)
	if request.status in FINAL_STATUSES or not request.checkout_request_id:
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
		status = "Needs Review" if attempts >= MAX_VERIFY_ATTEMPTS else request.status
		request.db_set({"verify_attempts": attempts, "status": status, "result_desc": str(e)[:1000]})
		return

	code = cint(result.get("ResultCode", -1))
	desc = result.get("ResultDesc") or ""
	if code != 0:
		status = "Cancelled" if code == protocol.STK_CANCELLED_BY_USER else "Failed"
		request.db_set({"verify_attempts": attempts, "status": status, "result_code": code, "result_desc": desc})
		return

	request.db_set({"verify_attempts": attempts, "result_code": 0, "result_desc": desc})
	if not request.mpesa_receipt:
		# Paid, but the receipt only comes in the callback, which hasn't arrived. The
		# callback, or a person, finishes this.
		if attempts >= MAX_VERIFY_ATTEMPTS:
			request.db_set("status", "Needs Review")
		return

	_post(request, account)


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


def recover_pending() -> None:
	"""Scheduler: resolve STK requests whose callback never came."""
	stale = frappe.get_all(
		"Daraja STK Request",
		filters={
			"status": ["in", ["Pending", "Verifying"]],
			"checkout_request_id": ["is", "set"],
			"modified": ["<", add_to_date(now_datetime(), seconds=-90)],
			"creation": [">", add_to_date(now_datetime(), days=-2)],
		},
		pluck="name",
	)
	for name in stale:
		frappe.enqueue(verify, queue="short", name=name, job_id=f"daraja-stk-verify-{name}", deduplicate=True)
