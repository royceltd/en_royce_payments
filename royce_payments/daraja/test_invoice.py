"""Integration tests: M-Pesa on POS invoices, and the STK flow end to end.

    bench --site <test-site> run-tests --app royce_payments

Daraja is never called: client.post is patched. What these guard: an invoice's M-Pesa amount
always stands for money Safaricom confirmed, and one receipt is recorded exactly once.
"""

from itertools import count
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from royce_payments.daraja import callbacks, invoice, posting, stk
from royce_payments.daraja.test_daraja import COMPANY, _account, _call, _company, _customer

ITEM = "_Test Mobile Payments Service"
POS_PROFILE = "_Test Mobile Payments POS"
# A fresh number per prompt: a second prompt to one number within two minutes is refused.
_phones = count(10000001)
STK_ACCEPTED = {
	"MerchantRequestID": "29115-34620561-1",
	"ResponseCode": "0",
	"ResponseDescription": "Success. Request accepted for processing",
	"CustomerMessage": "Success. Request accepted for processing",
}


def _pos_profile():
	ledger = _company()
	mop = frappe.get_doc("Mode of Payment", "M-Pesa")
	if not any(row.company == COMPANY for row in mop.accounts):
		mop.append("accounts", {"company": COMPANY, "default_account": ledger})
		mop.save()
	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": ITEM,
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
	if not frappe.db.exists("POS Profile", POS_PROFILE):
		company = frappe.get_cached_doc("Company", COMPANY)
		frappe.get_doc(
			{
				"doctype": "POS Profile",
				"name": POS_PROFILE,
				"company": COMPANY,
				"currency": "KES",
				"warehouse": frappe.db.get_value("Warehouse", {"company": COMPANY, "is_group": 0}, "name"),
				"write_off_account": company.write_off_account
				or frappe.db.get_value(
					"Account", {"company": COMPANY, "root_type": "Expense", "is_group": 0}, "name"
				),
				"write_off_cost_center": company.cost_center,
				"write_off_limit": 1,
				"update_stock": 0,
				"payments": [{"mode_of_payment": "M-Pesa", "default": 1}],
			}
		).insert()
	return POS_PROFILE


def _pos_invoice(rate=1000):
	"""An unsubmitted POS-type Sales Invoice, as the POS screen saves it before payment."""
	_account()
	return frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"company": COMPANY,
			"customer": _customer(),
			"currency": "KES",
			"is_pos": 1,
			"pos_profile": _pos_profile(),
			"update_stock": 0,
			"items": [{"item_code": ITEM, "qty": 1, "rate": rate}],
			# The POS screen fills these from the POS Profile; a server-side insert doesn't.
			"payments": [{"mode_of_payment": "M-Pesa", "amount": 0}],
		}
	).insert()


def _set_mpesa(doc, amount):
	for row in doc.payments:
		if row.mode_of_payment == "M-Pesa":
			row.amount = amount
	doc.save()


def _received_c2b(trans_id, amount=1000):
	"""A Paybill/Till payment Safaricom confirmed that matched nothing: what a cashier picks."""
	return frappe.get_doc(
		{
			"doctype": "Daraja C2B Payment",
			"trans_id": trans_id,
			"daraja_account": _account().name,
			"status": "Needs Review",
			"verified": 1,
			"amount": amount,
			"trans_time": frappe.utils.now_datetime(),
			"business_shortcode": _account().business_shortcode,
			"msisdn": "254708374149",
		}
	).insert(ignore_permissions=True)


def _stk_callback(checkout_id, receipt, amount, code=0):
	items = [
		{"Name": "Amount", "Value": amount},
		{"Name": "MpesaReceiptNumber", "Value": receipt},
		{"Name": "TransactionDate", "Value": 20261005101500},
		{"Name": "PhoneNumber", "Value": 254708374149},
	]
	return {
		"Body": {
			"stkCallback": {
				"MerchantRequestID": "29115-34620561-1",
				"CheckoutRequestID": checkout_id,
				"ResultCode": code,
				"ResultDesc": "The service request is processed successfully.",
				"CallbackMetadata": {"Item": items} if code == 0 else None,
			}
		}
	}


def _prompt(doc, amount, checkout_id):
	"""Send an STK prompt for a document, with Daraja patched out. Returns the request name."""
	accepted = {**STK_ACCEPTED, "CheckoutRequestID": checkout_id}
	with patch("royce_payments.daraja.client.post", return_value=accepted), patch("frappe.db.commit"):
		return stk.request_payment(doc.doctype, doc.name, f"07{next(_phones)}", amount)["name"]


def _complete_prompt(name, receipt, amount):
	"""The customer enters their PIN: Safaricom's callback, then our STK Push Query."""
	request = frappe.get_doc("Daraja STK Request", name)
	token = _account().get_password("callback_token")
	_call(callbacks.stk_result, token, _stk_callback(request.checkout_request_id, receipt, amount))
	paid = {
		"ResponseCode": "0",
		"ResultCode": "0",
		"ResultDesc": "The service request is processed successfully.",
	}
	with patch("royce_payments.daraja.client.post", return_value=paid):
		stk.verify(name)
	return frappe.get_doc("Daraja STK Request", name)


class TestMpesaOnPOSInvoice(IntegrationTestCase):
	def test_typed_in_mpesa_without_money_is_refused(self):
		doc = _pos_invoice()
		_set_mpesa(doc, 1000)
		self.assertRaisesRegex(frappe.ValidationError, "has been received", doc.submit)

	def test_prompt_paid_on_phone_is_recorded_by_the_invoice(self):
		doc = _pos_invoice()
		name = _prompt(doc, 1000, "ws_CO_POS_0001")
		request = _complete_prompt(name, "POSSTK0001", 1000)
		self.assertEqual(request.status, "Received")
		self.assertTrue(request.verified)

		_set_mpesa(doc, 1000)
		doc.submit()
		self.assertEqual(frappe.db.get_value("Daraja STK Request", name, "status"), "Paid")
		row = next(p for p in doc.payments if p.mode_of_payment == "M-Pesa")
		self.assertEqual(row.reference_no, "POSSTK0001")
		# The invoice carries the money; no Payment Entry on top of it.
		self.assertFalse(frappe.db.exists("Payment Entry", {"reference_no": "POSSTK0001"}))

	def test_prompt_cannot_ask_for_more_than_the_invoice(self):
		doc = _pos_invoice(rate=500)
		_prompt(doc, 300, "ws_CO_POS_0002")
		self.assertRaises(frappe.ValidationError, _prompt, doc, 300, "ws_CO_POS_0003")

	def test_till_payment_applied_then_cancelled_is_free_again(self):
		doc = _pos_invoice()
		_received_c2b("POSC2B0001")
		summary = invoice.apply(
			doc.doctype, doc.name, [{"doctype": "Daraja C2B Payment", "name": "POSC2B0001"}]
		)
		self.assertEqual(summary["total"], 1000)

		_set_mpesa(doc, 1000)
		doc.submit()
		c2b = frappe.get_doc("Daraja C2B Payment", "POSC2B0001")
		self.assertEqual((c2b.status, c2b.against_name), ("Posted", doc.name))

		doc.cancel()
		c2b.reload()
		self.assertEqual(c2b.status, "Needs Review")
		self.assertFalse(c2b.invoice_name)

	def test_one_payment_cannot_go_on_two_invoices(self):
		first, second = _pos_invoice(), _pos_invoice()
		_received_c2b("POSC2B0002")
		payment = [{"doctype": "Daraja C2B Payment", "name": "POSC2B0002"}]
		invoice.apply(first.doctype, first.name, payment)
		self.assertRaises(frappe.ValidationError, invoice.apply, second.doctype, second.name, payment)

	def test_unconfirmed_payment_cannot_be_applied(self):
		doc = _pos_invoice()
		c2b = _received_c2b("POSC2B0003")
		c2b.db_set({"verified": 0, "status": "Verifying"})
		payment = [{"doctype": "Daraja C2B Payment", "name": "POSC2B0003"}]
		self.assertRaises(frappe.ValidationError, invoice.apply, doc.doctype, doc.name, payment)

	def test_more_applied_than_entered_is_refused(self):
		doc = _pos_invoice()
		_received_c2b("POSC2B0004", amount=1000)
		invoice.apply(doc.doctype, doc.name, [{"doctype": "Daraja C2B Payment", "name": "POSC2B0004"}])
		_set_mpesa(doc, 400)
		self.assertRaisesRegex(frappe.ValidationError, "are applied to this invoice", doc.submit)

	def test_deleting_the_draft_frees_its_payments(self):
		doc = _pos_invoice()
		_received_c2b("POSC2B0005")
		invoice.apply(doc.doctype, doc.name, [{"doctype": "Daraja C2B Payment", "name": "POSC2B0005"}])
		doc.delete()
		self.assertFalse(frappe.db.get_value("Daraja C2B Payment", "POSC2B0005", "invoice_name"))

	def test_receipt_on_an_invoice_never_gets_a_payment_entry(self):
		doc = _pos_invoice()
		_received_c2b("POSC2B0006")
		invoice.apply(doc.doctype, doc.name, [{"doctype": "Daraja C2B Payment", "name": "POSC2B0006"}])
		with self.assertRaises(posting.NeedsReview):
			posting.post_payment(
				_account(),
				amount=1000,
				receipt="POSC2B0006",
				payment_date=frappe.utils.today(),
				customer=_customer(),
			)


class TestSTKFlow(IntegrationTestCase):
	def _invoice(self):
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": COMPANY,
				"customer": _customer(),
				"currency": "KES",
				"items": [{"item_code": ITEM, "qty": 1, "rate": 2000}],
			}
		)
		_account()
		_pos_profile()  # creates the item
		doc.insert()
		doc.submit()
		return doc

	def test_submitted_invoice_is_paid_by_payment_entry(self):
		doc = self._invoice()
		name = _prompt(doc, 2000, "ws_CO_SI_0001")
		request = _complete_prompt(name, "SISTK00001", 2000)
		self.assertEqual(request.status, "Paid")
		pe = frappe.get_doc("Payment Entry", request.payment_entry)
		self.assertEqual(pe.references[0].reference_name, doc.name)
		self.assertEqual(frappe.db.get_value("Sales Invoice", doc.name, "outstanding_amount"), 0)

	def test_cancelled_on_phone_is_recorded_not_crashed(self):
		# Regression: a callback with no payment (cancelled, timed out) has no Amount, and
		# writing None to the NOT NULL amount column crashed the endpoint, so Safaricom
		# got an error and kept retrying.
		doc = self._invoice()
		name = _prompt(doc, 2000, "ws_CO_SI_0004")
		checkout_id = frappe.db.get_value("Daraja STK Request", name, "checkout_request_id")
		token = _account().get_password("callback_token")
		response, _enqueue = _call(callbacks.stk_result, token, _stk_callback(checkout_id, None, None, code=1032))
		self.assertEqual(response.get("ResultCode"), 0)
		self.assertEqual(frappe.db.get_value("Daraja STK Request", name, "callback_result_code"), "1032")

		cancelled = {"ResponseCode": "0", "ResultCode": "1032", "ResultDesc": "Request cancelled by user"}
		with patch("royce_payments.daraja.client.post", return_value=cancelled):
			stk.verify(name)
		self.assertEqual(frappe.db.get_value("Daraja STK Request", name, "status"), "Cancelled")
		self.assertFalse(frappe.db.exists("Payment Entry", {"reference_no": ["is", "set"], "party": doc.customer, "paid_amount": 2000}))

	def test_late_callback_after_needs_review_is_kept_and_finishes_the_payment(self):
		doc = self._invoice()
		name = _prompt(doc, 2000, "ws_CO_SI_0002")
		# The scheduler gave up before Safaricom's callback came.
		frappe.db.set_value("Daraja STK Request", name, {"status": "Needs Review", "verify_attempts": 10})

		request = _complete_prompt(name, "SISTK00002", 2000)
		self.assertEqual(request.mpesa_receipt, "SISTK00002")
		self.assertEqual(request.status, "Paid")

	def test_needs_review_without_receipt_posts_after_statement_check(self):
		doc = self._invoice()
		name = _prompt(doc, 2000, "ws_CO_SI_0003")
		frappe.db.set_value("Daraja STK Request", name, "status", "Needs Review")

		self.assertRaises(
			frappe.ValidationError, stk.post_reviewed, name, doc.customer, doc.name, "SISTK00003"
		)
		pe = stk.post_reviewed(name, doc.customer, doc.name, "SISTK00003", confirmed_on_statement=1)
		self.assertEqual(frappe.db.get_value("Payment Entry", pe, "reference_no"), "SISTK00003")
		self.assertEqual(frappe.db.get_value("Daraja STK Request", name, "status"), "Paid")


class TestUseCallbackAtTheTill(IntegrationTestCase):
	"""Daraja Account opt-in (pos_use_callback): a paid POS prompt counts on Safaricom's result,
	and is confirmed afterwards."""

	def setUp(self):
		_account().db_set("pos_use_callback", 1)

	def tearDown(self):
		_account().db_set("pos_use_callback", 0)

	def _paid_callback(self, doc, amount, checkout_id, receipt, paid_amount=None):
		name = _prompt(doc, amount, checkout_id)
		token = _account().get_password("callback_token")
		_call(callbacks.stk_result, token, _stk_callback(checkout_id, receipt, paid_amount or amount))
		return name

	def test_paid_prompt_counts_at_once_then_is_confirmed(self):
		doc = _pos_invoice()
		name = self._paid_callback(doc, 1000, "ws_CO_FAST_0001", "FASTPAY001")
		request = frappe.get_doc("Daraja STK Request", name)
		self.assertEqual((request.status, request.verified), ("Received", 0))

		# The sale completes before Safaricom confirms.
		_set_mpesa(doc, 1000)
		doc.submit()
		self.assertEqual(frappe.db.get_value("Daraja STK Request", name, "status"), "Paid")

		confirmed = {"ResponseCode": "0", "ResultCode": "0", "ResultDesc": "ok"}
		with patch("royce_payments.daraja.client.post", return_value=confirmed):
			stk.verify(name)
		request.reload()
		self.assertEqual((request.status, request.verified, request.confirmation_failed), ("Paid", 1, 0))
		self.assertFalse(frappe.db.exists("Payment Entry", {"reference_no": "FASTPAY001"}))

	def test_safaricom_disagreeing_flags_it_and_tells_accounts_managers(self):
		doc = _pos_invoice()
		name = self._paid_callback(doc, 1000, "ws_CO_FAST_0002", "FASTPAY002")
		_set_mpesa(doc, 1000)
		doc.submit()

		failed = {"ResponseCode": "0", "ResultCode": "1", "ResultDesc": "The balance is insufficient"}
		with patch("royce_payments.daraja.client.post", return_value=failed):
			stk.verify(name)
		request = frappe.get_doc("Daraja STK Request", name)
		self.assertTrue(request.confirmation_failed)
		self.assertEqual(request.status, "Paid")  # the sale stands; a person decides
		self.assertTrue(frappe.db.exists("Notification Log", {"document_name": name}))
		self.assertTrue(
			frappe.db.exists(
				"Comment", {"reference_doctype": doc.doctype, "reference_name": doc.name, "content": ["like", "%FASTPAY002%"]}
			)
		)

	def test_a_failed_confirmation_stops_counting_on_an_unsubmitted_sale(self):
		doc = _pos_invoice()
		name = self._paid_callback(doc, 1000, "ws_CO_FAST_0003", "FASTPAY003")
		failed = {"ResponseCode": "0", "ResultCode": "1", "ResultDesc": "Failed"}
		with patch("royce_payments.daraja.client.post", return_value=failed):
			stk.verify(name)
		_set_mpesa(doc, 1000)
		self.assertRaisesRegex(frappe.ValidationError, "has been received", doc.submit)

	def test_a_result_for_a_different_amount_waits_for_confirmation(self):
		doc = _pos_invoice()
		name = self._paid_callback(doc, 1000, "ws_CO_FAST_0004", "FASTPAY004", paid_amount=10)
		self.assertEqual(frappe.db.get_value("Daraja STK Request", name, "status"), "Verifying")

	def test_off_by_default_a_paid_prompt_waits_for_confirmation(self):
		_account().db_set("pos_use_callback", 0)
		doc = _pos_invoice()
		name = self._paid_callback(doc, 1000, "ws_CO_FAST_0005", "FASTPAY005")
		self.assertEqual(frappe.db.get_value("Daraja STK Request", name, "status"), "Verifying")
		summary = invoice.get_summary(doc.doctype, doc.name)
		self.assertEqual(summary["total"], 0)
		self.assertTrue(summary["pending"][0]["paid"])  # shown as "Paid, confirming..."
