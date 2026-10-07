"""C2B: payments customers make to the business's own Paybill or Till.

A confirmation from Safaricom is only a claim (Daraja signs nothing). It is stored as
Unverified, confirmed with the Transaction Status API, and only then matched and posted.
See ADR-020 in royce_ip.
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from royce_payments.daraja import client, protocol
from royce_payments.daraja.posting import check_sales_invoice, match_c2b, post_or_review
from royce_payments.daraja.protocol import DarajaError

MAX_VERIFY_ATTEMPTS = 5
# Transaction Status answers by callback; if none came in this long, ask again.
VERIFY_RETRY_MINUTES = 10


@frappe.whitelist(methods=["POST"])
def register_urls(daraja_account: str):
	account = frappe.get_doc("Daraja Account", daraja_account)
	account.check_permission("write")
	body = {
		"ShortCode": account.till_number if account.shortcode_type == "Till" else account.business_shortcode,
		# "Completed": if our validation URL can't be reached, Safaricom completes the
		# payment anyway. Never lose a customer's payment to our downtime.
		"ResponseType": "Completed",
		"ConfirmationURL": client.callback_url(account, "c2b_confirm"),
		"ValidationURL": client.callback_url(account, "c2b_validate"),
	}
	try:
		response = client.post(account, protocol.C2B_REGISTER_PATH, body)
	except DarajaError as e:
		frappe.throw(_("Safaricom did not register the URLs: {0}").format(e))
	account.db_set({"c2b_registered_on": now_datetime(), "c2b_register_response": frappe.as_json(response)})
	return response


def record_confirmation(account, payload: dict, raw: str) -> str | None:
	"""Store a C2B confirmation as Unverified. Returns the record name, or None if it was a resend."""
	c2b = protocol.parse_c2b(payload)
	if c2b.business_shortcode not in (account.business_shortcode, account.till_number):
		raise ValueError(f"Shortcode {c2b.business_shortcode} is not this account's")
	if frappe.db.exists("Daraja C2B Payment", c2b.trans_id):
		return None
	doc = frappe.get_doc(
		{
			"doctype": "Daraja C2B Payment",
			"trans_id": c2b.trans_id,
			"daraja_account": account.name,
			"status": "Unverified",
			"transaction_type": c2b.transaction_type,
			"trans_time": c2b.trans_time,
			"amount": c2b.amount,
			"business_shortcode": c2b.business_shortcode,
			"bill_ref_number": c2b.bill_ref_number,
			"msisdn": c2b.msisdn,
			"payer_name": c2b.payer_name,
			"raw_payload": raw,
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		return None
	return doc.name


def start_verification(name: str) -> None:
	"""Ask Safaricom whether this C2B payment really happened. Safe to run repeatedly."""
	frappe.set_user("Administrator")
	doc = frappe.get_doc("Daraja C2B Payment", name, for_update=True)
	if doc.status not in ("Unverified", "Verifying"):
		return
	account = frappe.get_doc("Daraja Account", doc.daraja_account)

	if not (account.initiator_name and account.get_password("security_credential", raise_exception=False)):
		doc.db_set(
			{
				"status": "Needs Review",
				"review_note": _(
					"Not verified: this Daraja Account has no initiator set up. Check the payment on "
					"the M-Pesa statement before posting it."
				),
			}
		)
		return

	attempts = cint(doc.verify_attempts) + 1
	if attempts > MAX_VERIFY_ATTEMPTS:
		doc.db_set(
			{"status": "Needs Review", "review_note": _("Safaricom did not confirm this payment after several tries.")}
		)
		return

	till = account.shortcode_type == "Till"
	body = {
		"Initiator": account.initiator_name,
		"SecurityCredential": account.get_password("security_credential"),
		"CommandID": "TransactionStatusQuery",
		"TransactionID": doc.trans_id,
		"PartyA": account.till_number if till else account.business_shortcode,
		"IdentifierType": "2" if till else "4",
		"ResultURL": client.callback_url(account, "status_result"),
		"QueueTimeOutURL": client.callback_url(account, "status_timeout"),
		"Remarks": "Verify payment",
		"Occasion": "",
	}
	try:
		response = client.post(account, protocol.TRANSACTION_STATUS_PATH, body)
	except DarajaError as e:
		doc.db_set({"verify_attempts": attempts, "status": "Verifying", "review_note": str(e)[:1000]})
		return
	doc.db_set(
		{
			"verify_attempts": attempts,
			"status": "Verifying",
			"originator_conversation_id": response.get("OriginatorConversationID"),
			"conversation_id": response.get("ConversationID"),
		}
	)


def apply_status_result(name: str, payload: dict) -> None:
	"""Called (via the queue) when Safaricom answers our Transaction Status query."""
	frappe.set_user("Administrator")
	doc = frappe.get_doc("Daraja C2B Payment", name, for_update=True)
	if doc.status != "Verifying":
		return
	result = protocol.parse_status_result(payload)
	doc.db_set("status_result", frappe.as_json(payload))

	if result.result_code != 0:
		# Safaricom couldn't answer (often our initiator setup), which says nothing about
		# the payment itself. A person decides; it is not rejected.
		doc.db_set({"status": "Needs Review", "review_note": f"Verification failed: {result.result_desc}"})
		return
	ok, reason = protocol.status_confirms(result, doc.trans_id, doc.amount)
	if not ok:
		doc.db_set({"status": "Rejected", "review_note": reason})
		return

	doc.db_set(
		{
			"status": "Verified",
			"verified": 1,
			"verified_by": "Safaricom Transaction Status",
			"credit_party": str(result.params.get("CreditPartyName") or "")[:140],
		}
	)
	match_and_post(doc)


def match_and_post(doc) -> None:
	account = frappe.get_doc("Daraja Account", doc.daraja_account)

	stk = frappe.db.get_value("Daraja STK Request", {"mpesa_receipt": doc.trans_id}, ["name", "payment_entry"], as_dict=True)
	if stk:
		# The same money as an STK payment. It posts once, through whichever path gets
		# there first (post_payment is idempotent on the receipt).
		doc.db_set({"status": "Duplicate", "review_note": _("Same payment as STK request {0}").format(stk.name)})
		if stk.payment_entry:
			doc.db_set("payment_entry", stk.payment_entry)
		return

	against_doctype, against_name, customer = match_c2b(account, doc.bill_ref_number)
	if not customer:
		doc.db_set(
			{
				"status": "Needs Review",
				"review_note": _("Account number '{0}' matches no invoice, order or customer").format(
					doc.bill_ref_number or ""
				),
			}
		)
		return
	_post(doc, account, customer, against_doctype, against_name)


def _post(doc, account, customer, against_doctype=None, against_name=None) -> None:
	pe, problem = post_or_review(
		account,
		amount=doc.amount,
		receipt=doc.trans_id,
		payment_date=doc.trans_time or now_datetime(),
		customer=customer,
		against_doctype=against_doctype,
		against_name=against_name,
		remarks=_("M-Pesa payment {0} from {1}, account {2}").format(
			doc.trans_id, doc.payer_name or doc.msisdn, doc.bill_ref_number or "-"
		),
	)
	if problem:
		doc.db_set({"status": "Needs Review", "review_note": problem})
		return
	doc.db_set(
		{
			"status": "Posted",
			"customer": customer,
			"against_doctype": against_doctype,
			"against_name": against_name,
			"payment_entry": pe,
		}
	)


@frappe.whitelist(methods=["POST"])
def post_reviewed(name: str, customer: str, sales_invoice: str | None = None, confirmed_on_statement=0):
	"""A person posts a C2B payment from the review queue."""
	frappe.has_permission("Payment Entry", "create", throw=True)
	doc = frappe.get_doc("Daraja C2B Payment", name, for_update=True)
	doc.check_permission("write")
	if doc.status != "Needs Review" or doc.payment_entry:
		frappe.throw(_("Only unposted payments in Needs Review can be posted"))
	if doc.invoice_name:
		frappe.throw(_("This payment is applied to {0} {1}").format(_(doc.invoice_doctype), doc.invoice_name))
	if not doc.verified and not cint(confirmed_on_statement):
		frappe.throw(_("This payment was not verified with Safaricom. Confirm it on the M-Pesa statement first."))

	account = frappe.get_doc("Daraja Account", doc.daraja_account)
	if sales_invoice:
		check_sales_invoice(account, sales_invoice, customer)

	if not doc.verified:
		doc.db_set({"verified": 1, "verified_by": frappe.session.user})
	_post(doc, account, customer, "Sales Invoice" if sales_invoice else None, sales_invoice)
	doc.add_comment("Info", _("Posted from review by {0}").format(frappe.session.user))
	return doc.payment_entry


def recover_pending() -> None:
	"""Scheduler: start verification for new records and retry ones Safaricom never answered."""
	names = frappe.get_all(
		"Daraja C2B Payment",
		filters={"status": "Unverified", "modified": ["<", add_to_date(now_datetime(), seconds=-60)]},
		pluck="name",
	) + frappe.get_all(
		"Daraja C2B Payment",
		filters={
			"status": "Verifying",
			"modified": ["<", add_to_date(now_datetime(), minutes=-VERIFY_RETRY_MINUTES)],
		},
		pluck="name",
	)
	for name in names:
		frappe.enqueue(
			start_verification, queue="short", name=name, job_id=f"daraja-c2b-verify-{name}", deduplicate=True
		)
