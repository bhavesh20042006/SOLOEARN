from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

from core.config import DB_PATH, ensure_directories


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Generator[sqlite3.Connection, None, None]:
    ensure_directories()
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
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
            """
        )
        # Check column migration for content_hash in existing tables
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(opportunities)").fetchall()]
        if "content_hash" not in cols:
            conn.execute("ALTER TABLE opportunities ADD COLUMN content_hash TEXT DEFAULT ''")


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

    paid_at = utc_now() if status == "paid" else None
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
    """
    Atomically claims an opportunity for execution using an atomic SQL UPDATE.
    Transitions status from 'approved_for_work' to 'in_progress'.
    Returns True if successfully claimed; False if already claimed or invalid state.
    Guarantees thread-safe and process-safe concurrency.
    """
    with connect() as conn:
        cur = conn.execute(
            """UPDATE opportunities 
               SET status = 'in_progress' 
               WHERE id = ? AND status = 'approved_for_work' AND source_permitted = 1""",
            (opportunity_id,),
        )
        claimed = cur.rowcount > 0
    if claimed:
        log_event("opportunity_claimed_for_execution", f"id={opportunity_id}")
    return claimed


def recover_interrupted_tasks() -> list[int]:
    """
    Identifies tasks or opportunities left in an inconsistent/in_progress state
    due to process interruption or crash, and recovers them safely to 'approved_for_work'.
    """
    recovered_opps: list[int] = []
    with connect() as conn:
        rows = conn.execute(
            """SELECT o.id FROM opportunities o
               JOIN tasks t ON t.opportunity_id = o.id
               WHERE o.status = 'in_progress' AND t.status IN ('created', 'in_progress', 'qa_testing', 'revising')"""
        ).fetchall()
        for r in rows:
            opp_id = int(r["id"])
            conn.execute("UPDATE opportunities SET status = 'approved_for_work' WHERE id = ?", (opp_id,))
            conn.execute("UPDATE tasks SET status = 'needs_manual_review', notes = 'Interrupted execution recovered on startup' WHERE opportunity_id = ?", (opp_id,))
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
    if not client_name.strip():
        raise ValueError("Client name cannot be empty")
    try:
        subtotal = float(amount_subtotal)
    except (ValueError, TypeError):
        raise ValueError("Subtotal must be a valid number")
    if not math.isfinite(subtotal) or subtotal <= 0:
        raise ValueError("Subtotal must be a finite number greater than zero")

    tax_rate = max(0.0, float(tax_rate))
    tax_amount = round(subtotal * (tax_rate / 100.0), 2)
    amount_total = round(subtotal + tax_amount, 2)

    with connect() as conn:
        opp = conn.execute("SELECT id, title, status FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
        if opp is None:
            raise ValueError(f"Opportunity #{opportunity_id} not found")
        if opp["status"] not in {"submitted_by_owner", "won", "invoiced", "submission_ready"}:
            raise ValueError(
                f"Cannot generate invoice for opportunity #{opportunity_id} in status '{opp['status']}'. "
                "Deliverables must be completed and submitted first."
            )

        inv_num = generate_invoice_number()
        now = utc_now()
        due_at = datetime.fromisoformat(now).timestamp() + (due_days * 86400)
        due_str = datetime.fromtimestamp(due_at, timezone.utc).isoformat(timespec="seconds")

        cur = conn.execute(
            """INSERT INTO invoices(
                invoice_number, opportunity_id, task_id, client_name, service_tier,
                amount_subtotal, tax_rate, tax_amount, amount_total, currency,
                status, notes, issued_at, due_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'INR', 'issued', ?, ?, ?)""",
            (
                inv_num,
                opportunity_id,
                task_id,
                client_name.strip()[:120],
                service_tier.strip()[:60],
                subtotal,
                tax_rate,
                tax_amount,
                amount_total,
                notes.strip()[:1000],
                now,
                due_str,
            ),
        )
        inv_id = int(cur.lastrowid)

        # Update opportunity to invoiced if eligible
        if opp["status"] in {"submitted_by_owner", "won"}:
            conn.execute("UPDATE opportunities SET status='invoiced' WHERE id=?", (opportunity_id,))

    log_event("invoice_created", f"invoice={inv_num}; total={amount_total}; opp_id={opportunity_id}")
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
    if not owner_confirmation:
        raise ValueError("Payment verification requires explicit human owner confirmation")
    if not payment_reference or not payment_reference.strip():
        raise ValueError("Bank payment reference (UTR / Transaction ID) is required")

    try:
        amt = float(verified_amount)
    except (ValueError, TypeError):
        raise ValueError("Verified amount must be a valid number")

    if not math.isfinite(amt) or amt <= 0:
        raise ValueError("Verified amount must be a finite number greater than zero")

    with connect() as conn:
        inv = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if inv is None:
            raise ValueError(f"Invoice #{invoice_id} not found")
        if inv["status"] == "paid":
            raise ValueError(f"Invoice #{invoice_id} is already marked as paid")

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

    log_event("invoice_payment_verified", f"invoice_id={invoice_id}; ref={payment_reference}; amount={amt}")
    return get_invoice(invoice_id)  # type: ignore[return-value]


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
