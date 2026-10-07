"""Integration tests: needs a bench site with erpnext and royce_payments.

    bench --site <test-site> run-tests --app royce_payments

Daraja itself is never called: client.post is patched. What these guard is ADR-020's
core rule: nothing Safaricom (or anyone) sends posts a payment until it is verified.
"""

from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.app import make_form_dict
from frappe.tests import IntegrationTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from royce_payments.daraja import c2b, callbacks, posting

COMPANY = "_Test Mobile Payments KE"
SHORTCODE = "600638"


def _company():
	if not frappe.db.exists("Company", COMPANY):
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": COMPANY,
				"abbr": "TRPK",
				"country": "Kenya",
				"default_currency": "KES",
				"create_chart_of_accounts_based_on": "Standard Template",
			}
		).insert()
	ledger = frappe.db.get_value("Account", {"company": COMPANY, "account_name": "M-Pesa Test"}, "name")
	if not ledger:
		parent = frappe.db.get_value("Account", {"company": COMPANY, "account_type": "Bank", "is_group": 1}, "name")
		ledger = (
			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": "M-Pesa Test",
					"company": COMPANY,
					"parent_account": parent,
					"account_type": "Bank",
					"account_currency": "KES",
				}
			)
			.insert()
			.name
		)
	return ledger


def _customer(name="_Test Daraja Customer"):
	if not frappe.db.exists("Customer", name):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": name,
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			}
		).insert()
	return name


def _account(initiator=True):
	ledger = _company()
	if not frappe.db.exists("Mode of Payment", "M-Pesa"):
		frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": "M-Pesa", "type": "Phone"}).insert()
	name = "_Test Paybill" + (" verified" if initiator else "")
	if frappe.db.exists("Daraja Account", name):
		return frappe.get_doc("Daraja Account", name)
	return frappe.get_doc(
		{
			"doctype": "Daraja Account",
			"account_name": name,
			"company": COMPANY,
			"receiving_account": ledger,
			"mode_of_payment": "M-Pesa",
			"shortcode_type": "Paybill",
			"business_shortcode": SHORTCODE,
			"consumer_key": "key",
			"consumer_secret": "secret",
			"passkey": "passkey",
			"initiator_name": "apiop" if initiator else None,
			"security_credential": "cred" if initiator else None,
		}
	).insert()


def _confirmation(trans_id, amount="1500.00", bill_ref="", shortcode=SHORTCODE):
	return {
		"TransactionType": "Pay Bill",
		"TransID": trans_id,
		"TransTime": "20260926143015",
		"TransAmount": amount,
		"BusinessShortCode": shortcode,
		"BillRefNumber": bill_ref,
		"MSISDN": "254708374149",
		"FirstName": "Jane",
	}


def _status_payload(originator_id, trans_id, amount, status="Completed"):
	return {
		"Result": {
			"ResultCode": 0,
			"ResultDesc": "ok",
			"OriginatorConversationID": originator_id,
			"ConversationID": "AG_1",
			"ResultParameters": {
				"ResultParameter": [
					{"Key": "ReceiptNo", "Value": trans_id},
					{"Key": "Amount", "Value": amount},
					{"Key": "TransactionStatus", "Value": status},
				]
			},
		}
	}


def _call(endpoint, token, payload):
	"""Call a guest callback through a real JSON request, as Safaricom sends it: the token in
	the query string, the payload as a JSON body. Returns (top-level response, enqueue mock)."""
	builder = EnvironBuilder(method="POST", query_string={"t": token} if token else None, json=payload)
	frappe.local.request = Request(builder.get_environ())
	frappe.local.response = frappe._dict()
	try:
		make_form_dict(frappe.local.request)
		with patch("frappe.enqueue") as enqueue:
			endpoint(**frappe.form_dict)
	finally:
		frappe.local.request = None
	return dict(frappe.local.response), enqueue


class TestDarajaCallbacks(IntegrationTestCase):
	def setUp(self):
		self.account = _account()
		self.token = self.account.get_password("callback_token")

	def test_token_is_hex_and_only_its_hash_is_searchable(self):
		self.assertRegex(self.token, r"^[0-9a-f]{48}$")
		self.assertNotEqual(self.account.callback_token_hash, self.token)

	def test_no_or_wrong_token_does_nothing(self):
		for token in (None, "", "0" * 48):
			response, _ = _call(callbacks.c2b_confirm, token, _confirmation("TOKENTEST1"))
			self.assertEqual(response.get("ResultCode"), 1)
		self.assertFalse(frappe.db.exists("Daraja C2B Payment", "TOKENTEST1"))

	def test_confirmation_is_stored_unverified_and_posts_nothing(self):
		response, _ = _call(callbacks.c2b_confirm, self.token, _confirmation("UNVERIF001", bill_ref=_customer()))
		self.assertEqual(response.get("ResultCode"), 0)
		doc = frappe.get_doc("Daraja C2B Payment", "UNVERIF001")
		self.assertEqual(doc.status, "Unverified")
		self.assertFalse(doc.payment_entry)
		self.assertFalse(frappe.db.exists("Payment Entry", {"reference_no": "UNVERIF001"}))

	def test_resend_is_ignored(self):
		_call(callbacks.c2b_confirm, self.token, _confirmation("RESEND0001"))
		response, _ = _call(callbacks.c2b_confirm, self.token, _confirmation("RESEND0001", amount="999999.00"))
		self.assertEqual(response.get("ResultCode"), 0)
		self.assertEqual(frappe.db.get_value("Daraja C2B Payment", "RESEND0001", "amount"), 1500)

	def test_other_shortcode_is_refused(self):
		response, _ = _call(callbacks.c2b_confirm, self.token, _confirmation("OTHERSC001", shortcode="111111"))
		self.assertEqual(response.get("ResultCode"), 1)
		self.assertFalse(frappe.db.exists("Daraja C2B Payment", "OTHERSC001"))

	def test_status_result_for_a_query_we_never_made_is_ignored(self):
		_call(callbacks.c2b_confirm, self.token, _confirmation("FORGED0001"))
		_response, enqueue = _call(callbacks.status_result, self.token, _status_payload("made-up-id", "FORGED0001", 1500))
		enqueue.assert_not_called()

	def test_token_in_the_query_string_is_honoured_on_a_json_body(self):
		# Regression: Frappe leaves the query string out of form_dict for JSON bodies, which
		# once made every real Safaricom callback fail the token check.
		response, enqueue = _call(callbacks.c2b_confirm, self.token, _confirmation("QSTOKEN001"))
		self.assertEqual(response.get("ResultCode"), 0)
		self.assertTrue(frappe.db.exists("Daraja C2B Payment", "QSTOKEN001"))
		enqueue.assert_called_once()


class TestC2BVerification(IntegrationTestCase):
	def _verifying(self, account, trans_id, amount="1500.00", bill_ref=""):
		c2b.record_confirmation(account, _confirmation(trans_id, amount, bill_ref), "{}")
		response = {"OriginatorConversationID": f"orig-{trans_id}", "ConversationID": "AG_1", "ResponseCode": "0"}
		with patch("royce_payments.daraja.client.post", return_value=response):
			c2b.start_verification(trans_id)
		return frappe.get_doc("Daraja C2B Payment", trans_id)

	def test_without_initiator_waits_for_review(self):
		account = _account(initiator=False)
		c2b.record_confirmation(account, _confirmation("NOINIT0001", bill_ref=_customer()), "{}")
		c2b.start_verification("NOINIT0001")
		doc = frappe.get_doc("Daraja C2B Payment", "NOINIT0001")
		self.assertEqual(doc.status, "Needs Review")
		self.assertFalse(doc.payment_entry)

	def test_mismatched_amount_is_rejected(self):
		doc = self._verifying(_account(), "MISMATCH01", bill_ref=_customer())
		self.assertEqual(doc.status, "Verifying")
		c2b.apply_status_result(doc.name, _status_payload(doc.originator_conversation_id, doc.trans_id, 15))
		doc.reload()
		self.assertEqual(doc.status, "Rejected")
		self.assertFalse(doc.payment_entry)

	def test_verified_payment_posts_once_to_the_customer(self):
		customer = _customer()
		account = _account()
		doc = self._verifying(account, "GOODPAY001", bill_ref=customer)
		c2b.apply_status_result(doc.name, _status_payload(doc.originator_conversation_id, doc.trans_id, 1500))
		doc.reload()
		self.assertEqual(doc.status, "Posted")
		pe = frappe.get_doc("Payment Entry", doc.payment_entry)
		self.assertEqual(pe.docstatus, 1)
		self.assertEqual(pe.party, customer)
		self.assertEqual(pe.paid_to, account.receiving_account)
		self.assertEqual(pe.paid_amount, 1500)

		again = posting.post_payment(
			account, amount=Decimal("1500"), receipt="GOODPAY001", payment_date=doc.trans_time, customer=customer
		)
		self.assertEqual(again, pe.name)

	def test_unknown_account_number_waits_for_review(self):
		doc = self._verifying(_account(), "NOMATCH001", bill_ref="NO-SUCH-THING")
		c2b.apply_status_result(doc.name, _status_payload(doc.originator_conversation_id, doc.trans_id, 1500))
		doc.reload()
		self.assertEqual(doc.status, "Needs Review")
		self.assertTrue(doc.verified)
		self.assertFalse(doc.payment_entry)
