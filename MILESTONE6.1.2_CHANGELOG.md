# SOLOEARN v0.6.1.2 — Financial reconciliation hardening

## Defects addressed

1. **Invoice bank reconciliation amount mismatch:** previously, any positive credit row could mark the entire invoice paid and record the full invoice amount. It now requires an exact gross match and validates invoice existence/status before mutating the statement row. Partial/over-payments are rejected and left unreconciled.
2. **Expense reconciliation amount mismatch:** bank debits must exactly match the recorded expense amount. Partial expense payments are rejected rather than marking the full expense paid.
3. **Statement-row reuse:** already reconciled rows cannot be reconciled again; invalid entity matches do not consume rows or increment import counts.
4. **Unclassified bank cash movements:** owner draws, income-tax payments, GST payments and other cash-only movements now post to the corresponding ledger type. “Other” cash movements affect bank cash but not business profit.
5. **Refund accounting:** added `record_invoice_refund` to split invoice refunds into ex-GST service refunds and GST-refund components. Ex-GST refunds reduce service receipts and profit; GST refunds reduce the GST reserve. This records an owner-confirmed ledger event; it does not initiate or independently verify a bank transfer.
6. **Tax profile safety:** the tax engine now rejects unsupported regimes and tax profiles rather than silently applying the default new-regime calculation. Current estimate support is limited to resident individual sole proprietors under the default new regime for tax year 2026–27.
7. **Loss visibility:** operating losses remain negative in monthly profit reporting; progress is clamped at zero but profit itself is not.
8. **GST turnover basis:** threshold monitoring includes all non-cancelled SOLOEARN invoices issued during the configured financial year plus owner-entered other aggregate turnover under the same PAN, rather than counting only paid invoices.
9. **After-tax capacity estimates:** required project counts now account for estimated incremental income tax, not just pre-tax service contribution.
10. **Dashboard usability:** added controls to reconcile imported statement rows with invoices, expenses, tax payments, GST payments, owner draws or other cash movements.

## Validation

- Existing M1–M6 regression suite retained.
- Added 12 M6.1.2 tests for partial/overpayment rejection, expense matching, repeated rows, invalid invoice matches, cash-only movement, invoice refund split, tax regime fail-closed behavior, GST same-PAN turnover, negative operating results and post-tax job counts.
- Full local test suite: 151 passed, 0 failures, 0 errors, 0 skips.
- `compileall` passed.
- Release ZIP was re-extracted and the complete suite rerun from that extraction.

## Explicit limitations

- Partial invoice and expense payments are not implemented; they are rejected safely.
- Tax estimate is not a return-filing engine and must be reviewed by a CA.
- GST aggregate turnover still depends on the owner entering all other same-PAN turnover and confirming applicable state rules/exceptions.
- No bank API, UTR verification service, automatic payment collection, or money movement exists.
- Prices, service hours, customer demand and expense ratios remain assumptions until measured through genuine paid projects.
