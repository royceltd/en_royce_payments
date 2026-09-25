# Royce Payments

Payment integrations for ERPNext v16. Phase 1: M-Pesa through Safaricom's Daraja API.

- **STK Push:** "Request M-Pesa Payment" (under Create) on submitted Sales Invoices and
  Sales Orders sends a PIN prompt to the customer's phone. The payment is confirmed with
  Safaricom and posted as a Payment Entry against the document.
- **Paybill/Till (C2B):** payments customers make to the business's own number are
  recorded, confirmed with Safaricom's Transaction Status API, matched by the account
  number the payer typed (Sales Invoice, Sales Order or Customer ID) and posted. Anything
  unmatched or unverifiable waits in **Daraja C2B Payment** with status *Needs Review*.

Design and security model: ADR-020 in the `royce_ip` repo. The short version: a callback
from Safaricom is a claim, not proof. Nothing is posted until it is verified.

## Setup (per company)

1. Create an app on developer.safaricom.co.ke (sandbox first; Go-Live for production).
2. **Daraja Account** → new: company, Paybill or Till, shortcode, consumer key/secret,
   STK passkey. The receiving ledger defaults to the M-Pesa Mode of Payment's account
   (`kenyan_accountant` seeds one).
3. For automatic C2B posting: an API initiator from the M-Pesa org portal, and its
   security credential (generated on the Daraja portal). Without these, C2B payments are
   recorded but a person posts each one.
4. **Test Connection**, then **Register Paybill/Till URLs**. In production Safaricom allows
   this once per shortcode.
5. The site's scheduler must be on (it finishes payments whose callback got lost).

Cloudflare in front of the site must not challenge `/api/method/royce_payments.cb.*`.

## Tests

```
python -m unittest royce_payments.daraja.test_protocol     # no Frappe needed
bench --site <test-site> run-tests --app royce_payments     # full suite
```
