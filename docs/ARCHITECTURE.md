# SOLOEARN — Technical Architecture

> Last updated: November 2026 · Milestone M8.0.3 (Architecture Frozen)

---

## System Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                           EXTERNAL BOUNDARY                                  │
│  ┌────────────┐  ┌────────────┐  ┌──────────────┐  ┌────────────────────┐   │
│  │ RSS/Atom   │  │ JSON API   │  │ Local File   │  │ Client Upload      │   │
│  │ Feeds      │  │ Endpoints  │  │ Dropzone     │  │ (Tokenized Portal) │   │
│  └─────┬──────┘  └─────┬──────┘  └──────┬───────┘  └─────────┬──────────┘   │
│        │               │                │                     │              │
│        └───────┬───────┴────────┬───────┘                     │              │
│                ▼                ▼                              ▼              │
│    ┌────────────────────────────────────────────────────────────────────┐     │
│    │                    SSRF PROTECTION LAYER                          │     │
│    │    IP validation · Scheme allowlist · Redirect safety · DNS check │     │
│    └───────────────────────────┬────────────────────────────────────────┘     │
│                                ▼                                             │
│ ┌──────────────────────────────────────────────────────────────────────────┐  │
│ │                          THE FRIDAY BRAIN                                │  │
│ │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────┐  │  │
│ │  │ Goal Engine  │  │ LLM Reasoner │  │ State Machine│  │ Episodic   │  │  │
│ │  │ (ReAct Loop) │  │ (Ollama/     │  │ (Fallback    │  │ Memory     │  │  │
│ │  │              │  │  qwen3:4b)   │  │  Planner)    │  │ (CRM)      │  │  │
│ │  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └────────────┘  │  │
│ │         │                 │                  │                          │  │
│ │         └─────────┬───────┴──────────────────┘                          │  │
│ │                   ▼                                                     │  │
│ │  ┌─────────────────────────────────────────────────────────────────┐    │  │
│ │  │                  TOOL REGISTRY (8 registered tools)             │    │  │
│ │  │  discover_prospects · prepare_pitch · dispatch_outreach         │    │  │
│ │  │  draft_proposal · register_order · execute_workflow             │    │  │
│ │  │  audit_deliverables · generate_invoice · release_deliverables   │    │  │
│ │  └──────────────────────────┬──────────────────────────────────────┘    │  │
│ └─────────────────────────────┼──────────────────────────────────────────┘   │
│                               ▼                                              │
│ ┌──────────────────────────────────────────────────────────────────────────┐  │
│ │                    THE NERVOUS SYSTEM (CONTROLLER)                       │  │
│ │                                                                         │  │
│ │  Permission Tiers:                                                      │  │
│ │  ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────────┐    │  │
│ │  │ TIER_AUTONOMOUS  │ │ TIER_GUARDED_BIZ │ │ TIER_OWNER_APPROVAL  │    │  │
│ │  │ Safe local ops   │ │ Margin-checked   │ │ High-risk financial  │    │  │
│ │  └──────────────────┘ └──────────────────┘ └──────────────────────┘    │  │
│ │                                                                         │  │
│ │  5 Approval Gates:                                                      │  │
│ │  G1: Unusual Discount (>20%)     G4: Outbound Outreach Dispatch        │  │
│ │  G2: Rush SLA (<24h)             G5: Refund Authorization              │  │
│ │  G3: Deliverable Release (payment-gated)                                │  │
│ │                                                                         │  │
│ │  Invariants: 50% margin floor · HMAC-SHA256 escalation hashing         │  │
│ │  Single-use consumed tokens · 24h expiration · Append-only audit logs  │  │
│ └──────────────────────────────┬──────────────────────────────────────────┘  │
│                                ▼                                             │
│ ┌──────────────────────────────────────────────────────────────────────────┐  │
│ │                          THE SOLOEARN BODY                               │  │
│ │                                                                         │  │
│ │  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌──────────────────┐    │  │
│ │  │ Discovery │  │  Sales    │  │ Delivery  │  │ Quality Agent    │    │  │
│ │  │ Engine    │  │  Engine   │  │ Engine    │  │ (Independent QA) │    │  │
│ │  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘  └────────┬─────────┘    │  │
│ │        │              │              │                  │              │  │
│ │        ▼              ▼              ▼                  ▼              │  │
│ │  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌──────────────────┐    │  │
│ │  │ Risk      │  │ Proposal  │  │ Revision  │  │ UPI Payment      │    │  │
│ │  │ Analyzer  │  │ Generator │  │ Engine    │  │ Engine (M01/04)  │    │  │
│ │  └───────────┘  └───────────┘  └───────────┘  └──────────────────┘    │  │
│ │                                                                         │  │
│ │  ┌──────────────────────────────────────────────────────────────────┐   │  │
│ │  │                    FINANCIAL STACK                                │   │  │
│ │  │  Append-only Ledger (integer paise) · Tax Engine (IT Act 2025)  │   │  │
│ │  │  Net Profit Optimizer · Invoice Generator · GST Monitoring      │   │  │
│ │  └──────────────────────────────────────────────────────────────────┘   │  │
│ └──────────────────────────────┬──────────────────────────────────────────┘  │
│                                ▼                                             │
│ ┌──────────────────────────────────────────────────────────────────────────┐  │
│ │                          PERSISTENCE LAYER                               │  │
│ │  SQLite (WAL mode) · Foreign keys ON · Busy timeout 5s                  │  │
│ │  Tables: opportunities · tasks · revenue · invoices · proposals          │  │
│ │  friday_memories · friday_action_logs (append-only, trigger-protected)  │  │
│ │  friday_escalations · friday_accepted_orders · financial_ledger_txns    │  │
│ │  discovery_sources · commercial_leads · bank_reconciliation_log         │  │
│ │  Isolated workspaces: workspaces/task_XXXX/                             │  │
│ └──────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Data Flow: Lead → Revenue

```
Discovery  ──→  Scoring  ──→  Pitch  ──→  Proposal  ──→  Order
   │               │            │           │              │
   │ RSS/Atom/     │ Capability │ LLM or    │ Margin ≥50%  │ Dataset SHA-256
   │ JSON/Local    │ + Risk     │ Template  │ SLA ≥24h     │ verified
   │               │ + Fraud    │           │              │
   ▼               ▼            ▼           ▼              ▼
Accept  ──→  Execute  ──→  QA Audit  ──→  Invoice  ──→  Payment
   │            │              │             │              │
   │ Gate 4     │ Isolated     │ 6-suite     │ UPI QR       │ Bank credit
   │ approval   │ workspace    │ independent │ Mode 01/04   │ verified
   │            │              │ ≤3 retries  │              │
   ▼            ▼              ▼             ▼              ▼
                                                        Release
                                                           │
                                                           │ Gate 3
                                                           │ payment-gated
                                                           ▼
                                                     Deliverable ZIP
```

---

## Security Architecture

### Layer 1: Network Boundary
- **SSRF Protection**: All outbound HTTP resolves DNS and validates IP against private/loopback/link-local/reserved ranges
- **Safe Redirect Handler**: Blocks 3xx redirects to internal destinations
- **XML/XXE Defense**: Rejects `<!DOCTYPE>` and `<!ENTITY>` in RSS/Atom feeds
- **Ollama Binding**: LLM client enforces `http://127.0.0.1` or `http://localhost` only

### Layer 2: Input Validation
- **File Upload**: Extension allowlist (.csv, .xlsx, .tsv, .txt), 25MB limit, UUID filename isolation
- **Formula Injection (CWE-1236)**: Detects `=`, `+`, `-`, `@` prefixes while preserving negative numeric literals
- **Path Traversal**: `safe_workspace_path()` blocks `..`, symlinks, absolute paths with full chain validation
- **Credential Rejection**: `SecurityViolationError` on UPI PIN, MPIN, OTP, CVV, password in any input

### Layer 3: Authorization
- **3 Permission Tiers**: Autonomous → Guarded Business → Owner Approval
- **5 Human Gates**: Discount, Rush SLA, Deliverable Release, Outreach, Refund
- **HMAC-SHA256 Escalation Hashing**: Canonical argument hashing prevents parameter tampering
- **Single-Use Tokens**: Consumed atomically via `BEGIN IMMEDIATE` with 24h expiry
- **Append-Only Audit**: Database triggers prevent UPDATE/DELETE on production `friday_action_logs`

### Layer 4: Webhook Security
- **HMAC-SHA256 Signature Verification**: On all merchant payment webhooks
- **Replay Protection**: 300-second timestamp skew window
- **Idempotency**: Duplicate event IDs rejected before ledger mutation
- **UTR Deduplication**: Database-level UNIQUE constraint on `(utr, is_sandbox)`

### Layer 5: Data Isolation
- **Per-Task Workspaces**: Each job gets `workspaces/task_XXXX/` with isolated storage
- **No Cloud Transmission**: Customer data never leaves the local machine
- **Dashboard Binding**: Streamlit bound to `127.0.0.1` only
- **Approved Deliverable Allowlist**: Only explicitly listed files are packaged in ZIP

---

## Module Dependency Map

```
config.py ←── db.py ←── financial_ledger.py ←── tax_engine.py
    │              │              │
    │              ├── scoring.py │
    │              │              │
    ▼              ▼              ▼
analytics.py   risk_analyzer.py   net_profit_optimizer.py
    │              │
    ▼              ▼
qa_agent.py   discovery_engine.py ←── capability_matcher.py
    │              │
    ▼              ▼
revision_engine.py  opportunity_import.py
    │
    ▼
service_executors.py ──→ (analytics, qa_agent, revision_engine)
    │
    ▼
upi_engine.py ←── invoice_generator.py
    │
    ▼
friday_secure_portal.py
    │
    ▼
autonomy_profitability_system.py ──→ (all engines unified)
    │
    ▼
friday_agent.py ──→ (FridayBrain, FridayPermissionController,
                      FridayToolRegistry, FridayMemory)
    │
    ▼
app.py (Streamlit dashboard entry point)
```
