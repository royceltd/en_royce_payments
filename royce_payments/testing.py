"""`before_tests` hook: make a fresh site testable.

A new site has no company, fiscal year or warehouse types until the setup wizard runs, and
`run-tests --app royce_payments` only runs this app's before_tests, not ERPNext's. So complete
setup here, as a Kenyan tenant would.
"""

import frappe
from frappe.utils import now_datetime


def before_tests():
	frappe.clear_cache()
	if not frappe.get_list("Company"):
		from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

		year = now_datetime().year
		setup_complete(
			{
				"currency": "KES",
				"full_name": "Test User",
				"company_name": "_Test Company KE",
				"timezone": "Africa/Nairobi",
				"company_abbr": "_TCKE",
				"industry": "Retail",
				"country": "Kenya",
				"fy_start_date": f"{year}-01-01",
				"fy_end_date": f"{year}-12-31",
				"language": "english",
				"company_tagline": "Testing",
				"email": "test@example.com",
				"password": "test",
				"chart_of_accounts": "Standard",
			}
		)
		_rebase_future_scheduled_jobs()
	frappe.db.commit()  # nosemgrep


def _rebase_future_scheduled_jobs():
	"""Jobs registered before setup set the time zone were stamped in Frappe's fallback
	(Asia/Kolkata, 2.5h ahead of Nairobi). A never-run job's first run counts from its
	creation, so the scheduler would skip them for hours on a site used for manual testing.
	Same fix as kenyan_accountant's site_defaults, which may not be installed here."""
	now = frappe.utils.now_datetime()
	for name in frappe.get_all(
		"Scheduled Job Type",
		filters={"creation": [">", now], "last_execution": ["is", "not set"]},
		pluck="name",
	):
		frappe.db.set_value("Scheduled Job Type", name, "creation", now, update_modified=False)
