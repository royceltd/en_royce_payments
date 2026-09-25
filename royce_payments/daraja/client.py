"""HTTP calls to Daraja for one Daraja Account."""

import hashlib

import frappe
import requests

from royce_payments.daraja import protocol
from royce_payments.daraja.protocol import DarajaError

TIMEOUT_SECONDS = 30


def token_hash(token: str) -> str:
	return hashlib.sha256(token.encode()).hexdigest()


def site_url() -> str:
	"""This site's public https origin. Derived from the site, never a hardcoded domain."""
	host = (frappe.conf.get("host_name") or frappe.local.site).rstrip("/")
	host = host.removeprefix("http://").removeprefix("https://")
	return f"https://{host}"


def callback_url(account, alias: str) -> str:
	"""Public URL for one of the `royce_payments.cb.*` aliases in hooks.py, with this account's token."""
	url = f"{site_url()}/api/method/royce_payments.cb.{alias}?t={account.get_password('callback_token')}"
	protocol.check_callback_url(url)
	return url


def _token_cache_key(account) -> str:
	return f"royce_payments:daraja_token:{account.name}"


def get_access_token(account, refresh: bool = False) -> str:
	key = _token_cache_key(account)
	if not refresh:
		cached = frappe.cache.get_value(key)
		if cached:
			return cached

	response = requests.get(
		protocol.base_url(account.environment) + protocol.OAUTH_PATH,
		auth=(account.get_password("consumer_key"), account.get_password("consumer_secret")),
		timeout=TIMEOUT_SECONDS,
	)
	if response.status_code != 200:
		raise DarajaError(
			f"Daraja rejected the consumer key/secret (HTTP {response.status_code})",
			status=response.status_code,
		)
	data = response.json()
	token = data["access_token"]
	expires_in = int(data.get("expires_in") or 3599)
	frappe.cache.set_value(key, token, expires_in_sec=max(expires_in - 120, 60))
	return token


def post(account, path: str, body: dict) -> dict:
	"""POST to Daraja. Retries once with a fresh token on 401. Raises DarajaError on any failure."""
	for attempt in (1, 2):
		token = get_access_token(account, refresh=attempt == 2)
		try:
			response = requests.post(
				protocol.base_url(account.environment) + path,
				json=body,
				headers={"Authorization": f"Bearer {token}"},
				timeout=TIMEOUT_SECONDS,
			)
		except requests.RequestException as e:
			raise DarajaError(f"Could not reach Daraja: {e}") from e

		if response.status_code == 401 and attempt == 1:
			continue
		try:
			data = response.json()
		except ValueError:
			data = {}

		if response.status_code >= 400 or data.get("errorCode"):
			raise DarajaError(
				data.get("errorMessage") or f"Daraja returned HTTP {response.status_code}",
				code=str(data.get("errorCode") or ""),
				status=response.status_code,
			)
		code = data.get("ResponseCode")
		if code is not None and str(code) != "0":
			raise DarajaError(data.get("ResponseDescription") or "Daraja refused the request", code=str(code))
		return data

	raise DarajaError("Daraja kept rejecting the access token", status=401)
