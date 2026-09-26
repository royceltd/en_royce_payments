# Copyright (c) 2026, Royce Technologies LTD and contributors
# For license information, please see license.txt

import re
import secrets

import frappe
from frappe import _
from frappe.model.document import Document

from royce_payments.daraja import client
from royce_payments.daraja.protocol import DarajaError


class DarajaAccount(Document):
	def before_insert(self):
		# Hex only: a random token can then never contain a word Daraja bans in URLs.
		token = secrets.token_hex(24)
		self.callback_token = token
		self.callback_token_hash = client.token_hash(token)

	def validate(self):
		if frappe.get_cached_value("Company", self.company, "default_currency") != "KES":
			frappe.throw(_("Company {0} must use KES for M-Pesa").format(self.company))

		for field in ("business_shortcode", "till_number"):
			value = (self.get(field) or "").strip()
			self.set(field, value)
			if value and not re.fullmatch(r"\d{5,10}", value):
				frappe.throw(_("{0} must be digits only").format(self.meta.get_label(field)))

		if not self.receiving_account:
			self.receiving_account = frappe.db.get_value(
				"Mode of Payment Account",
				{"parent": self.mode_of_payment, "company": self.company},
				"default_account",
			)
		if not self.receiving_account:
			frappe.throw(_("Set a Receiving Ledger, or a default account on Mode of Payment {0}").format(
				self.mode_of_payment
			))
		account = frappe.get_cached_value(
			"Account", self.receiving_account, ["company", "account_type", "is_group"], as_dict=True
		)
		if account.company != self.company or account.is_group or account.account_type not in ("Bank", "Cash"):
			frappe.throw(_("Receiving Ledger must be a Bank or Cash ledger of {0}").format(self.company))


@frappe.whitelist(methods=["POST"])
def test_connection(daraja_account: str):
	account = frappe.get_doc("Daraja Account", daraja_account)
	account.check_permission("write")
	try:
		client.get_access_token(account, refresh=True)
	except DarajaError as e:
		frappe.throw(str(e))
	except Exception as e:
		frappe.throw(_("Could not reach Daraja: {0}").format(e))
	return _("Connected to Daraja ({0})").format(account.environment)
