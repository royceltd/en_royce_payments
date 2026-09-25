"""Daraja wire formats: building requests and reading callbacks.

No Frappe imports on purpose, so these rules can be unit-tested on their own
(`python -m unittest royce_payments.daraja.test_protocol`).
"""

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

SANDBOX_BASE_URL = "https://sandbox.safaricom.co.ke"
PRODUCTION_BASE_URL = "https://api.safaricom.co.ke"

OAUTH_PATH = "/oauth/v1/generate?grant_type=client_credentials"
STK_PUSH_PATH = "/mpesa/stkpush/v1/processrequest"
STK_PUSH_QUERY_PATH = "/mpesa/stkpushquery/v1/query"
C2B_REGISTER_PATH = "/mpesa/c2b/v1/registerurl"
TRANSACTION_STATUS_PATH = "/mpesa/transactionstatus/v1/query"

# STK Push Query answers with this error code while the customer hasn't finished yet.
STK_STILL_PROCESSING = "500.001.1001"
STK_CANCELLED_BY_USER = 1032

# Daraja rejects callback URLs containing any of these (case-insensitive).
BANNED_URL_WORDS = ("m-pesa", "mpesa", "safaricom", "exec", "cmd", "sql", "query")

# Kenya has no daylight saving; Daraja timestamps are Nairobi local time.
NAIROBI = timezone(timedelta(hours=3))

ACCOUNT_REFERENCE_MAX = 12
TRANSACTION_DESC_MAX = 13


class DarajaError(Exception):
	"""A Daraja call failed. `code` is Daraja's errorCode/ResponseCode when it sent one."""

	def __init__(self, message: str, code: str | None = None, status: int | None = None):
		super().__init__(message)
		self.code = code
		self.status = status


def base_url(environment: str) -> str:
	return PRODUCTION_BASE_URL if environment == "Production" else SANDBOX_BASE_URL


def timestamp(now: datetime | None = None) -> str:
	now = now or datetime.now(NAIROBI)
	if now.tzinfo is not None:
		now = now.astimezone(NAIROBI)
	return now.strftime("%Y%m%d%H%M%S")


def stk_password(shortcode: str, passkey: str, ts: str) -> str:
	return base64.b64encode(f"{shortcode}{passkey}{ts}".encode()).decode()


def normalize_phone(raw: str) -> str:
	"""Return a Kenyan mobile number as 2547XXXXXXXX / 2541XXXXXXXX, or raise ValueError."""
	digits = re.sub(r"[\s\-()]", "", str(raw or "")).lstrip("+")
	if digits.startswith("0"):
		digits = "254" + digits[1:]
	elif len(digits) == 9:
		digits = "254" + digits
	if not re.fullmatch(r"254[17]\d{8}", digits):
		raise ValueError(f"Not a Kenyan mobile number: {raw!r}")
	return digits


def check_callback_url(url: str) -> None:
	"""Raise ValueError if Daraja would reject this callback URL."""
	if not url.startswith("https://"):
		raise ValueError(f"Callback URL must use https: {url}")
	lowered = url.lower()
	for word in BANNED_URL_WORDS:
		if word in lowered:
			raise ValueError(f"Callback URL contains {word!r}, which Daraja rejects: {url}")


def account_reference(text: str) -> str:
	"""STK AccountReference: at most 12 characters. Keeps the end, where document numbers differ."""
	cleaned = re.sub(r"[^A-Za-z0-9-]", "", text or "")
	return cleaned[-ACCOUNT_REFERENCE_MAX:] or "PAYMENT"


def transaction_desc(text: str) -> str:
	return (re.sub(r"[^A-Za-z0-9 -]", "", text or "").strip() or "Payment")[:TRANSACTION_DESC_MAX]


def to_amount(value) -> Decimal | None:
	if value in (None, ""):
		return None
	try:
		return Decimal(str(value)).quantize(Decimal("0.01"))
	except InvalidOperation:
		return None


def parse_daraja_time(value) -> datetime | None:
	"""'20260926143015' (Nairobi local) -> naive datetime, as Frappe stores it."""
	text = str(value or "").strip()
	try:
		return datetime.strptime(text, "%Y%m%d%H%M%S")
	except ValueError:
		return None


def _items_to_dict(items, key_name: str, value_name: str) -> dict:
	if isinstance(items, dict):
		items = [items]
	return {i.get(key_name): i.get(value_name) for i in items or [] if isinstance(i, dict) and i.get(key_name)}


@dataclass
class StkResult:
	merchant_request_id: str
	checkout_request_id: str
	result_code: int
	result_desc: str
	amount: Decimal | None = None
	receipt: str | None = None
	phone: str | None = None
	transaction_time: datetime | None = None


def parse_stk_callback(payload: dict) -> StkResult:
	cb = (payload or {}).get("Body", {}).get("stkCallback")
	if not isinstance(cb, dict) or not cb.get("CheckoutRequestID"):
		raise ValueError("Not an STK callback")
	meta = _items_to_dict((cb.get("CallbackMetadata") or {}).get("Item"), "Name", "Value")
	return StkResult(
		merchant_request_id=str(cb.get("MerchantRequestID") or ""),
		checkout_request_id=str(cb["CheckoutRequestID"]),
		result_code=int(cb.get("ResultCode", -1)),
		result_desc=str(cb.get("ResultDesc") or ""),
		amount=to_amount(meta.get("Amount")),
		receipt=str(meta["MpesaReceiptNumber"]) if meta.get("MpesaReceiptNumber") else None,
		phone=str(meta["PhoneNumber"]) if meta.get("PhoneNumber") else None,
		transaction_time=parse_daraja_time(meta.get("TransactionDate")),
	)


@dataclass
class C2BConfirmation:
	trans_id: str
	transaction_type: str
	trans_time: datetime | None
	amount: Decimal | None
	business_shortcode: str
	bill_ref_number: str
	msisdn: str
	payer_name: str


def parse_c2b(payload: dict) -> C2BConfirmation:
	payload = payload or {}
	trans_id = str(payload.get("TransID") or "").strip()
	if not re.fullmatch(r"[A-Z0-9]{6,20}", trans_id):
		raise ValueError("Missing or malformed TransID")
	names = [str(payload.get(k) or "").strip() for k in ("FirstName", "MiddleName", "LastName")]
	return C2BConfirmation(
		trans_id=trans_id,
		transaction_type=str(payload.get("TransactionType") or ""),
		trans_time=parse_daraja_time(payload.get("TransTime")),
		amount=to_amount(payload.get("TransAmount")),
		business_shortcode=str(payload.get("BusinessShortCode") or "").strip(),
		bill_ref_number=str(payload.get("BillRefNumber") or "").strip(),
		msisdn=str(payload.get("MSISDN") or "").strip(),
		payer_name=" ".join(n for n in names if n),
	)


@dataclass
class StatusResult:
	result_code: int
	result_desc: str
	originator_conversation_id: str
	conversation_id: str
	params: dict = field(default_factory=dict)


def parse_status_result(payload: dict) -> StatusResult:
	result = (payload or {}).get("Result")
	if not isinstance(result, dict):
		raise ValueError("Not a Transaction Status result")
	params = _items_to_dict((result.get("ResultParameters") or {}).get("ResultParameter"), "Key", "Value")
	return StatusResult(
		result_code=int(result.get("ResultCode", -1)),
		result_desc=str(result.get("ResultDesc") or ""),
		originator_conversation_id=str(result.get("OriginatorConversationID") or ""),
		conversation_id=str(result.get("ConversationID") or ""),
		params=params,
	)


def status_confirms(result: StatusResult, trans_id: str, amount: Decimal) -> tuple[bool, str]:
	"""Does this Transaction Status result prove the C2B payment we were told about?"""
	if result.result_code != 0:
		return False, f"Safaricom returned {result.result_code}: {result.result_desc}"
	status = str(result.params.get("TransactionStatus") or "")
	if status != "Completed":
		return False, f"Transaction status is {status or 'missing'}, not Completed"
	receipt = str(result.params.get("ReceiptNo") or "")
	if receipt != trans_id:
		return False, f"Receipt {receipt or 'missing'} does not match {trans_id}"
	confirmed = to_amount(result.params.get("Amount"))
	if confirmed is None or confirmed != to_amount(amount):
		return False, f"Amount {confirmed} does not match {amount}"
	return True, "Confirmed by Transaction Status"


def normalize_reference(text: str) -> str:
	return re.sub(r"\s+", "", text or "").upper()
