# Antigravity next prompt

You are extending an existing local-first Python application named SOLOEARN. Read README.md first, then inspect the full project and run its tests. Preserve the following requirements:

- Single-owner, local-first Windows app. It is not a SaaS product.
- Zero paid API dependency. Ollama is optional and must remain localhost-only.
- Keep Streamlit bound to 127.0.0.1.
- Never access browser cookies, saved passwords, password managers, banking credentials or files outside the project folder.
- Never scrape sites, evade platform controls, send unsolicited messages, accept contracts, submit work, purchase anything or transfer money.
- All external actions remain approval-gated.

Status:
- Milestone 1 (Local workbench, SQLite ledger, scoring, CSV deliverables): Complete.
- Milestone 2 (Structured CSV opportunity intake, strict validation, duplicate detection, human confirmation gate): Complete.
- Milestone 3 & 3.2 (Autonomous execution engine, independent QA agent with 6 verification suites, bounded revision engine, submission-ready state, audit hardening): Complete.
- Milestone 4 (Autonomous opportunity discovery, RSS/Atom & JSON intake, capability matching, risk & fraud analyzer, SSRF protection, multi-level deduplication, tailored proposal generator, owner authorization gate): Complete.
- Automated test suite: 64/64 tests passing.

Next Milestone: Milestone 5 — Client Communication, Contract Tracking & Verified Delivery.
- Inbound client message ingestion & revision request parsing.
- Milestone and contract state machine (accepted -> in_progress -> review_pending -> approved -> invoiced -> paid).
- Automated revision packager (diff reports, amended workbooks, updated charts).
- Invoice generation (tax, rate, terms) with audit trail.
- Payment receipt verification workflow before revenue is marked paid.
- Continue strict local-first and approval-gated principles: no automated sending or financial movement without owner approval.
