# SOLOEARN v0.6.1.2 — Independent audit report

## Audit basis

The supplied M6.1 ZIP was extracted into a fresh directory and its original suite was rerun. The original archive passed 139 tests. Independent source inspection then identified a reproducible high-severity accounting issue: a partial bank credit could mark a full invoice as paid and post the full invoice amount. A separate check showed that the tax engine accepted a `regime` argument but ignored it, returning new-regime figures for an old-regime request.

## Corrections made in v0.6.1.2

- Invoice reconciliation now validates invoice existence, eligible state, credit direction, exact full invoice amount and duplicate bank reference before changing the statement row.
- Expense reconciliation now validates expense existence, debit direction and exact full expense amount before mutation.
- Already-reconciled rows cannot be reused. Invalid matches remain unreconciled.
- Owner draws, tax payments, GST payments and other bank movements post explicit cash-ledger entries; other cash movements do not become business income.
- Refunds split into ex-GST service refund and GST-refund components with cumulative invoice cap and duplicate reference protection.
- Unsupported tax regimes fail closed; tax profile validation requires resident individual sole proprietor, new regime and tax year 2026–27.
- Negative operating profit is not clipped to zero.
- GST turnover estimates include non-cancelled invoices issued within the configured tax year and manually entered other aggregate turnover under the same PAN.
- Required job-count estimates apply incremental income tax to service contribution.
- Added a dashboard reconciliation form for imported bank statement rows.

## Reproducible original defects

1. **Partial invoice payment:** a test invoice for ₹1,180 was matched against a ₹500 credit. Original code returned `status='reconciled'`, set the invoice to `paid`, and posted ₹1,000 service receipts plus ₹180 GST. This is corrected: the row is rejected and left unreconciled.
2. **Ignored tax regime:** `compute_income_tax_india_2026(1_500_000, regime='old_1961')` returned the same total tax as `regime='new_2025'`. This is corrected: unsupported regimes now raise `ValueError`.
3. **Refund exclusion:** a ₹50,000 service receipt plus a ₹10,000 ex-GST refund was still reported as ₹50,000 collected receipts and operating profit. This is corrected: refunds reduce realized ex-GST receipts and the GST-refund component reduces GST reserve.
4. **GST turnover:** the optimizer previously counted only invoices with status `paid`, omitting issued but unpaid invoices and turnover from other business activities under the same PAN. This is corrected by using tax-year invoice subtotals plus an owner-entered other-PAN turnover figure.
5. **Loss clipping:** negative operating profit was clipped to zero. This is corrected; tax loss carry-forward/set-off is still not modelled.
6. **Job count tax omission:** required job counts compared a post-tax gap directly with pre-tax service contribution. They now include estimated incremental tax. Service unit economics remain illustrative assumptions, not observed prices/costs.

## Verification

- Python compilation: passed.
- Full unittest suite from a clean release extraction: 151 tests passed, 0 failures, 0 errors, 0 skips.
- ZIP `testzip()` integrity: passed.
- Streamlit live UI and Windows PowerShell launchers were not executable in the Linux audit environment; the owner must run the Windows test and smoke test.

## Financial and tax limitations

- Tax estimates are planning estimates for a resident individual sole proprietor, default new regime, tax year 2026–27 and ordinary-rate taxable income. Non-resident status, other legal entities, other tax regimes, special-rate capital gains/income, loss set-offs and tax-law changes require CA review and are rejected or not modelled.
- GST threshold monitoring remains an estimate. Other aggregate turnover under the same PAN must be entered manually; state-specific thresholds, exemptions, exports and compulsory-registration exceptions require CA review.
- Partial invoice and expense payments are not supported; mismatched amounts are safely rejected.
- Bank reconciliation is owner-attested from imported statements; no bank API or independent UTR verification exists.
- This release cannot guarantee customer demand, receipts, ₹1 lakh monthly post-tax profit or any income level. There is no demonstrated sustained customer acquisition or paid-month evidence.
