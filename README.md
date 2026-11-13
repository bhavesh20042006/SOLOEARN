# SOLOEARN — Autonomous B2B Operations & FRIDAY Agent Core

A local-first, profit-optimized private operations engine for B2B analytics and business process automation. SOLOEARN couples deterministic service delivery workflows with **FRIDAY**—an autonomous AI agent core governed by a strict, fail-closed permission nervous system.

> **Commercial Reality Notice:** All software engineering benchmarks and architectural capability proofs in this repository have passed automated testing (241 passed, 2 skipped across 243 discovered tests; 25/25 in FRIDAY Agent Core test suite; 7/7 local preflight checks). However, software tests do **not** constitute realized business income. The live commercial ledger remains at `paying_customers: 0, revenue: ₹0.00, customer_advances: ₹0.00`. Realizing revenue requires completing the 9 real-world onboarding steps (Kotak Safalta Current Account, Kotak.biz merchant activation, and live client acquisition).

---

## Architecture: The Tripartite Model

```
+-------------------------------------------------------------------------+
|                           THE FRIDAY BRAIN                              |
|   Goal Reasoning • ReAct Planning • Episodic CRM Memory • Context Tracking |
+-------------------------------------------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|                      THE NERVOUS SYSTEM (CONTROLLER)                    |
|    5 Human Approval Gates • Margin Floor (>=50%) • Turnaround SLA (>=24h) |
|         OWASP File Intake Sanitization • Durable Action Audit Logs       |
+-------------------------------------------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|                           THE SOLOEARN BODY                             |
|  Discovery • Sales Engine • Delivery Workspaces • QA Agent • UPI Ledger |
+-------------------------------------------------------------------------+
```

1. **The Brain (`core/friday_agent.py`)**:
   - `FridayBrain` handles multi-step goal reasoning (`drive_revenue`, `qualify_leads`, `deliver_order`), ReAct loop tool selection, and context progression.
   - `FridayMemory` isolates per-client episodic records, requirements, and communication history in SQLite without cross-tenant information leakage.
2. **The Nervous System (`core/friday_agent.py` & `core/friday_secure_portal.py`)**:
   - `FridayPermissionController` intercepts all tool calls. Enforces 5 Non-Bypassable Human Approval Gates, 50% contribution margin floors, and standard 24h turnaround SLAs.
   - `FridaySecurePortal` implements OWASP-compliant tokenized file ingestion (50MB limits, UUID file isolation, and formula injection sanitization preserving negative numeric values).
   - `FridayPaymentWebhookHandler` enforces HMAC-SHA256 signature verification and replay/idempotency protection before releasing deliverables.
3. **The Body (`core/autonomy_profitability_system.py` & `core/upi_engine.py`)**:
   - Discovery, sales, delivery workspaces, quality verification (up to 3 automated repair attempts), and statutory Mode 01/04 UPI invoicing.

---

## The 5 Human Approval Gates

| Gate | Description | Escalation Trigger |
| :--- | :--- | :--- |
| **Gate 1** | **Unusual Contract / Discounting** | Proposed fee discounted $>20\%$ below standard tier price, or contribution margin $<50\%$. |
| **Gate 2** | **Rush Turnaround Commitment** | Turnaround requested under standard 24h SLA; requires owner capacity verification. |
| **Gate 3** | **Deliverable Release** | Strictly locked until invoice status is verified as `paid` via cleared bank credit or valid webhook. |
| **Gate 4** | **Outbound Outreach Dispatch** | Cold outreach message or email requires owner click-to-dispatch confirmation. |
| **Gate 5** | **Unverified Execution Safeguard** | Blocks scraping protected sites, CAPTCHA bypassing, or sending customer data to public AI APIs. |

---

## Supported Service Workflows

1. **Tier 1: CSV Analytics & Reporting Bundle** (₹5,000 standard base)
   - Multi-tab Excel workbook, statistical summary, interactive charts, and executive HTML report.
2. **Tier 2: Data Engineering & Transformation Pipeline** (₹15,000 standard base)
   - Schema mapping, automated cleaning, typed output datasets, and standalone transformation scripts.
3. **Tier 3: Interactive Web Dashboard Pack** (₹20,000 standard base)
   - Offline-ready responsive HTML dashboard with KPI metric cards, searchable tables, and data visualizers.
4. **Tier 4: Business Process Automation Pack** (₹15,000 standard base)
   - Reusable pipeline scripts, cross-file reconciliation, duplicate removal, and audit reporting.

---

## Verification & Integrity

### Test Suite Execution
```bash
# Run FRIDAY Agent Core 25-test security & integrity regression suite
python -m unittest tests.test_friday_agent_core

# Run complete regression discovery suite (243 tests: 241 passed, 2 skipped, 0 failures)
python -m unittest discover -s tests

# Run Stage A Local Preflight verification (7/7 steps passed)
python scripts/run_m7_local_preflight.py
```

### Archive Checksum (M8.0.3 Production Integrity Release)
- Archive: `SOLOEARN_Friday_Agent_Core_Ready.zip` (336,785 bytes)
- Base64 ASCII: `SOLOEARN_Friday_Agent_Core_Ready.zip.b64.txt` (449,048 chars)
- **SHA256**: `ABAD4DD2EE6CDB6741CBEA53E12DB774D2D17DBC87D56FFCC47F2B8F87EF8E78`

### M8.0.3 Production Integrity Corrections
Following independent static code auditing and regression testing of FRIDAY M8.0.2 on GitHub, 7 key integrity defect areas were resolved and verified across 16 mandatory regression tests (Tests 10–25 in `tests.test_friday_agent_core`):
1. **Approval Security & Single-Use Consumption**: Gated owner escalations now hash canonical arguments with SHA-256 (`args_hash`), enforce a strict 24-hour expiration window, and atomically mark records as consumed (`is_consumed = 1`) upon tool execution, permanently preventing parameter tampering or double consumption.
2. **Customer Acceptance Precondition**: Tool `execute_data_workflow` is bound directly to `friday_accepted_orders`, verifying proposal client ownership, environment scoping, and exact dataset SHA-256 match before any delivery processing begins.
3. **Webhook Security & UTR Deduplication**: Bank webhook processing strictly enforces non-empty bank UTRs (never generating synthetic timestamp references), rejects underpayments, overpayments, NaNs, and infinities, validates successful status (`captured`/`succeeded`), scopes queries strictly to `is_sandbox`, and blocks duplicate UTR reuse across events via database-level `friday_settled_utrs` unique constraints under `BEGIN IMMEDIATE` transactions.
4. **Concurrent Token & Ingestion Protection**: Serialized atomic token claims in SQLite (`BEGIN IMMEDIATE`) prevent concurrent race conditions on single-use upload tokens (`friday_portal_tokens`) and deliverable download tokens (`friday_deliverable_tokens`).
5. **Delivery Containment & Single-Use Downloads**: Deliverable releases verify payment match against the exact task, check workspace directory containment against path traversal, and provide `download_client_deliverable()` with atomic single-use download validation and audit event logging.
6. **Truthful Outreach Reporting**: Outreach dispatch never synthesizes fake message IDs; when no external delivery provider credentials exist in the environment, pitches are truthfully recorded as drafts (`drafted_no_provider_configured`).
7. **Sequence Precondition Guards & Immutability**: All tools enforce workflow sequence preconditions, failing closed if called out-of-sequence by an LLM without crashing or corrupting state. Database append-only triggers prevent any update or deletion of production audit records.


## What works in v0.6.1.2

- Local Streamlit dashboard bound to `127.0.0.1` only.
- SQLite database for opportunities, tasks, revenue entries and an audit log.
- Risk-adjusted opportunity scoring (an estimate, not a payment guarantee).
- **Structured Opportunity CSV Import (Milestone 2 intake)**:
  - Validates all columns: `title, source, url, description, amount_inr, estimated_hours, skill_match_pct, payment_confidence_pct, risk_level, source_permitted`.
  - Rejects unsafe URLs (strictly `http://` and `https://` only; blocks `javascript:`, `file:`, `ftp:`, `data:`, etc.).
  - Validates numeric boundaries (positive hours, non-negative amounts, 0-100% ranges, 0-10 risk scale).
  - Duplicate detection: identifies duplicates within the uploaded file and against existing database opportunities.
  - Interactive validation preview with error breakdowns and explicit human approval checkbox before importing.
  - Pre-packaged sample import file: `sample_opportunities.csv`.
- **Autonomous Execution Engine (Milestone 3 & 3.2 Hardened)**:
  - Decomposes approved opportunity requirements into structured 7-step work plans.
  - Fail-closed execution gate: reloads authoritative opportunity from SQLite, rejects unknown/unapproved/unpermitted jobs, and propagates state-transition errors.
  - Generates full deliverables in task-scoped isolated workspaces (`workspaces/task_XXXX/`).
  - Python handles calculations, transformations, and Excel compilation. Optional local Qwen/Ollama is used only for aggregate report observations; execution planning and proposals are deterministic.
- **Independent Quality Assurance (QA) Agent (Milestone 3 & 3.2 Hardened)**:
  - 6-suite verification: file existence, openpyxl workbook structure, comprehensive all-sheet formula injection protection, mathematical identity checks, PNG binary validation, HTML presentation integrity, archive allow-list health.
  - Verifies workbook/CSV dimensions, checks profile column completeness, and requires complete numeric summary rows with independently recalculated means.
  - Generates detailed JSON scorecard with percentage grade and diagnostics.
- **Self-Healing Revision Engine (Milestone 3 & 3.2 Hardened)**:
  - Diagnoses failing QA checks and executes automated repairs (re-compiling workbooks, re-rendering visuals, re-aligning summaries).
  - Escapes untrusted text and chart names when synthesizing presentation reports to prevent HTML injection.
  - Strictly bounded retry loop (hard cap of max 3 attempts) preventing endless cycles.
  - Enforces workspace isolation with normalized path traversal and symlink protections.
  - Promotes work to `submission_ready` only upon 100% QA pass.
- **Autonomous Opportunity Discovery & Screening (Milestone 4)**:
  - Inbound lead intake adapter: parses standard RSS 2.0 feeds, Atom XML feeds, JSON APIs, and local file dropzones (`data/leads_dropzone/`).
  - Zero web scraping or platform-rule evasion: ingests only permitted sources and user-provided feeds without automated account creation or terms violations.
  - Strict SSRF protection: blocks loopback addresses (`127.0.0.1`, `localhost`), link-local IPs, private network ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), and non-HTTP schemes.
  - Capability Matcher (`core/capability_matcher.py`): scores scope against SOLOEARN's verified deliverable capabilities (CSV/Excel analytics, data cleaning, charts, HTML summaries) and penalizes/rejects unsupported domains (mobile apps, crypto, video editing, copywriting).
  - Risk & Fraud Analyzer (`core/risk_analyzer.py`): flags off-platform communication schemes (Telegram, WhatsApp, Skype), payment scams (crypto, gift cards, check deposits), rate anomalies, and suspicious TLDs.
  - Multi-level deduplication: canonicalizes URLs and hashes title/description via SHA-256 fingerprinting.
  - Tailored Proposal Generator (`core/proposal_generator.py`): produces honest, professional proposals referencing the verified deliverable bundle (`business_analysis.xlsx`, `cleaned_data.csv`, `report.html`, charts) and six automated QA categories; automated checks do not guarantee client acceptance or error-free work.
  - Explicit Human Authorization Gate: proposals start as `draft` and strictly require owner review and checkbox authorization before submission.
- **Revenue Operations & Multi-Service Execution (Milestone 5)**:
  - **4 currently implemented service workflows** (the catalog prices below are illustrative starting assumptions, not verified market rates or guaranteed sales):
    1. *Data Analytics & Excel Pack* (assumed ₹5,000) — multi-tab Excel workbook, statistical profile, charts, and executive HTML report.
    2. *Data Engineering & CSV Pipeline Pack* (assumed ₹15,000) — cleaned dataset, schema manifest, data dictionary, reusable Python pipeline, and test file.
    3. *Interactive HTML Dashboard Pack* (assumed ₹20,000) — offline-ready HTML dashboard with KPI cards, escaped table data, and client-side filtering.
    4. *CSV Reconciliation Automation Pack* (assumed ₹15,000) — a real deterministic CSV workflow that normalizes headers, removes exact duplicate rows, writes a reconciled CSV, and emits a JSON summary. It is not a general-purpose business process automation platform.
  - **SSRF & Security Hardening**:
    - Comprehensive DNS resolution checks resolving all IPv4/IPv6 addresses to reject private, loopback, link-local, and reserved subnets.
    - Custom redirect handler `SafeSSRFRedirectHandler` blocking 301/302 redirects to unsafe or internal destinations.
    - Deterministic XML/XXE defense disallowing `<!DOCTYPE` and `<!ENTITY` declarations in RSS/Atom feeds.
  - **Concurrency & Atomic Database Locking**:
    - Atomic SQL claims (`claim_opportunity_for_execution`) preventing race conditions during concurrent execution requests.
    - SQLite WAL mode with 5,000ms busy timeout.
  - **Client Invoicing & Payment Verification Workflow**:
    - Generates formal print-ready HTML invoices (`INV-YYYY-XXXX.html`) with line items and a user-entered tax calculation; confirm applicable invoice requirements before issuing.
    - Owner-attested payment receipt recording requiring explicit confirmation, a bank reference (UTR/Transaction ID), and exact amount matching; this is not an automatic bank or UTR verification integration.
  - **Background Discovery Scheduler (optional separate process)**:
    - Recurring polling of active, permitted discovery sources when `START_SOLOEARN_SCHEDULER.bat` is running.
    - Interrupted jobs are reset to a safe state and flagged for manual review; they are not automatically resumed.
    - Health heartbeat file (`data/daemon_heartbeat.json`).
    - This scheduler does not autonomously execute client jobs, accept contracts, send proposals, submit work, or process payments.
  - **Net Profit Target Optimizer (planning target: ₹1,00,000 / month after business costs and estimated income tax)**:
    - Separates quoted pipeline, contracted backlog, invoices, cleared owner-confirmed receipts, operating expenses, estimated tax, and available owner cash.
    - Uses a versioned tax estimate for a resident individual sole proprietor under the default new regime for tax year 2026–27; other regimes, non-resident status, other entity types and special-rate income are rejected for estimate purposes pending CA review.
    - Calculates planning receipts needed from observed operating costs and tax-reviewed deductible costs. GST collected is excluded from income; GST aggregate turnover includes non-cancelled SOLOEARN invoices for the tax year plus owner-entered other same-PAN turnover.
    - Displays estimated after-tax profit and incremental-tax-adjusted project counts. Catalog prices, delivery hours and direct costs remain assumptions until measured from real jobs.
    - This is a planning model, not a validated demand forecast, accounting opinion, tax filing, bank integration, take-home guarantee, or promise that the system will reach the target.
- Optional local Ollama connection for aggregate-only report observations. Proposal drafts are deterministic templates; the local model is not used to create external proposal claims.
- CSV workbench that creates:
  - `business_analysis.xlsx` (cleaned data, data-quality profile, numeric summary)
  - `cleaned_data.csv`
  - `profile.json`
  - `insights.txt`
  - `report.html`
  - simple charts in `charts/`
  - `deliverables.zip`
- A revenue ledger. Payment receipt is owner-attested after an independent bank-statement check; the app does not connect to bank accounts or independently verify UTRs.
- Task workspace path checks; the app binds only to localhost.

## Hardware recommendation

Your stated hardware is AMD Ryzen 7 5700U, 16 GB RAM and integrated Radeon Vega graphics. Start CPU-first. GPU acceleration depends on the exact GPU SKU, VRAM/shared-memory amount, Windows driver and Ollama backend support. Current Ollama Windows ROCm support is limited to a published list of AMD Radeon RX 7000 cards and selected Radeon PRO cards; Vulkan can provide additional support, but actual compatibility varies. Don't assume that an integrated Radeon GPU will be used.

Recommended first model: `qwen3:4b-instruct` (about 2.5 GB in the Ollama library). Keep context small (the app sets 4096 tokens) and close memory-heavy applications during inference. If 4B is too slow, use `qwen3:1.7b`; if it runs comfortably and the laptop has enough headroom, test `qwen3:8b` later. Model size on disk is not the same as total runtime RAM usage.

## Windows setup

1. Install Python 3.11+ if it is not already installed.
2. Optional, install Ollama from the official page: <https://ollama.com/download>.
3. Open PowerShell in this project folder.
4. If PowerShell blocks local scripts, use a one-time process-only execution policy:

   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   ```

5. Install Python packages:

   ```powershell
   .\scripts\setup_windows.ps1
   ```

6. Optional: download the local model (roughly 2.5 GB):

   ```powershell
   ollama pull qwen3:4b-instruct
   ```

   Check it is available:

   ```powershell
   ollama list
   ```

7. Start the private dashboard with `START_SOLOEARN.bat` (this also launches the optional feed-polling scheduler in a separate minimized console), or run `scripts\run_windows.ps1` to start only the dashboard.

8. Open <http://127.0.0.1:8501> in your browser. Keep the dashboard terminal open while using it.
9. If you started only the dashboard with `run_windows.ps1`, you can start recurring authorized-feed polling separately with `START_SOLOEARN_SCHEDULER.bat`. Stop the scheduler console with Ctrl+C or close it. It polls feeds only; it does not accept jobs, execute paid work, or send proposals.

Run the automated checks after setup:

```powershell
.\scripts\test_windows.ps1
```

If Ollama is not installed or the model is not pulled, the workbench still runs. Proposal drafting is always a deterministic, owner-editable template; reports use deterministic aggregate observations.

## Diagnose your exact AMD GPU

Run:

```powershell
.\scripts\diagnose_windows.ps1
```

Share the exact GPU name and driver version to determine whether ROCm or Vulkan acceleration is realistic. Don't install random ROCm builds or set undocumented GPU overrides on Windows.

## Test the CSV workbench

Use `sample_sales.csv` included in this folder. In the app:

1. Open **CSV Workbench**.
2. Upload `sample_sales.csv`.
3. Click **Run local analysis**.
4. Download the generated ZIP and inspect every sheet/chart before using it for a client.

The generated sample report is only a product test; it is not a paid job or evidence of revenue.

## Test Structured Opportunity Import

Use `sample_opportunities.csv` included in this folder:

1. Open **Opportunities** in the dashboard.
2. Select the **Import from CSV** tab.
3. Upload `sample_opportunities.csv` (or click *Download sample opportunities CSV template*).
4. Review the real-time validation preview:
   - Total rows, valid rows, duplicates, and any malformed rows.
   - Preview of calculated risk-adjusted expected values (₹/hour) and score labels.
5. Check the confirmation checkbox to verify sources are authorized and permitted.
6. Click **Confirm & import into pipeline**.
7. The opportunities are committed to the local database and appear ranked in your pipeline table.

## Test Autonomous Execution & Quality Assurance (Milestone 3)

1. Open **Opportunities** in the dashboard.
2. Select any opportunity and click **Approve for work** (e.g. an approved ₹5,000 retail sales analysis).
3. Scroll down to the **Autonomous Execution & QA Verification** panel.
4. Select the dataset: "Use bundled sample_sales.csv" or upload a custom CSV.
5. Click **🚀 Execute Work Plan & Run QA**:
   - The Autonomous Execution Engine interprets requirements and generates a structured 7-step plan.
   - It performs all calculations, cleaning, chart generation, and compiles `business_analysis.xlsx`.
   - The Independent QA Agent runs 6 verification suites auditing formulas, identities, headers, and files.
   - If any defects occur, the Revision Engine automatically repairs and re-tests them.
   - Upon 100% pass, the opportunity and task advance to **`submission_ready`**.
6. Expand **View Execution Plan & QA Report Details** to inspect check results.
7. Click **📦 Download Verified Deliverables ZIP** to inspect the finished client package.
8. Check the human review authorization checkbox and click **Authorize & Mark Submitted by Owner**.

## Test Autonomous Discovery & Proposals (Milestone 4)

1. Open **Opportunities** in the dashboard.
2. Select the **Autonomous Discovery (M4)** tab.
3. Manage Discovery Sources:
   - Pre-configured with local dropzone, sample RSS 2.0 (`data/sample_feeds/sample_rss.xml`), and sample JSON API (`data/sample_feeds/sample_leads.json`).
   - Add custom RSS/Atom feeds or JSON endpoints with verification and authorization flags.
4. Click **📡 Poll Authorized Discovery Sources**:
   - Ingests feeds with strict SSRF protection (blocking loopbacks and private networks).
   - Scans `data/leads_dropzone/` for inbound lead files.
   - Capability Matcher evaluates data analytics skill match and penalizes unsupported domains.
   - Risk & Fraud Analyzer flags Telegram/WhatsApp redirects, crypto/wire payment scams, and rate anomalies.
   - Multi-level deduplication drops repeated URLs and identical content hashes.
5. Review the Inbound Discovery Pipeline:
   - Inspect hourly yield ranking (₹/hour), capability match %, and risk score.
   - Review flagged scam warnings and reasons before acting.
6. Generate and Authorize Tailored Proposals:
   - Select a high-scoring discovered lead and click **Generate Tailored Proposal**.
   - Review the proposal text referencing verified deliverable artifacts (`business_analysis.xlsx`, `report.html`, charts) and six automated QA categories, subject to client-specific review.
   - Check the **I have verified this proposal meets platform standards** checkbox and click **Authorize Proposal for Submission**.
   - Only authorized proposals advance to the ready queue; zero automatic or unsolicited sending.

## Use Antigravity / Claude Code

Open this folder in Antigravity. The suggested next development prompt is in `ANTIGRAVITY_NEXT_PROMPT.md`.

Claude Pro can be used interactively through Claude Code for development if your plan includes it. Anthropic currently includes Claude Code access with Pro, but that is a developer tool with subscription limits, not a general API credential to embed into a background service. The runtime in this prototype talks only to Ollama at localhost. Antigravity is the development environment, not the 24/7 earnings runtime.

## M6.1.2 financial reconciliation safeguards

The v0.6.1.2 release includes 151 automated tests. See `SOLOEARN_Milestone6.1.2_test_results_final.txt` for the captured run. The Linux audit environment does not include Streamlit, so the live dashboard must still be smoke-tested on Windows.


- A bank credit cannot mark an invoice paid unless the exact gross statement amount equals the full invoice total. A debit cannot mark an expense paid unless it exactly matches the full recorded expense amount. Partial settlements are rejected and left unreconciled.
- A statement row is one-use; invalid entity matches and repeated reconciliation are rejected before ledger mutation.
- Other bank inflows/outflows can be reconciled to cash-only ledger events and do not become business income.
- Invoice refunds split the gross refund proportionally into ex-GST service refund and GST-refund entries. Service refunds reduce realized receipts/profit; GST refunds reduce the GST reserve.
- Unsupported tax profiles fail closed instead of silently using the new-regime calculation.
- Negative operating results remain negative; they are not clipped to zero. Taxable-loss set-off/carry-forward is not inferred by this estimator.

## Security boundaries

- Do not paste passwords, API keys, cookies, bank login details or personal documents into the app.
- Do not enable unrestricted computer-use access.
- Do not execute instructions extracted from job descriptions or uploaded documents.
- Do not send a proposal, make a commitment, accept a contract or submit deliverables without reviewing the scope and approving the action.
- Follow each platform's rules for APIs, automation and outreach.
- Back up the `data/` folder; it contains the local ledger.

## Verified scope and current limitations (v0.6.1.2)

- Four deterministic service workflows are implemented: analytics, data engineering, interactive HTML dashboard generation, and CSV reconciliation automation. There is no separate fifth software-automation executor in this release.
- The business automation service is intentionally limited to CSV header normalization, exact duplicate removal, and a summary report. It does not automate arbitrary external business systems.
- The background scheduler polls permitted discovery sources and writes a heartbeat. It does not automatically execute paid jobs, contact leads, or restart interrupted work. Interrupted work is flagged for owner review.
- Proposal authorization is a local approval state; there is no automatic proposal submission integration in this release.
- Payment status is owner-attested from a bank statement. There is no bank API, account connection, automatic UTR validation, or money movement.
- Invoice output is a local HTML draft. Confirm agreed scope, client identity, applicable tax registration, and legal invoice requirements before sending it; this app is not tax or legal advice.
- Direct manual paid-revenue entries are disabled; a paid receipt is recorded only through owner-confirmed invoice payment workflow. This remains an owner attestation, not an independent bank verification.
- The ₹1,00,000 goal is an **after-business-expense and estimated-income-tax planning target**. It is not guaranteed or yet achieved; prices and delivery times remain unvalidated assumptions.
- M6.1.2 bank-statement reconciliation rejects partial invoice and expense payments rather than marking the full item paid. Partial payment allocation is not implemented yet.
- Refunds must be recorded as ex-GST service refund plus GST-refund components; the invoice refund helper records an owner-confirmed ledger adjustment but does not initiate or independently verify a bank transfer.
- The tax estimate currently supports only a resident individual sole proprietor, default new regime, tax year 2026–27 and ordinary-rate taxable income. Other regimes, non-resident status, entities, and special-rate income are not modelled. A Chartered Accountant must review the tax profile and treatment.
- GST threshold monitoring includes non-cancelled SOLOEARN invoice subtotals issued in the tax year plus owner-entered other aggregate turnover under the same PAN. It remains an estimate; the owner/CA must confirm all taxable, exempt, export and other turnover, applicable state threshold and exceptions.
- Bank reconciliation and payment status rely on owner attestation after checking a bank statement. There is no bank API, independent UTR validation, automatic payment collection or money movement.
- This project has not demonstrated a repeatable paying-customer acquisition channel, genuine paid customer, or sustained monthly profit. The first commercial validation milestone is to complete and collect payment for one genuine client project.

Future work should prioritize evidence-backed client acquisition, independently validated service outputs, secure supported integrations, and operational monitoring. Do not enable automatic contract acceptance, proposal sending, or financial actions without platform authorization and tested safeguards.
