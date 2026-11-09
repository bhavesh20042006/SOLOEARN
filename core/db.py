from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator

import core.config
from core.config import ensure_directories


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Generator[sqlite3.Connection, None, None]:
    ensure_directories()
    conn = sqlite3.connect(core.config.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source TEXT NOT NULL,
                url TEXT DEFAULT '',
                description TEXT DEFAULT '',
                amount_inr REAL NOT NULL DEFAULT 0,
                estimated_hours REAL NOT NULL DEFAULT 1,
                skill_match REAL NOT NULL DEFAULT 0.5,
                payment_confidence REAL NOT NULL DEFAULT 0.5,
                risk_level REAL NOT NULL DEFAULT 5,
                source_permitted INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'new',
                score REAL NOT NULL DEFAULT 0,
                content_hash TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS execution_claims (
                opportunity_id INTEGER PRIMARY KEY,
                claimed_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER,
                title TEXT NOT NULL,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                workspace TEXT NOT NULL,
                artifact_path TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id)
            );

            CREATE TABLE IF NOT EXISTS revenue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER,
                task_id INTEGER,
                description TEXT NOT NULL,
                amount_inr REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'expected',
                paid_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id),
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS discovery_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                url_or_path TEXT NOT NULL,
                is_permitted INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 1,
                poll_interval_minutes INTEGER NOT NULL DEFAULT 60,
                last_polled_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER NOT NULL,
                draft_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                model_used TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id)
            );

            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT UNIQUE NOT NULL,
                opportunity_id INTEGER NOT NULL,
                task_id INTEGER,
                client_name TEXT NOT NULL,
                service_tier TEXT NOT NULL DEFAULT 'data_analytics',
                amount_subtotal REAL NOT NULL,
                tax_rate REAL NOT NULL DEFAULT 0.0,
                tax_amount REAL NOT NULL DEFAULT 0.0,
                amount_total REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'INR',
                status TEXT NOT NULL DEFAULT 'issued',
                payment_reference TEXT DEFAULT '',
                payment_verified_by_owner INTEGER NOT NULL DEFAULT 0,
                notes TEXT DEFAULT '',
                issued_at TEXT NOT NULL,
                due_at TEXT NOT NULL,
                paid_at TEXT,
                artifact_path TEXT DEFAULT '',
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id),
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE TABLE IF NOT EXISTS revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                client_feedback TEXT NOT NULL,
                diff_summary TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'requested',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                expense_date TEXT NOT NULL,
                vendor TEXT NOT NULL,
                category TEXT NOT NULL,
                amount_inr REAL NOT NULL,
                amount_paise INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'INR',
                job_id INTEGER,
                is_recurring INTEGER NOT NULL DEFAULT 0,
                business_use_pct REAL NOT NULL DEFAULT 100.0,
                gst_amount_inr REAL NOT NULL DEFAULT 0.0,
                tax_deductibility TEXT NOT NULL DEFAULT 'unknown',
                reviewer_notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES opportunities(id)
            );

            CREATE TABLE IF NOT EXISTS financial_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_date TEXT NOT NULL,
                tx_type TEXT NOT NULL,
                amount_inr REAL NOT NULL,
                amount_paise INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'INR',
                reference_id TEXT DEFAULT '',
                description TEXT NOT NULL,
                is_reversal INTEGER NOT NULL DEFAULT 0,
                linked_tx_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(linked_tx_id) REFERENCES financial_transactions(id)
            );

            CREATE TABLE IF NOT EXISTS tax_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_name TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT 'individual_sole_proprietor',
                residential_status TEXT NOT NULL DEFAULT 'resident',
                tax_year TEXT NOT NULL DEFAULT '2026-27',
                regime TEXT NOT NULL DEFAULT 'new_2025',
                other_annual_income_inr REAL NOT NULL DEFAULT 0.0,
                advance_tax_paid_inr REAL NOT NULL DEFAULT 0.0,
                tds_credits_inr REAL NOT NULL DEFAULT 0.0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bank_statement_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                reconciled_count INTEGER NOT NULL DEFAULT 0,
                notes TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS bank_statement_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER NOT NULL,
                tx_date TEXT NOT NULL,
                description TEXT NOT NULL,
                amount_inr REAL NOT NULL,
                amount_paise INTEGER NOT NULL,
                tx_type TEXT NOT NULL,
                bank_ref TEXT DEFAULT '',
                is_reconciled INTEGER NOT NULL DEFAULT 0,
                matched_entity_type TEXT DEFAULT '',
                matched_entity_id TEXT DEFAULT '',
                confirmed_by_owner INTEGER NOT NULL DEFAULT 0,
                confirmed_at TEXT,
                FOREIGN KEY(import_id) REFERENCES bank_statement_imports(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS gst_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period TEXT NOT NULL,
                aggregate_turnover_inr REAL NOT NULL DEFAULT 0.0,
                gst_collected_inr REAL NOT NULL DEFAULT 0.0,
                gst_paid_inr REAL NOT NULL DEFAULT 0.0,
                itc_claimed_inr REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'tracking',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bank_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                opening_balance_inr REAL,
                as_of_date TEXT,
                is_confirmed INTEGER NOT NULL DEFAULT 0,
                confirmed_at TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        # Check column migrations in existing tables
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(opportunities)").fetchall()]
        if "content_hash" not in cols:
            conn.execute("ALTER TABLE opportunities ADD COLUMN content_hash TEXT DEFAULT ''")

        exp_cols = [r["name"] for r in conn.execute("PRAGMA table_info(expenses)").fetchall()]
        if "ca_review_confirmed" not in exp_cols:
            conn.execute("ALTER TABLE expenses ADD COLUMN ca_review_confirmed INTEGER NOT NULL DEFAULT 0")
        if "ca_review_notes" not in exp_cols:
            conn.execute("ALTER TABLE expenses ADD COLUMN ca_review_notes TEXT DEFAULT ''")
        if "reviewed_by" not in exp_cols:
            conn.execute("ALTER TABLE expenses ADD COLUMN reviewed_by TEXT DEFAULT ''")
        if "reviewed_at" not in exp_cols:
            conn.execute("ALTER TABLE expenses ADD COLUMN reviewed_at TEXT")


def log_event(event: str, details: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log(event, details, created_at) VALUES (?, ?, ?)",
            (event[:80], details[:4000], utc_now()),
        )


def add_opportunity(data: dict[str, Any], score: float) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO opportunities(
                title, source, url, description, amount_inr, estimated_hours,
                skill_match, payment_confidence, risk_level, source_permitted,
                status, score, content_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)""",
            (
                data["title"][:180],
                data["source"][:80],
                data.get("url", "")[:500],
                data.get("description", "")[:4000],
                float(data["amount_inr"]),
                float(data["estimated_hours"]),
                float(data["skill_match"]),
                float(data["payment_confidence"]),
                float(data["risk_level"]),
                1 if data.get("source_permitted") else 0,
                float(score),
                data.get("content_hash", "")[:128],
                utc_now(),
            ),
        )
        item_id = int(cur.lastrowid)
    log_event("opportunity_added", f"id={item_id}; title={data['title'][:100]}")
    return item_id


def get_opportunity(item_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM opportunities WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else None


def list_opportunities() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM opportunities ORDER BY score DESC, id DESC").fetchall()
    return [dict(row) for row in rows]


VALID_OPPORTUNITY_TRANSITIONS: dict[str, set[str]] = {
    "new": {"shortlisted", "approved_for_work", "rejected"},
    "shortlisted": {"approved_for_work", "new", "rejected"},
    "approved_for_work": {"in_progress", "shortlisted", "rejected"},
    "in_progress": {"submission_ready", "needs_manual_review", "approved_for_work", "rejected"},
    "needs_manual_review": {"in_progress", "rejected"},
    "submission_ready": {"submitted_by_owner", "in_progress", "rejected"},
    "submitted_by_owner": {"won", "lost", "invoiced"},
    "won": {"invoiced", "paid"},
    "invoiced": {"paid", "lost"},
    "paid": set(),  # terminal completed state
    "lost": {"shortlisted", "new"},  # can reopen
    "rejected": {"new", "shortlisted"},  # can reopen
}

VALID_TASK_STATUSES: set[str] = {
    "created",
    "in_progress",
    "qa_testing",
    "revising",
    "submission_ready",
    "completed",
    "failed",
    "needs_manual_review",
}

VALID_PROPOSAL_STATUSES: set[str] = {
    "draft",
    "approved_by_owner",
    "rejected",
}

VALID_INVOICE_STATUSES: set[str] = {
    "issued",
    "payment_pending",
    "paid",
    "cancelled",
}


def update_opportunity_status(item_id: int, status: str) -> None:
    allowed = {
        "new",
        "shortlisted",
        "approved_for_work",
        "in_progress",
        "submission_ready",
        "needs_manual_review",
        "submitted_by_owner",
        "won",
        "invoiced",
        "paid",
        "lost",
        "rejected",
    }
    if status not in allowed:
        raise ValueError("Invalid opportunity status")
    with connect() as conn:
        row = conn.execute("SELECT status, source_permitted FROM opportunities WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise ValueError("Opportunity not found")
        current_status = row["status"]
        if current_status != status:
            allowed_next = VALID_OPPORTUNITY_TRANSITIONS.get(current_status, set())
            if status not in allowed_next:
                raise ValueError(
                    f"Illegal status transition: cannot move opportunity from '{current_status}' to '{status}'."
                )
        if status in {"approved_for_work", "in_progress", "submission_ready", "submitted_by_owner", "won", "invoiced", "paid"} and not row["source_permitted"]:
            raise ValueError("Cannot approve work from a source that has not been marked as permitted")

        if status in {"submission_ready", "submitted_by_owner", "invoiced", "paid"}:
            task_row = conn.execute(
                "SELECT id, status, artifact_path FROM tasks WHERE opportunity_id=? AND status IN ('submission_ready', 'completed')",
                (item_id,),
            ).fetchone()
            if task_row is None or not task_row["artifact_path"]:
                raise ValueError(
                    f"Cannot mark opportunity #{item_id} as '{status}': "
                    "no associated task has completed QA with a verified deliverable package."
                )

        conn.execute("UPDATE opportunities SET status=? WHERE id=?", (status, item_id))
    log_event("opportunity_status_changed", f"id={item_id}; status={status}")


def create_task(title: str, task_type: str, workspace: str, opportunity_id: int | None = None, notes: str = "") -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO tasks(opportunity_id, title, task_type, status, workspace, notes, created_at)
            VALUES (?, ?, ?, 'created', ?, ?, ?)""",
            (opportunity_id, title[:180], task_type[:60], str(workspace), notes[:2000], utc_now()),
        )
        task_id = int(cur.lastrowid)
    log_event("task_created", f"id={task_id}; title={title[:120]}")
    return task_id


def complete_task(task_id: int, artifact_path: str, notes: str = "") -> None:
    with connect() as conn:
        row = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"Task #{task_id} not found")
        conn.execute(
            "UPDATE tasks SET status='completed', artifact_path=?, notes=?, completed_at=? WHERE id=?",
            (artifact_path, notes[:2000], utc_now(), task_id),
        )
    log_event("task_completed", f"id={task_id}; artifact={artifact_path}")


def update_task_status(
    task_id: int,
    status: str,
    notes: str = "",
    artifact_path: str | None = None,
    workspace: str | None = None,
) -> None:
    if status not in VALID_TASK_STATUSES:
        raise ValueError(f"Invalid task status: '{status}'")
    with connect() as conn:
        row = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"Task #{task_id} not found")

        updates = ["status=?", "notes=?"]
        params: list[Any] = [status, notes[:2000]]
        if artifact_path is not None:
            updates.append("artifact_path=?")
            params.append(artifact_path)
        if workspace is not None:
            updates.append("workspace=?")
            params.append(str(workspace))
        if status in {"completed", "submission_ready"}:
            updates.append("completed_at=?")
            params.append(utc_now())
        params.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id=?", params)
    log_event("task_status_changed", f"id={task_id}; status={status}")


def get_task(task_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_tasks() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT t.*, o.title AS opportunity_title FROM tasks t
               LEFT JOIN opportunities o ON o.id=t.opportunity_id
               ORDER BY t.id DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def add_revenue(
    description: str,
    amount_inr: float,
    status: str,
    opportunity_id: int | None = None,
    task_id: int | None = None,
) -> int:
    try:
        amt = float(amount_inr)
    except (ValueError, TypeError):
        raise ValueError("Invalid amount")

    if not math.isfinite(amt) or amt <= 0:
        raise ValueError("Amount must be a finite number greater than zero")

    if status not in {"expected", "invoiced", "paid", "cancelled"}:
        raise ValueError("Invalid revenue status")

    if status == "paid":
        if opportunity_id is not None:
            with connect() as conn:
                opp_row = conn.execute("SELECT status FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
            if opp_row is None:
                raise ValueError("Referenced opportunity does not exist")
            if opp_row["status"] not in {"submitted_by_owner", "won", "invoiced", "paid"}:
                raise ValueError(
                    f"Cannot record confirmed 'paid' revenue for an opportunity in status '{opp_row['status']}'. "
                    "Work must be submitted by the owner first."
                )
        raise ValueError(
            "Direct paid-revenue entries are disabled. Record a paid receipt through the invoice workflow "
            "after independently checking the bank statement and confirming the payment reference."
        )

    paid_at = None
    with connect() as conn:
        if opportunity_id is not None:
            opp_row = conn.execute("SELECT status FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
            if opp_row is None:
                raise ValueError("Referenced opportunity does not exist")
            if status == "paid" and opp_row["status"] not in {"submitted_by_owner", "won", "invoiced", "paid"}:
                raise ValueError(
                    f"Cannot record confirmed 'paid' revenue for an opportunity in status '{opp_row['status']}'. "
                    "Work must be submitted by the owner first."
                )
        cur = conn.execute(
            """INSERT INTO revenue(opportunity_id, task_id, description, amount_inr, status, paid_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (opportunity_id, task_id, description[:180], float(amt), status, paid_at, utc_now()),
        )
        item_id = int(cur.lastrowid)
    log_event("revenue_recorded", f"id={item_id}; amount={amt}; status={status}")
    return item_id


def list_revenue() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM revenue ORDER BY id DESC").fetchall()
    return [dict(row) for row in rows]


def metrics() -> dict[str, float | int]:
    with connect() as conn:
        paid = conn.execute("SELECT COALESCE(SUM(amount_inr),0) FROM revenue WHERE status='paid'").fetchone()[0]
        pipeline = conn.execute("SELECT COALESCE(SUM(amount_inr),0) FROM revenue WHERE status IN ('expected','invoiced')").fetchone()[0]
        opportunities = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('completed', 'submission_ready')").fetchone()[0]
    return {"paid_inr": float(paid), "pipeline_inr": float(pipeline), "opportunities": int(opportunities), "completed_tasks": int(tasks)}


def recent_audit(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


# Milestone 4: Discovery Sources & Proposals Management

def add_discovery_source(
    name: str,
    source_type: str,
    url_or_path: str,
    is_permitted: bool = True,
    is_active: bool = True,
    poll_interval_minutes: int = 60,
) -> int:
    valid_types = {"rss", "atom", "json_api", "local_folder"}
    if source_type not in valid_types:
        raise ValueError(f"Invalid source type '{source_type}'. Allowed: {', '.join(sorted(valid_types))}")
    if not name.strip():
        raise ValueError("Source name is required")
    if not url_or_path.strip():
        raise ValueError("URL or file path is required")

    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO discovery_sources(name, source_type, url_or_path, is_permitted, is_active, poll_interval_minutes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                name.strip()[:100],
                source_type,
                url_or_path.strip()[:500],
                1 if is_permitted else 0,
                1 if is_active else 0,
                max(5, int(poll_interval_minutes)),
                utc_now(),
            ),
        )
        source_id = int(cur.lastrowid)
    log_event("discovery_source_added", f"id={source_id}; name={name}")
    return source_id


def list_discovery_sources(active_only: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM discovery_sources"
    if active_only:
        query += " WHERE is_active=1"
    query += " ORDER BY id ASC"
    with connect() as conn:
        rows = conn.execute(query).fetchall()
    return [dict(row) for row in rows]


def get_discovery_source(source_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM discovery_sources WHERE id=?", (source_id,)).fetchone()
    return dict(row) if row else None


def update_discovery_source_polled(source_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE discovery_sources SET last_polled_at=? WHERE id=?",
            (utc_now(), source_id),
        )


def delete_discovery_source(source_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM discovery_sources WHERE id=?", (source_id,))
    log_event("discovery_source_deleted", f"id={source_id}")


def save_proposal_draft(opportunity_id: int, draft_text: str, model_used: str = "") -> int:
    if not draft_text.strip():
        raise ValueError("Proposal draft text cannot be empty")
    with connect() as conn:
        opp = conn.execute("SELECT id FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
        if opp is None:
            raise ValueError(f"Opportunity #{opportunity_id} not found")

        existing = conn.execute("SELECT id FROM proposals WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        now = utc_now()
        if existing:
            prop_id = int(existing["id"])
            conn.execute(
                "UPDATE proposals SET draft_text=?, model_used=?, updated_at=? WHERE id=?",
                (draft_text[:10000], model_used[:60], now, prop_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO proposals(opportunity_id, draft_text, status, model_used, created_at, updated_at)
                VALUES (?, ?, 'draft', ?, ?, ?)""",
                (opportunity_id, draft_text[:10000], model_used[:60], now, now),
            )
            prop_id = int(cur.lastrowid)
    log_event("proposal_draft_saved", f"id={prop_id}; opp_id={opportunity_id}")
    return prop_id


def get_proposal(opportunity_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM proposals WHERE opportunity_id=?", (opportunity_id,)).fetchone()
    return dict(row) if row else None


def update_proposal_status(proposal_id: int, status: str) -> None:
    if status not in VALID_PROPOSAL_STATUSES:
        raise ValueError(f"Invalid proposal status '{status}'")
    with connect() as conn:
        row = conn.execute("SELECT id FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if row is None:
            raise ValueError(f"Proposal #{proposal_id} not found")
        conn.execute("UPDATE proposals SET status=?, updated_at=? WHERE id=?", (status, utc_now(), proposal_id))
    log_event("proposal_status_changed", f"id={proposal_id}; status={status}")


def list_proposals() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.*, o.title as opportunity_title, o.source as opportunity_source, o.amount_inr
               FROM proposals p
               JOIN opportunities o ON o.id=p.opportunity_id
               ORDER BY p.id DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


# Milestone 5: Revenue Operations, Invoicing, Revisions & Resilient Concurrency

def claim_opportunity_for_execution(opportunity_id: int) -> bool:
    """Atomically acquire a per-opportunity execution lease across threads/processes."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        opp = conn.execute(
            "SELECT id, status, source_permitted FROM opportunities WHERE id=?",
            (opportunity_id,),
        ).fetchone()
        if opp is None or not opp["source_permitted"] or opp["status"] not in {"approved_for_work", "in_progress"}:
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO execution_claims(opportunity_id, claimed_at) VALUES (?, ?)",
            (opportunity_id, utc_now()),
        )
        if cur.rowcount != 1:
            return False
        if opp["status"] == "approved_for_work":
            conn.execute("UPDATE opportunities SET status='in_progress' WHERE id=?", (opportunity_id,))
    log_event("opportunity_claimed_for_execution", f"id={opportunity_id}")
    return True


def release_opportunity_execution_claim(opportunity_id: int) -> None:
    """Release the execution lease after QA/failure handling is finished."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM execution_claims WHERE opportunity_id=?", (opportunity_id,))
    if cur.rowcount:
        log_event("opportunity_execution_claim_released", f"id={opportunity_id}")


def recover_interrupted_tasks(stale_after_seconds: int = 6 * 60 * 60) -> list[int]:
    """Flag genuinely interrupted work while leaving fresh execution leases alone.

    A running worker holds an execution_claims row. The separate discovery
    scheduler must not mistake that live task for a crash. A stale lease is
    reclaimed only after the configured grace period (six hours by default);
    tasks with no lease are legacy/interrupted records and are flagged for review.
    """
    if isinstance(stale_after_seconds, bool) or not isinstance(stale_after_seconds, int) or stale_after_seconds < 60:
        raise ValueError("stale_after_seconds must be an integer of at least 60 seconds")
    stale_before = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)).isoformat(timespec="seconds")
    recovered_opps: list[int] = []
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT DISTINCT o.id FROM opportunities o
               JOIN tasks t ON t.opportunity_id = o.id
               LEFT JOIN execution_claims c ON c.opportunity_id = o.id
               WHERE o.status = 'in_progress'
                 AND t.status IN ('created', 'in_progress', 'qa_testing', 'revising')
                 AND (c.opportunity_id IS NULL OR c.claimed_at <= ?)""",
            (stale_before,),
        ).fetchall()
        for row in rows:
            opp_id = int(row["id"])
            conn.execute("DELETE FROM execution_claims WHERE opportunity_id = ?", (opp_id,))
            conn.execute("UPDATE opportunities SET status = 'approved_for_work' WHERE id = ?", (opp_id,))
            conn.execute(
                "UPDATE tasks SET status = 'needs_manual_review', notes = 'Interrupted execution recovered; owner review required' WHERE opportunity_id = ? AND status IN ('created', 'in_progress', 'qa_testing', 'revising')",
                (opp_id,),
            )
            recovered_opps.append(opp_id)
    if recovered_opps:
        log_event("crash_recovery_executed", f"recovered_opps={recovered_opps}")
    return recovered_opps


def generate_invoice_number() -> str:
    """Generates sequential invoice identifier: INV-YYYY-XXXX."""
    year = datetime.now(timezone.utc).year
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE invoice_number LIKE ?",
            (f"INV-{year}-%",),
        ).fetchone()
        count = (row[0] if row else 0) + 1
    return f"INV-{year}-{count:04d}"


def create_invoice(
    opportunity_id: int,
    client_name: str,
    service_tier: str = "data_analytics",
    amount_subtotal: float = 0.0,
    tax_rate: float = 0.0,
    task_id: int | None = None,
    due_days: int = 14,
    notes: str = "",
) -> dict[str, Any]:
    client = str(client_name or "").strip()
    if not client:
        raise ValueError("Client name cannot be empty")
    if isinstance(amount_subtotal, bool) or isinstance(tax_rate, bool) or isinstance(due_days, bool):
        raise ValueError("Subtotal, tax rate and due days must be numeric values, not booleans")
    try:
        subtotal = float(amount_subtotal)
        tax_rate_value = float(tax_rate)
        due_days_number = float(due_days)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Subtotal, tax rate and due days must be valid numbers") from exc
    if not math.isfinite(due_days_number) or not due_days_number.is_integer():
        raise ValueError("Payment due days must be a finite whole number")
    due_days_value = int(due_days_number)
    if not math.isfinite(subtotal) or subtotal <= 0:
        raise ValueError("Subtotal must be a finite number greater than zero")
    if not math.isfinite(tax_rate_value) or not 0 <= tax_rate_value <= 28:
        raise ValueError("Tax rate must be finite and between 0% and 28%")
    if not 1 <= due_days_value <= 60:
        raise ValueError("Payment due days must be between 1 and 60")

    supported_tiers = {"data_analytics", "data_engineering", "interactive_dashboard", "business_automation"}
    tier = str(service_tier or "").strip()
    if tier not in supported_tiers:
        raise ValueError(f"Unsupported service tier: {tier!r}")

    tax_amount = round(subtotal * (tax_rate_value / 100.0), 2)
    amount_total = round(subtotal + tax_amount, 2)

    with connect() as conn:
        # Serialize invoice numbering and duplicate checks across concurrent callers.
        conn.execute("BEGIN IMMEDIATE")
        opp = conn.execute("SELECT id, title, status, source_permitted FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
        if opp is None:
            raise ValueError(f"Opportunity #{opportunity_id} not found")
        if not opp["source_permitted"]:
            raise ValueError("Cannot invoice an opportunity from an unpermitted source")
        if opp["status"] not in {"submitted_by_owner", "won"}:
            raise ValueError(
                f"Cannot generate invoice for opportunity #{opportunity_id} in status '{opp['status']}'. "
                "The owner must submit the completed deliverable first."
            )

        active_invoice = conn.execute(
            "SELECT invoice_number FROM invoices WHERE opportunity_id=? AND status <> 'cancelled' LIMIT 1",
            (opportunity_id,),
        ).fetchone()
        if active_invoice:
            raise ValueError(f"An active invoice ({active_invoice['invoice_number']}) already exists for this opportunity")

        if task_id is None:
            task_row = conn.execute(
                """SELECT id, status, artifact_path FROM tasks
                   WHERE opportunity_id=? AND status IN ('submission_ready', 'completed') AND artifact_path <> ''
                   ORDER BY id DESC LIMIT 1""",
                (opportunity_id,),
            ).fetchone()
        else:
            task_row = conn.execute(
                "SELECT id, opportunity_id, status, artifact_path FROM tasks WHERE id=? AND opportunity_id=?",
                (task_id, opportunity_id),
            ).fetchone()
            if task_row is not None and (task_row["status"] not in {"submission_ready", "completed"} or not task_row["artifact_path"]):
                task_row = None
        if task_row is None:
            raise ValueError("An associated QA-ready task with a deliverable artifact is required before invoicing")
        task_id_value = int(task_row["id"])

        year = datetime.now(timezone.utc).year
        count_row = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE invoice_number LIKE ?",
            (f"INV-{year}-%",),
        ).fetchone()
        inv_num = f"INV-{year}-{int(count_row[0] or 0) + 1:04d}"
        now = utc_now()
        due_str = datetime.fromtimestamp(
            datetime.fromisoformat(now).timestamp() + (due_days_value * 86400), timezone.utc
        ).isoformat(timespec="seconds")

        cur = conn.execute(
            """INSERT INTO invoices(
                invoice_number, opportunity_id, task_id, client_name, service_tier,
                amount_subtotal, tax_rate, tax_amount, amount_total, currency,
                status, notes, issued_at, due_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'INR', 'issued', ?, ?, ?)""",
            (
                inv_num, opportunity_id, task_id_value, client[:120], tier[:60],
                subtotal, tax_rate_value, tax_amount, amount_total,
                str(notes or "").strip()[:1000], now, due_str,
            ),
        )
        inv_id = int(cur.lastrowid)
        conn.execute("UPDATE opportunities SET status='invoiced' WHERE id=?", (opportunity_id,))

    log_event("invoice_created", f"invoice={inv_num}; total={amount_total}; opp_id={opportunity_id}; task_id={task_id_value}")
    return get_invoice(inv_id)  # type: ignore[return-value]


def get_invoice(invoice_id: int | None = None, invoice_number: str | None = None) -> dict[str, Any] | None:
    with connect() as conn:
        if invoice_id is not None:
            row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        elif invoice_number is not None:
            row = conn.execute("SELECT * FROM invoices WHERE invoice_number=?", (invoice_number,)).fetchone()
        else:
            return None
    return dict(row) if row else None


def list_invoices() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT i.*, o.title as opportunity_title
               FROM invoices i
               JOIN opportunities o ON o.id=i.opportunity_id
               ORDER BY i.id DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def verify_invoice_payment(
    invoice_id: int,
    payment_reference: str,
    verified_amount: float,
    owner_confirmation: bool = False,
) -> dict[str, Any]:
    """
    Strict payment receipt verification workflow.
    Requires owner confirmation checkbox, non-empty UTR/transaction reference,
    and exact amount matching invoice total.
    """
    if owner_confirmation is not True:
        raise ValueError("Payment verification requires explicit human owner confirmation")
    if not isinstance(payment_reference, str) or not payment_reference.strip():
        raise ValueError("Bank payment reference (UTR / Transaction ID) is required")
    if isinstance(verified_amount, bool):
        raise ValueError("Verified amount must be a numeric amount, not a boolean")

    try:
        amt = float(verified_amount)
    except (ValueError, TypeError):
        raise ValueError("Verified amount must be a valid number")

    if not math.isfinite(amt) or amt <= 0:
        raise ValueError("Verified amount must be a finite number greater than zero")

    with connect() as conn:
        # Acquire the SQLite write reservation before checking the reference.
        # This prevents two concurrent verifications from reusing one UTR.
        conn.execute("BEGIN IMMEDIATE")
        inv = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if inv is None:
            raise ValueError(f"Invoice #{invoice_id} not found")
        if inv["status"] not in {"issued", "payment_pending"}:
            raise ValueError(f"Invoice #{invoice_id} is not awaiting payment (status: {inv['status']})")
        duplicate_ref = conn.execute(
            "SELECT id FROM invoices WHERE payment_reference=? AND status='paid' AND id<>? LIMIT 1",
            (payment_reference.strip()[:120], invoice_id),
        ).fetchone()
        if duplicate_ref:
            raise ValueError("This payment reference has already been used for another paid invoice")

        inv_total = float(inv["amount_total"])
        if abs(amt - inv_total) >= 0.01:
            raise ValueError(
                f"Payment amount mismatch: received ₹{amt:,.2f} does not match invoice total ₹{inv_total:,.2f}"
            )

        now = utc_now()
        conn.execute(
            """UPDATE invoices 
               SET status='paid', payment_reference=?, payment_verified_by_owner=1, paid_at=?
               WHERE id=?""",
            (payment_reference.strip()[:120], now, invoice_id),
        )

        opp_id = int(inv["opportunity_id"])
        task_id = inv["task_id"]
        # Update opportunity status to paid if allowed
        opp_row = conn.execute("SELECT status FROM opportunities WHERE id=?", (opp_id,)).fetchone()
        if opp_row and opp_row["status"] in {"submitted_by_owner", "won", "invoiced"}:
            conn.execute("UPDATE opportunities SET status='paid' WHERE id=?", (opp_id,))

        # Automatically record paid revenue in financial ledger
        conn.execute(
            """INSERT INTO revenue(opportunity_id, task_id, description, amount_inr, status, paid_at, created_at)
               VALUES (?, ?, ?, ?, 'paid', ?, ?)""",
            (
                opp_id,
                task_id,
                f"Verified payment for {inv['invoice_number']} ({inv['client_name']}) - Ref: {payment_reference.strip()}",
                amt,
                now,
                now,
            ),
        )

        # Post to M6 financial_transactions ledger once
        existing_tx = conn.execute(
            "SELECT id FROM financial_transactions WHERE reference_id=? AND tx_type='client_receipt'",
            (inv["invoice_number"],),
        ).fetchone()
        if not existing_tx:
            subtotal = float(inv["amount_subtotal"])
            tax_amt = float(inv["tax_amount"])
            conn.execute(
                """INSERT INTO financial_transactions (
                    tx_date, tx_type, amount_inr, amount_paise, currency,
                    reference_id, description, is_reversal, created_at
                ) VALUES (?, 'client_receipt', ?, ?, 'INR', ?, ?, 0, ?)""",
                (
                    now[:10],
                    subtotal,
                    int(round(subtotal * 100)),
                    inv["invoice_number"],
                    f"Owner-confirmed client receipt for {inv['invoice_number']} ({inv['client_name']})",
                    now,
                ),
            )
            if tax_amt > 0:
                conn.execute(
                    """INSERT INTO financial_transactions (
                        tx_date, tx_type, amount_inr, amount_paise, currency,
                        reference_id, description, is_reversal, created_at
                    ) VALUES (?, 'gst_collected', ?, ?, 'INR', ?, ?, 0, ?)""",
                    (
                        now[:10],
                        tax_amt,
                        int(round(tax_amt * 100)),
                        inv["invoice_number"],
                        f"GST collected on {inv['invoice_number']}",
                        now,
                    ),
                )

    log_event("invoice_payment_verified", f"invoice_id={invoice_id}; ref={payment_reference}; amount={amt}")
    return get_invoice(invoice_id)  # type: ignore[return-value]


def set_bank_opening_balance(
    balance_inr: float,
    as_of_date: str = "",
    account_name: str = "Primary Operating Account",
    is_confirmed: bool = True,
) -> int:
    """Sets owner-confirmed bank statement opening/closing balance and date."""
    if not math.isfinite(balance_inr):
        raise ValueError("Bank balance must be a finite number")
    date_val = as_of_date.strip() if as_of_date else utc_now()[:10]
    with connect() as conn:
        conn.execute("DELETE FROM bank_accounts WHERE account_name = ?", (account_name,))
        cur = conn.execute(
            """INSERT INTO bank_accounts (
                account_name, opening_balance_inr, as_of_date, is_confirmed, confirmed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                account_name,
                float(balance_inr),
                date_val,
                1 if is_confirmed else 0,
                utc_now() if is_confirmed else None,
                utc_now(),
            ),
        )
        return int(cur.lastrowid)


def get_bank_opening_balance(account_name: str = "Primary Operating Account") -> dict[str, Any] | None:
    """Returns the current owner-confirmed bank opening/closing balance, or None if unconfirmed."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM bank_accounts WHERE account_name = ? AND is_confirmed = 1 ORDER BY id DESC LIMIT 1",
            (account_name,),
        ).fetchone()
        return dict(row) if row else None


def upsert_tax_profile(
    profile_name: str = "Primary Proprietor",
    entity_type: str = "individual_sole_proprietor",
    residential_status: str = "resident",
    tax_year: str = "2026-27",
    regime: str = "new_2025",
    other_annual_income_inr: float = 0.0,
    advance_tax_paid_inr: float = 0.0,
    tds_credits_inr: float = 0.0,
) -> int:
    """Inserts or updates the active tax profile."""
    with connect() as conn:
        conn.execute("UPDATE tax_profiles SET is_active = 0 WHERE is_active = 1")
        cur = conn.execute(
            """INSERT INTO tax_profiles (
                profile_name, entity_type, residential_status, tax_year, regime,
                other_annual_income_inr, advance_tax_paid_inr, tds_credits_inr,
                is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                profile_name[:100],
                entity_type[:50],
                residential_status[:50],
                tax_year[:20],
                regime[:50],
                max(0.0, float(other_annual_income_inr)),
                max(0.0, float(advance_tax_paid_inr)),
                max(0.0, float(tds_credits_inr)),
                utc_now(),
            ),
        )
        return int(cur.lastrowid)


def get_active_tax_profile() -> dict[str, Any] | None:
    """Retrieves the active tax profile, or None if none configured."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM tax_profiles WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def create_revision_request(task_id: int, client_feedback: str) -> int:
    if not client_feedback.strip():
        raise ValueError("Client feedback cannot be empty")
    with connect() as conn:
        task = conn.execute("SELECT id, status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError(f"Task #{task_id} not found")

        cur = conn.execute(
            """INSERT INTO revisions(task_id, client_feedback, status, created_at)
               VALUES (?, ?, 'requested', ?)""",
            (task_id, client_feedback.strip()[:4000], utc_now()),
        )
        rev_id = int(cur.lastrowid)
        conn.execute("UPDATE tasks SET status='revising' WHERE id=?", (task_id,))
    log_event("revision_requested", f"task_id={task_id}; revision_id={rev_id}")
    return rev_id


def list_revisions(task_id: int | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        if task_id is not None:
            rows = conn.execute("SELECT * FROM revisions WHERE task_id=? ORDER BY id DESC", (task_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM revisions ORDER BY id DESC").fetchall()
    return [dict(row) for row in rows]


def complete_revision(revision_id: int, diff_summary: str = "") -> None:
    with connect() as conn:
        rev = conn.execute("SELECT id, task_id FROM revisions WHERE id=?", (revision_id,)).fetchone()
        if rev is None:
            raise ValueError(f"Revision #{revision_id} not found")
        now = utc_now()
        conn.execute(
            "UPDATE revisions SET status='completed', diff_summary=?, completed_at=? WHERE id=?",
            (diff_summary[:2000], now, revision_id),
        )
        conn.execute("UPDATE tasks SET status='submission_ready' WHERE id=?", (rev["task_id"],))
    log_event("revision_completed", f"revision_id={revision_id}")
