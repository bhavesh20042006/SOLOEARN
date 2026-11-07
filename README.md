# SOLOEARN v0.1 — Private Local Workbench

A local-first, single-owner prototype for organizing legitimate paid-work opportunities and producing a first repeatable deliverable: a CSV business-analysis pack.

**Important:** this is not AGI and it does not guarantee income. It is not an unattended earning bot. Version 0.1 does not scrape job sites, send proposals, accept contracts, submit deliverables, make purchases, move money, log in to a bank, or access personal files. Human approval is required for external actions.

## What works in v0.1

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
  - Python handles calculations, transformations, and Excel compilation; local Qwen/Ollama interprets nuance.
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
  - Tailored Proposal Generator (`core/proposal_generator.py`): produces honest, professional proposals referencing the verified deliverable bundle (`business_analysis.xlsx`, `cleaned_data.csv`, `report.html`, charts) and 6-suite QA guarantee with zero hallucinated promises.
  - Explicit Human Authorization Gate: proposals start as `draft` and strictly require owner review and checkbox authorization before submission.
- Optional local Ollama connection for proposal drafting and aggregate-only report observations.
- CSV workbench that creates:
  - `business_analysis.xlsx` (cleaned data, data-quality profile, numeric summary)
  - `cleaned_data.csv`
  - `profile.json`
  - `insights.txt`
  - `report.html`
  - simple charts in `charts/`
  - `deliverables.zip`
- A revenue ledger. Only manually mark revenue as `paid` after verifying receipt.
- Task workspace path checks; the app binds only to localhost.

## Hardware recommendation

Your stated hardware is Ryzen 5, 16 GB RAM and an AMD GPU. Start CPU-first. GPU acceleration depends on the exact GPU SKU, VRAM/shared-memory amount, Windows driver and Ollama backend support. Current Ollama Windows ROCm support is limited to a published list of AMD Radeon RX 7000 cards and selected Radeon PRO cards; Vulkan can provide additional support, but actual compatibility varies. Don't assume that an integrated Radeon GPU will be used.

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

7. Start the private dashboard:

   ```powershell
   .\scripts\run_windows.ps1
   ```

8. Open <http://127.0.0.1:8501> in your browser. Keep the terminal window open while using it.

Run the automated checks after setup:

```powershell
.\scripts\test_windows.ps1
```

If Ollama is not installed or the model is not pulled, the workbench still runs. Proposal drafting uses a clearly labeled template fallback and reports use deterministic aggregate observations.

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
   - Review the proposal text referencing verified deliverable artifacts (`business_analysis.xlsx`, `report.html`, charts) and 6-suite QA guarantee.
   - Check the **I have verified this proposal meets platform standards** checkbox and click **Authorize Proposal for Submission**.
   - Only authorized proposals advance to the ready queue; zero automatic or unsolicited sending.

## Use Antigravity / Claude Code

Open this folder in Antigravity. The suggested next development prompt is in `ANTIGRAVITY_NEXT_PROMPT.md`.

Claude Pro can be used interactively through Claude Code for development if your plan includes it. Anthropic currently includes Claude Code access with Pro, but that is a developer tool with subscription limits, not a general API credential to embed into a background service. The runtime in this prototype talks only to Ollama at localhost. Antigravity is the development environment, not the 24/7 earnings runtime.

## Security boundaries

- Do not paste passwords, API keys, cookies, bank login details or personal documents into the app.
- Do not enable unrestricted computer-use access.
- Do not execute instructions extracted from job descriptions or uploaded documents.
- Do not send a proposal, make a commitment, accept a contract or submit deliverables without reviewing the scope and approving the action.
- Follow each platform's rules for APIs, automation and outreach.
- Back up the `data/` folder; it contains the local ledger.

## Roadmap after v0.1

1. Add a reviewed intake format for allowed RSS/API feeds and direct inbound leads.
2. Add repeatable service templates (monthly KPI report, spreadsheet cleanup, dashboard refresh).
3. Add isolated per-task execution and regression tests.
4. Add a client/project checklist, revision management and approval queue.
5. Add platform integrations only where the platform explicitly supports them.
6. Consider unattended execution only for narrow, reversible actions with strict limits and a tested rollback path.

Do not add auto-submission or payment automation before a real customer workflow has been validated end-to-end.
