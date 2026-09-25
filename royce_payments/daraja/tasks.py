from royce_payments.daraja import c2b, stk


def recover_pending() -> None:
	"""Every 5 minutes: finish anything a lost or late callback left hanging."""
	stk.recover_pending()
	c2b.recover_pending()
