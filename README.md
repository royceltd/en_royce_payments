# Mobile Payments (`royce_payments`)

Payment integrations for ERPNext v16. Phase 1: M-Pesa through Safaricom's Daraja API.

- **STK Push:** "Request M-Pesa Payment" (under Create) on submitted Sales Invoices and
  Sales Orders sends a PIN prompt to the customer's phone. The payment is confirmed with
  Safaricom and posted as a Payment Entry against the document.
- **Paybill/Till (C2B):** payments customers make to the business's own number are
  recorded, confirmed with Safaricom's Transaction Status API, matched by the account
  number the payer typed (Sales Invoice, Sales Order or Customer ID) and posted. Anything
  unmatched or unverifiable waits in **Daraja C2B Payment** with status *Needs Review*.

- **POS and paid-at-sale invoices:** on the POS screen's payment step, the M-Pesa mode gets
  *Send prompt* (STK Push) and *Find payment* (a Till/Paybill payment the customer already
  made). The confirmed amount goes on the invoice's own M-Pesa payment row, with the receipt
  numbers in its reference, and the invoice records it when submitted: no separate Payment
  Entry. Works whether POS Settings creates Sales Invoices or POS Invoices, and on desk invoices
  with *Include Payment (POS)*. Cancelling or deleting the invoice frees the payment.
- **Received payments on submitted invoices:** *Receive M-Pesa Payment* on a Sales Invoice
  posts a payment that already arrived as a Payment Entry against it.

By default (Daraja Account → *M-Pesa amounts on invoices must match payments received*), an
invoice cannot be submitted with an M-Pesa amount that Safaricom hasn't confirmed. This stops
M-Pesa being keyed in for money that never arrived.

Faster checkout is opt-in (Daraja Account → *POS: use M-Pesa prompts as soon as Safaricom
reports them paid*). A paid prompt then counts at the till on Safaricom's result, which arrives
in seconds, instead of after Safaricom's separate confirmation, which can take a minute or more.
Confirmation still runs afterwards; if it disagrees or never comes, the payment is flagged
(*Confirmation Failed*), the invoice gets a comment and Accounts Managers are notified.
Till/Paybill payments always wait for confirmation.

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
5. For POS: add the M-Pesa mode of payment to each POS Profile, with the same ledger as the
   Daraja Account. Till payments can only be picked once Safaricom confirms them, so set up
   the initiator (step 3) for shops whose customers pay to the Till.
6. The site's scheduler must be on (it finishes payments whose callback got lost).

Cloudflare in front of the site must not challenge `/api/method/royce_payments.cb.*`.

## Tests

```
python -m unittest royce_payments.daraja.test_protocol     # no Frappe needed
bench --site <test-site> run-tests --app royce_payments     # full suite
```
