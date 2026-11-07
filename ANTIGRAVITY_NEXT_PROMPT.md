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
- Milestone 3 (Autonomous execution engine, independent QA agent with 6 verification suites, bounded revision engine, submission-ready state): Complete.
- Milestone 3.1 & 3.2 (Independent code audit hardening: Windows path normalization, fail-closed execution gate, all-sheet formula scanning, dimensions check, complete numeric summary validation, repaired HTML escaping, client archive allow-list packaging, retry hard cap, task tracking, finite revenue validation, UI state controls): Complete.
- Automated test suite: 49/49 tests passing.

Next Milestone: Milestone 4 — Autonomous Opportunity Discovery & Client Acquisition.
- Build an authorized inbound lead intake adapter (support permitted RSS/JSON feeds and direct inbound inquiry formats).
- Implement automated lead scoring, skill matching, and priority ranking without web scraping or platform-rule evasion.
- Generate tailored, honest client proposals referencing verified deliverable capabilities with zero unsubstantiated claims.
- Add an outreach and communication queue with strict human review and authorization gates prior to sending.
- Maintain isolated workspaces and local-first boundaries.
- Add comprehensive automated tests for feed parsing, lead scoring, proposal drafting, and communication safety guards. Run all tests before declaring completion.
