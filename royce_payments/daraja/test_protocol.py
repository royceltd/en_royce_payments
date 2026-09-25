"""Pure tests for the Daraja wire rules. No Frappe needed:

    python -m unittest royce_payments.daraja.test_protocol
"""

import base64
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from royce_payments.daraja import protocol


class TestPhone(unittest.TestCase):
	def test_accepts_common_kenyan_formats(self):
		for raw in ("0712345678", "0712 345 678", "+254712345678", "254712345678", "712345678", "0712-345-678"):
			self.assertEqual(protocol.normalize_phone(raw), "254712345678", raw)
		self.assertEqual(protocol.normalize_phone("0110345678"), "254110345678")

	def test_rejects_non_kenyan_and_landlines(self):
		for raw in ("", "12345", "0201234567", "+255712345678", "07123456789", "abc"):
			with self.assertRaises(ValueError, msg=raw):
				protocol.normalize_phone(raw)


class TestStkRequest(unittest.TestCase):
	def test_password_is_base64_of_shortcode_passkey_timestamp(self):
		pw = protocol.stk_password("174379", "passkey", "20260926101500")
		self.assertEqual(base64.b64decode(pw).decode(), "174379passkey20260926101500")

	def test_timestamp_is_nairobi_time(self):
		utc = datetime(2026, 9, 26, 22, 30, 0, tzinfo=timezone.utc)
		self.assertEqual(protocol.timestamp(utc), "20260927013000")

	def test_account_reference_keeps_the_end_and_fits(self):
		self.assertEqual(protocol.account_reference("ACC-SINV-2026-00042"), "V-2026-00042")
		self.assertEqual(protocol.account_reference("SO 12"), "SO12")
		self.assertEqual(protocol.account_reference(""), "PAYMENT")

	def test_transaction_desc_fits(self):
		self.assertLessEqual(len(protocol.transaction_desc("Royce Technologies Limited")), 13)


class TestCallbackUrl(unittest.TestCase):
	def test_accepts_our_alias_urls(self):
		protocol.check_callback_url("https://acme.royceerp.com/api/method/royce_payments.cb.c2b_confirm?t=ab12")

	def test_rejects_banned_words_any_case(self):
		for url in (
			"https://acme.royceerp.com/api/method/royce_mpesa.api.confirm",
			"https://acme.royceerp.com/api/method/x.M-PESA",
			"https://acme.royceerp.com/api/method/x.Safaricom",
			"https://acme.royceerp.com/api/method/x.status_query",
		):
			with self.assertRaises(ValueError, msg=url):
				protocol.check_callback_url(url)

	def test_rejects_plain_http(self):
		with self.assertRaises(ValueError):
			protocol.check_callback_url("http://acme.royceerp.com/api/method/royce_payments.cb.stk_result")

	def test_hex_tokens_can_never_hit_a_banned_word(self):
		# The callback token is secrets.token_hex(); every banned word needs a non-hex letter.
		for word in protocol.BANNED_URL_WORDS:
			self.assertTrue(set(word) - set("0123456789abcdef"), word)


class TestStkCallback(unittest.TestCase):
	def test_success(self):
		result = protocol.parse_stk_callback(
			{
				"Body": {
					"stkCallback": {
						"MerchantRequestID": "29115-34620561-1",
						"CheckoutRequestID": "ws_CO_191220191020363925",
						"ResultCode": 0,
						"ResultDesc": "The service request is processed successfully.",
						"CallbackMetadata": {
							"Item": [
								{"Name": "Amount", "Value": 1.00},
								{"Name": "MpesaReceiptNumber", "Value": "NLJ7RT61SV"},
								{"Name": "Balance"},
								{"Name": "TransactionDate", "Value": 20191219102115},
								{"Name": "PhoneNumber", "Value": 254708374149},
							]
						},
					}
				}
			}
		)
		self.assertEqual(result.checkout_request_id, "ws_CO_191220191020363925")
		self.assertEqual(result.result_code, 0)
		self.assertEqual(result.amount, Decimal("1.00"))
		self.assertEqual(result.receipt, "NLJ7RT61SV")
		self.assertEqual(result.phone, "254708374149")
		self.assertEqual(result.transaction_time, datetime(2019, 12, 19, 10, 21, 15))

	def test_cancelled_has_no_metadata(self):
		result = protocol.parse_stk_callback(
			{
				"Body": {
					"stkCallback": {
						"MerchantRequestID": "1",
						"CheckoutRequestID": "ws_CO_1",
						"ResultCode": 1032,
						"ResultDesc": "Request cancelled by user",
					}
				}
			}
		)
		self.assertEqual(result.result_code, protocol.STK_CANCELLED_BY_USER)
		self.assertIsNone(result.receipt)
		self.assertIsNone(result.amount)

	def test_garbage_is_rejected(self):
		for payload in ({}, {"Body": {}}, {"Body": {"stkCallback": {"ResultCode": 0}}}, None):
			with self.assertRaises(ValueError):
				protocol.parse_stk_callback(payload)


class TestC2B(unittest.TestCase):
	PAYLOAD = {
		"TransactionType": "Pay Bill",
		"TransID": "RKTQDM7W6S",
		"TransTime": "20260926143015",
		"TransAmount": "1500.00",
		"BusinessShortCode": "600638",
		"BillRefNumber": " ACC-SINV-2026-00042 ",
		"InvoiceNumber": "",
		"OrgAccountBalance": "49197.00",
		"ThirdPartyTransID": "",
		"MSISDN": "254708374149",
		"FirstName": "Jane",
		"MiddleName": "",
		"LastName": "Doe",
	}

	def test_parse(self):
		c2b = protocol.parse_c2b(self.PAYLOAD)
		self.assertEqual(c2b.trans_id, "RKTQDM7W6S")
		self.assertEqual(c2b.amount, Decimal("1500.00"))
		self.assertEqual(c2b.bill_ref_number, "ACC-SINV-2026-00042")
		self.assertEqual(c2b.payer_name, "Jane Doe")
		self.assertEqual(c2b.trans_time, datetime(2026, 9, 26, 14, 30, 15))

	def test_rejects_missing_or_odd_trans_id(self):
		for trans_id in ("", "x", "'; DROP TABLE", "abc def"):
			with self.assertRaises(ValueError, msg=trans_id):
				protocol.parse_c2b({**self.PAYLOAD, "TransID": trans_id})


def status_result(**params):
	defaults = {"TransactionStatus": "Completed", "ReceiptNo": "RKTQDM7W6S", "Amount": 1500}
	defaults.update(params)
	return protocol.parse_status_result(
		{
			"Result": {
				"ResultType": 0,
				"ResultCode": 0,
				"ResultDesc": "The service request is processed successfully.",
				"OriginatorConversationID": "1236-7134259-1",
				"ConversationID": "AG_20260926_0000",
				"TransactionID": "SI_QUERY_ID",
				"ResultParameters": {"ResultParameter": [{"Key": k, "Value": v} for k, v in defaults.items()]},
			}
		}
	)


class TestTransactionStatus(unittest.TestCase):
	def test_parse(self):
		result = status_result()
		self.assertEqual(result.originator_conversation_id, "1236-7134259-1")
		self.assertEqual(result.params["ReceiptNo"], "RKTQDM7W6S")

	def test_single_parameter_as_object(self):
		result = protocol.parse_status_result(
			{"Result": {"ResultCode": 0, "ResultParameters": {"ResultParameter": {"Key": "Amount", "Value": 5}}}}
		)
		self.assertEqual(result.params, {"Amount": 5})

	def test_confirms_matching_payment(self):
		ok, _reason = protocol.status_confirms(status_result(), "RKTQDM7W6S", Decimal("1500.00"))
		self.assertTrue(ok)

	def test_refuses_anything_that_does_not_match(self):
		cases = [
			(status_result(TransactionStatus="Failed"), "RKTQDM7W6S", "1500"),
			(status_result(ReceiptNo="OTHER12345"), "RKTQDM7W6S", "1500"),
			(status_result(Amount=1499), "RKTQDM7W6S", "1500"),
			(status_result(Amount=None), "RKTQDM7W6S", "1500"),
			(status_result(), "RKTQDM7W6S", "15000"),
		]
		for result, trans_id, amount in cases:
			ok, reason = protocol.status_confirms(result, trans_id, Decimal(amount))
			self.assertFalse(ok, reason)

	def test_refuses_error_result(self):
		result = protocol.parse_status_result({"Result": {"ResultCode": 2001, "ResultDesc": "Invalid initiator"}})
		ok, _reason = protocol.status_confirms(result, "RKTQDM7W6S", Decimal("1500"))
		self.assertFalse(ok)


if __name__ == "__main__":
	unittest.main()
