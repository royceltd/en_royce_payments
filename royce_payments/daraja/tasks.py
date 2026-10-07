from royce_payments.daraja import c2b, stk


def recover_pending() -> None:
	"""Every minute: finish anything a lost or late callback left hanging. Each record has its
	own back-off, so this only queries Safaricom when a record is due."""
	stk.recover_pending()
	c2b.recover_pending()
