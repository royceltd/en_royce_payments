"""Endpoints Safaricom calls. Reached through the `royce_payments.cb.*` aliases in hooks.py.

Everything here is guest-accessible, so every endpoint:
- does nothing at all without a valid per-account token (`?t=`),
- only stores what it received and queues work, never posts anything itself,
- answers in Safaricom's own shape (top-level ResultCode), fast.
"""

import json

import frappe

from royce_payments.daraja import c2b, client, posting, protocol, stk

MAX_BODY_BYTES = 64 * 1024
ACCEPTED = {"ResultCode": 0, "ResultDesc": "Accepted"}
REJECTED = {"ResultCode": 1, "ResultDesc": "Rejected"}
# Daraja's code for "invalid account number" in a validation response.
REJECT_ACCOUNT_NUMBER = {"ResultCode": "C2B00012", "ResultDesc": "Rejected"}


def _respond(body: dict) -> None:
	frappe.response.update(body)


def _account():
	token = frappe.form_dict.get("t")
	if not token or not isinstance(token, str):
		return None
	name = frappe.db.get_value(
		"Daraja Account", {"callback_token_hash": client.token_hash(token), "enabled": 1}, "name"
	)
	return frappe.get_doc("Daraja Account", name) if name else None


def _payload() -> tuple[dict, str]:
	raw = frappe.request.get_data(as_text=True) if frappe.request else ""
	if len(raw) > MAX_BODY_BYTES:
		raise ValueError("Callback body too large")
	data = json.loads(raw or "{}")
	if not isinstance(data, dict):
		raise ValueError("Callback body is not a JSON object")
	return data, raw


@frappe.whitelist(allow_guest=True, methods=["POST"])
def stk_result(**kwargs):
	account = _account()
	if not account:
		return _respond(REJECTED)
	try:
		payload, raw = _payload()
		result = protocol.parse_stk_callback(payload)
	except ValueError:
		return _respond(REJECTED)

	name = frappe.db.get_value(
		"Daraja STK Request",
		{"checkout_request_id": result.checkout_request_id, "daraja_account": account.name},
		"name",
	)
	if not name:
		frappe.log_error("Daraja STK callback for an unknown request", raw, reference_doctype="Daraja Account")
		return _respond(ACCEPTED)

	request = frappe.get_doc("Daraja STK Request", name)
	if request.status == "Pending":
		request.db_set(
			{
				"status": "Verifying",
				"callback_payload": raw,
				"callback_result_code": result.result_code,
				"callback_amount": result.amount,
				"mpesa_receipt": result.receipt,
				"transaction_time": result.transaction_time,
			}
		)
		frappe.enqueue(
			stk.verify, queue="short", name=name, enqueue_after_commit=True,
			job_id=f"daraja-stk-verify-{name}", deduplicate=True,
		)
	return _respond(ACCEPTED)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def c2b_confirm(**kwargs):
	account = _account()
	if not account:
		return _respond(REJECTED)
	try:
		payload, raw = _payload()
		name = c2b.record_confirmation(account, payload, raw)
	except ValueError as e:
		frappe.log_error("Daraja C2B confirmation refused", str(e), reference_doctype="Daraja Account")
		return _respond(REJECTED)

	if name:
		frappe.enqueue(
			c2b.start_verification, queue="short", name=name, enqueue_after_commit=True,
			job_id=f"daraja-c2b-verify-{name}", deduplicate=True,
		)
	return _respond(ACCEPTED)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def c2b_validate(**kwargs):
	"""Only called if Safaricom enabled external validation for the shortcode.

	Rejects a payment only when the account has validation on and the account number
	matches nothing. Any error accepts: a customer's payment must never fail on our side.
	"""
	try:
		account = _account()
		if not account:
			return _respond(REJECTED)
		if not account.c2b_validation:
			return _respond(ACCEPTED)
		payload, _raw = _payload()
		bill_ref = str(payload.get("BillRefNumber") or "")
		_dt, _dn, customer = posting.match_c2b(account, bill_ref)
		return _respond(ACCEPTED if customer else REJECT_ACCOUNT_NUMBER)
	except Exception:
		frappe.log_error("Daraja C2B validation error")
		return _respond(ACCEPTED)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def status_result(**kwargs):
	account = _account()
	if not account:
		return _respond(REJECTED)
	try:
		payload, raw = _payload()
		result = protocol.parse_status_result(payload)
	except ValueError:
		return _respond(REJECTED)

	# Only answers to a query we made count: the OriginatorConversationID was issued to
	# us by Safaricom when we asked, and is never exposed anywhere public.
	name = result.originator_conversation_id and frappe.db.get_value(
		"Daraja C2B Payment",
		{
			"originator_conversation_id": result.originator_conversation_id,
			"daraja_account": account.name,
			"status": "Verifying",
		},
		"name",
	)
	if not name:
		frappe.log_error("Daraja status result for an unknown query", raw, reference_doctype="Daraja Account")
		return _respond(ACCEPTED)

	frappe.enqueue(
		c2b.apply_status_result, queue="short", name=name, payload=payload, enqueue_after_commit=True
	)
	return _respond(ACCEPTED)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def status_timeout(**kwargs):
	# Nothing to do: the scheduler asks again after VERIFY_RETRY_MINUTES.
	return _respond(ACCEPTED if _account() else REJECTED)
