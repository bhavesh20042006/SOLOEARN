# SOLOEARN — Next development brief (after v0.5.3)

SOLOEARN is a private, local-first Windows application for one owner. Preserve all current tests and safety boundaries. Do not claim guaranteed income or readiness scores without independent evidence.

## Current verified scope
- Milestones 1–4: local workbench, opportunity intake, execution/QA, permitted RSS/Atom/JSON discovery, capability matching, risk screening and proposal drafts.
- Milestone 5.1: four supported deterministic workflows (analytics, data engineering pipeline, HTML dashboard, CSV reconciliation automation), local invoice drafts, owner-attested payment ledger, revenue planning, and an optional background feed-polling scheduler.
- The scheduler only polls permitted lead sources and flags interrupted work for owner review. It does not autonomously execute jobs or submit proposals.
- There is no bank API, automatic payment verification, fifth software-automation executor, guaranteed customer acquisition, or guaranteed monthly income.

## Next priority: Commercial validation
1. Audit the service definitions and produce a client-facing scope, input requirements, exclusions, QA criteria and acceptance checklist for each of the four real executors.
2. Build a lead qualification workflow that requires evidence of client identity, clear scope, authorized source, budget and payment terms.
3. Track funnel metrics based on observed outcomes: leads, replies, qualified leads, proposals, wins, delivered jobs, revisions, paid invoices, gross receipts and hours.
4. Add a quote builder that uses user-approved custom pricing, never invents market rates, GST, deadlines or guarantees.
5. Add duplicate-invoice prevention and payment-reference checks; direct paid-ledger insertion is disabled and paid receipts must flow through owner-confirmed invoice reconciliation; preserve owner attestation and do not connect to bank credentials.
6. Add end-to-end tests using synthetic customer datasets and explicit adversarial cases. Run all regression tests before each release.
7. Keep all outbound communication, contracts, submissions and money movement behind explicit owner approval and platform-supported integrations.

## Constraints
- No paid APIs, cloud hosting or added subscriptions by default.
- Do not scrape sites, evade platform controls, use stored browser cookies, or access personal files outside approved project workspaces.
- Keep Streamlit bound to 127.0.0.1.
- Use deterministic Python for calculations and the local Ollama endpoint only for optional language tasks.
- Measure gross confirmed receipts separately from pending invoices, pipeline, expenses and personal take-home amount.

## Release discipline
- Run unit, integration, security, concurrency and packaging tests on a fresh extraction.
- Record exact Python version, dependency versions, test count, failures and known limitations.
- Do not label any release 1,000/1,000 or claim ₹1 lakh/month earned unless independently supported by production evidence.
