from __future__ import annotations

"""
FRIDAY Agent Core (v1.0.0)
The Central Autonomous Brain & Nervous System for SOLOEARN.

Architecture:
- SOLOEARN is the Body: Database, financial ledger, delivery engines, QA audits, workspaces.
- FRIDAY is the Brain: Objectives, reasoning, planning, memory, conversation, and tool selection.
- Permission Controller is the Nervous System: Tool registry, security checks, approval gates,
  action logging, and failure recovery.

Guiding Principles:
1. Supervised Autonomy: Routine tasks execute autonomously; unusual commitments, pricing discounts
   below 50% margin, rush SLAs <24h, and unverified deliverable releases are strictly halted for owner authorization.
2. Confidentiality: Customer data stays local in isolated workspaces and is never transmitted to external APIs.
3. Idempotency: All actions (prospecting, proposals, delivery, reconciliation) are strictly idempotent.
"""

import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import core.config
from core.autonomy_profitability_system import (
    MINIMUM_ACCEPTABLE_CONTRIBUTION_MARGIN,
    MONTHLY_NET_PROFIT_TARGET_INR,
    SERVICE_PROFIT_SPECS,
    AutonomyController,
    DeliveryEngine,
    DeliveryJob,
    FinanceEngine,
    OpportunityEngine,
    ProspectFit,
    QualityAuditResult,
    QualityEngine,
    SalesEngine,
)
from core.commercial_funnel import add_commercial_lead, list_commercial_leads
from core.config import DATA_DIR, WORKSPACES_DIR, safe_workspace_path
from core.db import connect, get_opportunity, get_task, update_opportunity_status, update_task_status, utc_now
from core.llm import OllamaClient
from core.secrets_manager import (
    constant_time_compare,
    mask_secret,
    redact_secrets_from_text,
    redact_sensitive_dict,
)
from core.upi_engine import SimulatedBankMerchantAdapter, process_merchant_bank_webhook

logger = logging.getLogger("soloearn.friday")

# Permission Tiers for Tool Execution
TIER_AUTONOMOUS = "autonomous"              # Read-only or safe local compute (scouting, QA, internal processing)
TIER_GUARDED_BUSINESS = "guarded_business"  # Business actions bounded by strict rules (proposals >= 50% margin)
TIER_OWNER_APPROVAL = "owner_approval"      # High-risk actions (discounts > 20%, rush SLAs < 24h, unverified releases)


# ===========================================================================
# 1. Database Schema for FRIDAY Brain & Memory
# ===========================================================================

def init_friday_tables() -> None:
    """Initializes long-term memory, action logging, client session, and escalation tables with append-only triggers."""
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,         -- 'client', 'prospect', 'market_insight', 'system'
                entity_id TEXT NOT NULL,           -- e.g. client_name or domain
                memory_key TEXT NOT NULL,          -- e.g. 'preferred_service', 'data_format', 'objections'
                memory_value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(entity_type, entity_id, memory_key, is_sandbox)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_action_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT NOT NULL,
                step_number INTEGER NOT NULL,
                thought TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_args TEXT NOT NULL,
                tool_result TEXT NOT NULL,
                permission_decision TEXT NOT NULL, -- 'approved_auto', 'escalated_owner', 'rejected_safety'
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_friday_action_logs_no_update
            BEFORE UPDATE ON friday_action_logs
            FOR EACH ROW
            WHEN OLD.is_sandbox = 0
            BEGIN
                SELECT RAISE(ABORT, 'friday_action_logs is an append-only audit trail for production records: modifications are prohibited.');
            END;
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_friday_action_logs_no_delete
            BEFORE DELETE ON friday_action_logs
            FOR EACH ROW
            WHEN OLD.is_sandbox = 0
            BEGIN
                SELECT RAISE(ABORT, 'friday_action_logs is an append-only audit trail for production records: deletions are prohibited.');
            END;
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_client_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_name TEXT NOT NULL,
                contact_channel TEXT NOT NULL,
                stage TEXT NOT NULL,               -- 'identified', 'contacted', 'negotiating', 'active_job', 'completed'
                opportunity_id INTEGER,
                task_id INTEGER,
                invoice_number TEXT,
                last_interaction_at TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '{}',
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(client_name, is_sandbox)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_escalations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_args TEXT NOT NULL,
                args_hash TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_owner', -- 'pending_owner', 'approved', 'rejected', 'consumed'
                owner_notes TEXT DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                is_consumed INTEGER NOT NULL DEFAULT 0,
                consumed_at TEXT,
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            """
        )
        # Ensure migration columns for friday_escalations
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(friday_escalations)").fetchall()]
        if "args_hash" not in cols:
            conn.execute("ALTER TABLE friday_escalations ADD COLUMN args_hash TEXT DEFAULT ''")
        if "expires_at" not in cols:
            conn.execute("ALTER TABLE friday_escalations ADD COLUMN expires_at TEXT DEFAULT ''")
        if "is_consumed" not in cols:
            conn.execute("ALTER TABLE friday_escalations ADD COLUMN is_consumed INTEGER DEFAULT 0")
        if "consumed_at" not in cols:
            conn.execute("ALTER TABLE friday_escalations ADD COLUMN consumed_at TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_accepted_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_name TEXT NOT NULL,
                proposal_id INTEGER NOT NULL,
                opportunity_id INTEGER,
                service_tier TEXT NOT NULL,
                agreed_price_inr REAL NOT NULL,
                acceptance_ref TEXT NOT NULL,
                dataset_sha256 TEXT NOT NULL,
                dataset_bytes INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'accepted', -- 'accepted', 'in_delivery', 'completed'
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                accepted_at TEXT NOT NULL,
                FOREIGN KEY(proposal_id) REFERENCES proposals(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_deliverable_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                task_id INTEGER NOT NULL,
                opportunity_id INTEGER,
                invoice_number TEXT NOT NULL,
                client_name TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                file_sha256 TEXT NOT NULL,
                file_size_bytes INTEGER NOT NULL,
                is_downloaded INTEGER NOT NULL DEFAULT 0,
                download_count INTEGER NOT NULL DEFAULT 0,
                max_downloads INTEGER NOT NULL DEFAULT 1,
                expires_at TEXT NOT NULL,
                downloaded_at TEXT,
                created_at TEXT NOT NULL,
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                UNIQUE(token, is_sandbox),
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );
            """
        )

        # Ensure foreign keys cascade deletes for existing databases
        fk_orders = [dict(r) for r in conn.execute("PRAGMA foreign_key_list(friday_accepted_orders)").fetchall()]
        if fk_orders and any(f["table"] == "proposals" and f.get("on_delete") != "CASCADE" for f in fk_orders):
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("ALTER TABLE friday_accepted_orders RENAME TO _friday_accepted_orders_old")
            conn.execute(
                """
                CREATE TABLE friday_accepted_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_name TEXT NOT NULL,
                    proposal_id INTEGER NOT NULL,
                    opportunity_id INTEGER,
                    service_tier TEXT NOT NULL,
                    agreed_price_inr REAL NOT NULL,
                    acceptance_ref TEXT NOT NULL,
                    dataset_sha256 TEXT NOT NULL,
                    dataset_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'accepted',
                    is_sandbox INTEGER NOT NULL DEFAULT 0,
                    accepted_at TEXT NOT NULL,
                    FOREIGN KEY(proposal_id) REFERENCES proposals(id) ON DELETE CASCADE
                );
                """
            )
            conn.execute("INSERT INTO friday_accepted_orders SELECT * FROM _friday_accepted_orders_old")
            conn.execute("DROP TABLE _friday_accepted_orders_old")
            conn.execute("PRAGMA foreign_keys = ON")

        fk_tokens = [dict(r) for r in conn.execute("PRAGMA foreign_key_list(friday_deliverable_tokens)").fetchall()]
        if fk_tokens and any(f["table"] == "tasks" and f.get("on_delete") != "CASCADE" for f in fk_tokens):
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("ALTER TABLE friday_deliverable_tokens RENAME TO _friday_deliverable_tokens_old")
            conn.execute(
                """
                CREATE TABLE friday_deliverable_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    task_id INTEGER NOT NULL,
                    opportunity_id INTEGER,
                    invoice_number TEXT NOT NULL,
                    client_name TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    file_sha256 TEXT NOT NULL,
                    file_size_bytes INTEGER NOT NULL,
                    is_downloaded INTEGER NOT NULL DEFAULT 0,
                    download_count INTEGER NOT NULL DEFAULT 0,
                    max_downloads INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT NOT NULL,
                    downloaded_at TEXT,
                    created_at TEXT NOT NULL,
                    is_sandbox INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(token, is_sandbox),
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                """
            )
            conn.execute("INSERT INTO friday_deliverable_tokens SELECT * FROM _friday_deliverable_tokens_old")
            conn.execute("DROP TABLE _friday_deliverable_tokens_old")
            conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_delivery_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL,
                task_id INTEGER NOT NULL,
                client_name TEXT NOT NULL,
                delivery_method TEXT NOT NULL,
                status TEXT NOT NULL,
                bytes_transferred INTEGER NOT NULL,
                file_sha256 TEXT NOT NULL,
                ip_or_recipient TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                is_sandbox INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_communication_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_name TEXT NOT NULL,
                channel TEXT NOT NULL,
                recipient TEXT NOT NULL,
                subject TEXT DEFAULT '',
                body_text TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_message_id TEXT DEFAULT '',
                status TEXT NOT NULL,
                error_message TEXT DEFAULT '',
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_type TEXT NOT NULL,         -- 'client_delivery', 'negotiation', 'qa_incident', 'cycle_run'
                entity_id TEXT NOT NULL,           -- client_name, service_tier, cycle_id
                summary TEXT NOT NULL,
                key_learnings TEXT NOT NULL DEFAULT '[]', -- JSON list of strings
                outcome TEXT NOT NULL,             -- 'success', 'escalated', 'failed', 'settled'
                metadata_json TEXT NOT NULL DEFAULT '{}',
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_friday_episodes_no_update
            BEFORE UPDATE ON friday_episodes
            FOR EACH ROW
            WHEN OLD.is_sandbox = 0
            BEGIN
                SELECT RAISE(ABORT, 'friday_episodes is an append-only audit trail for production records: modifications are prohibited.');
            END;
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_friday_episodes_no_delete
            BEFORE DELETE ON friday_episodes
            FOR EACH ROW
            WHEN OLD.is_sandbox = 0
            BEGIN
                SELECT RAISE(ABORT, 'friday_episodes is an append-only audit trail for production records: deletions are prohibited.');
            END;
            """
        )
        conn.commit()


# ===========================================================================
# 2. Data Models
# ===========================================================================

@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    permission_tier: str
    handler: Callable[..., dict[str, Any]]


@dataclass
class ToolExecutionResult:
    tool_name: str
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    blocked_by_permission: bool = False
    escalation_reason: str = ""


@dataclass
class FridayThought:
    goal: str
    observation: str
    reasoning: str
    proposed_tool: str
    tool_arguments: dict[str, Any]
    confidence: float = 1.0


@dataclass
class ReActStep:
    step_number: int
    thought: str
    action_tool: str
    action_args: dict[str, Any]
    observation: str
    is_success: bool
    error_message: str = ""
    escalation_reason: str = ""
    timestamp: str = ""


@dataclass
class FridayCycleReport:
    cycle_id: str
    timestamp: str
    goal: str
    steps_executed: int
    tools_called: list[str]
    actions_approved: int
    actions_escalated: int
    cycle_status: str
    summary_notes: str
    react_trace: list[dict[str, Any]] = field(default_factory=list)
    fallback_reasons: list[str] = field(default_factory=list)


# ===========================================================================
# 3. Controlled Tool Registry & Permission Controller (The Nervous System)
# ===========================================================================

class FridayPermissionController:
    """
    Evaluates every tool call requested by FRIDAY against strict commercial,
    legal, security, and mathematical invariants before execution.
    """

    def __init__(self, is_sandbox: bool = False):
        self.is_sandbox = is_sandbox
        self.escalation_queue: list[dict[str, Any]] = []

    def is_autonomous_outreach_enabled(self) -> bool:
        """Checks if owner has explicitly enabled autonomous outreach in configuration."""
        val = os.environ.get("SOLOEARN_AUTONOMOUS_OUTREACH", "false").strip().lower()
        return val in ("true", "1", "yes", "enabled")

    def is_escalation_approved(self, tool_name: str, arguments: dict[str, Any], consume: bool = True) -> bool:
        """
        Checks if an exact, single-use, unexpired escalation matching tool_name and complete
        canonical arguments hash was approved by the human owner.
        """
        current_hash = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode("utf-8")).hexdigest()
        now = utc_now()
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, expires_at, is_consumed FROM friday_escalations 
                WHERE tool_name = ? AND args_hash = ? AND status = 'approved' 
                  AND is_consumed = 0 AND is_sandbox = ?
                ORDER BY id DESC LIMIT 1
                """,
                (tool_name, current_hash, int(self.is_sandbox)),
            ).fetchone()
            if not row:
                conn.commit()
                return False
            if row["expires_at"] and row["expires_at"] <= now:
                conn.commit()
                return False

            if consume:
                cur = conn.execute(
                    "UPDATE friday_escalations SET is_consumed = 1, consumed_at = ?, status = 'consumed' WHERE id = ? AND is_consumed = 0",
                    (now, row["id"]),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return False
            conn.commit()
            return True

    def record_escalation(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        reason: str,
        cycle_id: str = "",
        validity_hours: int = 24,
    ) -> int:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(hours=validity_hours)).isoformat()
        args_hash = hashlib.sha256(json.dumps(tool_args, sort_keys=True).encode("utf-8")).hexdigest()
        with connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO friday_escalations (
                    cycle_id, tool_name, tool_args, args_hash, reason, status,
                    expires_at, is_consumed, is_sandbox, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending_owner', ?, 0, ?, ?)
                """,
                (
                    cycle_id or "ADHOC",
                    tool_name,
                    json.dumps(tool_args),
                    args_hash,
                    reason,
                    expires,
                    int(self.is_sandbox),
                    now,
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def approve_escalation(self, escalation_id: int, owner_notes: str = "") -> dict[str, Any]:
        now = utc_now()
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM friday_escalations WHERE id = ? AND is_sandbox = ?",
                (escalation_id, int(self.is_sandbox)),
            ).fetchone()
            if not row:
                raise ValueError(f"Escalation #{escalation_id} not found in this environment")
            if row["status"] == "consumed" or bool(row["is_consumed"]):
                raise ValueError(f"Escalation #{escalation_id} has already been consumed")
            if row["expires_at"] and row["expires_at"] <= now:
                raise ValueError(f"Escalation #{escalation_id} expired at {row['expires_at']}")
            conn.execute(
                "UPDATE friday_escalations SET status = 'approved', owner_notes = ?, resolved_at = ? WHERE id = ?",
                (owner_notes or "Owner approved action", now, escalation_id),
            )
            conn.commit()
            return {
                "escalation_id": escalation_id,
                "status": "approved",
                "tool_name": row["tool_name"],
                "args_hash": row["args_hash"],
                "tool_args": json.loads(row["tool_args"]),
            }

    def reject_escalation(self, escalation_id: int, reason: str = "") -> dict[str, Any]:
        now = utc_now()
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM friday_escalations WHERE id = ? AND is_sandbox = ?",
                (escalation_id, int(self.is_sandbox)),
            ).fetchone()
            if not row:
                raise ValueError(f"Escalation #{escalation_id} not found in this environment")
            conn.execute(
                "UPDATE friday_escalations SET status = 'rejected', owner_notes = ?, resolved_at = ? WHERE id = ?",
                (reason or "Rejected by owner", now, escalation_id),
            )
            conn.commit()
            return {"escalation_id": escalation_id, "status": "rejected"}

    def evaluate_tool_call(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        client_session: dict[str, Any] | None = None,
    ) -> tuple[bool, str, str]:
        """
        Returns: (is_allowed, decision_type, reason)
        decision_type: 'approved_auto' | 'approved_owner' | 'escalated_owner' | 'rejected_safety'
        """
        # Check if owner previously approved this gated action
        if self.is_escalation_approved(tool.name, arguments):
            return True, "approved_owner", "Owner previously authorized this gated action."

        # Tier 1: Autonomous Tools (Safe local read/processing)
        if tool.permission_tier == TIER_AUTONOMOUS:
            return True, "approved_auto", "Tool is classified as safe autonomous operation"

        # Tier 2: Guarded Business Actions (Proposals, Pricing, Scoping, Outreach)
        if tool.permission_tier == TIER_GUARDED_BUSINESS:
            # Invariant 1: Pricing Margin Floor
            if tool.name in {"draft_commercial_proposal", "calculate_quote"}:
                tier = arguments.get("service_tier", "data_analytics")
                price = float(arguments.get("price_inr", 0.0))
                spec = next((s for s in SERVICE_PROFIT_SPECS if s.tier_id == tier), SERVICE_PROFIT_SPECS[0])
                
                # Check discount boundary (>20% discount requires owner review)
                if price < (spec.target_price_inr * 0.80):
                    reason = f"Proposed fee ₹{price:,.2f} is discounted >20% below standard tier price ₹{spec.target_price_inr:,.2f}"
                    self._escalate("unusual_contract_discount", reason, f"Client: {arguments.get('client_name')}")
                    self.record_escalation(tool.name, arguments, reason)
                    return False, "escalated_owner", reason

                # Check strict 50% margin floor
                direct_cost = spec.direct_cost_inr
                platform_fee = price * (spec.platform_fee_pct / 100.0)
                net_contrib = price - direct_cost - platform_fee
                margin_pct = (net_contrib / price) if price > 0 else 0.0

                if margin_pct < MINIMUM_ACCEPTABLE_CONTRIBUTION_MARGIN:
                    reason = f"Contribution margin {margin_pct*100:.1f}% violates statutory 50.0% floor"
                    self._escalate("margin_floor_violation", reason, f"Client: {arguments.get('client_name')}")
                    self.record_escalation(tool.name, arguments, reason)
                    return False, "rejected_safety", reason

                # Check turnaround SLA (<24h requires owner capacity verification)
                sla_hours = float(arguments.get("sla_hours", 48.0))
                if sla_hours < 24.0:
                    reason = f"Turnaround SLA of {sla_hours}h is under standard 24h limit; owner capacity check required"
                    self._escalate("rush_sla_capacity", reason, f"Client: {arguments.get('client_name')}")
                    self.record_escalation(tool.name, arguments, reason)
                    return False, "escalated_owner", reason

            # Invariant 2: Outbound Communication Gating (Gate 1 / Gate 4)
            if tool.name == "dispatch_outreach_pitch":
                if self.is_autonomous_outreach_enabled():
                    from core.outreach_dispatcher import get_outreach_rate_limit_status
                    can_send, count_today, remaining = get_outreach_rate_limit_status(is_sandbox=self.is_sandbox)
                    if can_send:
                        return True, "approved_auto", f"Autonomous outreach authorized under owner policy ({remaining} sends remaining today)."
                    reason = f"Autonomous outreach daily rate limit reached ({count_today} sent today). Halted for anti-spam protection."
                    self.record_escalation(tool.name, arguments, reason)
                    return False, "escalated_owner", reason

                reason = "Outreach pitch drafted successfully; requires owner review before external dispatch (Gate 1)"
                self._escalate("gate_1_outreach", reason, f"Client: {arguments.get('client_name')}")
                self.record_escalation(tool.name, arguments, reason)
                return False, "escalated_owner", reason

            return True, "approved_auto", "Guarded business action passed all margin and capacity checks"

        # Tier 3: High-Risk / Financial Settlement Actions (Gate 3, Gate 4, Gate 5)
        if tool.permission_tier == TIER_OWNER_APPROVAL:
            if tool.name == "release_completed_deliverables":
                # Ensure payment has been cleared before delivery release
                inv_num = arguments.get("invoice_number", "")
                task_id = arguments.get("task_id")
                if not self._is_invoice_paid(inv_num, task_id=task_id):
                    reason = f"Cannot release deliverables: Invoice {inv_num} has not received verified cleared payment matching Task #{task_id} (Gate 3)"
                    return False, "rejected_safety", reason
                return True, "approved_auto", "Verified cleared payment confirmed; deliverable dispatch approved"

            if tool.name == "process_client_refund":
                reason = f"Refund request of ₹{arguments.get('amount_inr', 0):,.2f} requires mandatory owner authorization (Gate 5)"
                self._escalate("refund_authorization", reason, f"Invoice: {arguments.get('invoice_number')}")
                return False, "escalated_owner", reason

        return False, "rejected_safety", f"Unrecognized permission tier: {tool.permission_tier}"

    def _escalate(self, gate_type: str, description: str, entity_ref: str) -> None:
        exc = {
            "escalation_id": f"FRIDAY-ESC-{int(time.time() * 1000) % 1000000}",
            "gate_type": gate_type,
            "description": description,
            "entity_ref": entity_ref,
            "created_at": utc_now(),
        }
        self.escalation_queue.append(exc)
        logger.warning(f"[FRIDAY ESCALATION] {gate_type}: {description} ({entity_ref})")

    def _is_invoice_paid(self, invoice_number: str, task_id: int | None = None) -> bool:
        if not invoice_number:
            return False
        with connect() as conn:
            row = conn.execute(
                "SELECT status, amount_total, amount_paid, task_id, opportunity_id FROM invoices WHERE invoice_number = ? AND is_sandbox = ?",
                (invoice_number, int(self.is_sandbox)),
            ).fetchone()
            if not row:
                return False
            if str(row["status"]).lower() != "paid":
                return False
            if float(row["amount_paid"] or 0) < float(row["amount_total"]):
                return False
            if task_id is not None:
                if row["task_id"] is not None and int(row["task_id"]) != int(task_id):
                    return False
                t = get_task(task_id)
                if t and t.get("opportunity_id") and row["opportunity_id"]:
                    if int(t["opportunity_id"]) != int(row["opportunity_id"]):
                        return False
            return True


class FridayToolRegistry:
    """Manages the catalogue of capabilities that FRIDAY's brain can request."""

    def __init__(self, permission_controller: FridayPermissionController, memory: FridayMemory | None = None):
        self.permission_controller = permission_controller
        self.memory = memory or FridayMemory(is_sandbox=permission_controller.is_sandbox)
        self.tools: dict[str, ToolDefinition] = {}
        self._register_default_tools()

    def register(self, tool: ToolDefinition) -> None:
        self.tools[tool.name] = tool

    def get_tool(self, name: str) -> ToolDefinition | None:
        return self.tools.get(name)

    def _validate_arguments(self, tool: ToolDefinition, arguments: dict[str, Any]) -> str | None:
        """Validates arguments against the tool's JSON parameter schema."""
        schema = tool.parameters_schema or {}
        required = schema.get("required", [])
        for req in required:
            if req not in arguments or arguments[req] is None or arguments[req] == "":
                return f"Missing required parameter '{req}'"

        props = schema.get("properties", {})
        for param, val in arguments.items():
            if val is None:
                continue
            if param in props:
                expected_type = props[param].get("type")
                if expected_type == "string" and not isinstance(val, str):
                    return f"Parameter '{param}' expected string, got {type(val).__name__}"
                elif expected_type == "integer":
                    if isinstance(val, bool) or not (isinstance(val, int) or (isinstance(val, float) and val.is_integer())):
                        return f"Parameter '{param}' expected integer, got {type(val).__name__}"
                elif expected_type == "number":
                    if isinstance(val, bool) or not isinstance(val, (int, float)):
                        return f"Parameter '{param}' expected number, got {type(val).__name__}"
                elif expected_type == "boolean" and not isinstance(val, bool):
                    return f"Parameter '{param}' expected boolean, got {type(val).__name__}"
                elif expected_type == "object" and not isinstance(val, dict):
                    return f"Parameter '{param}' expected object, got {type(val).__name__}"
        return None

    def _validate_tool_preconditions(self, tool: ToolDefinition, arguments: dict[str, Any]) -> str | None:
        """Enforces workflow sequence invariants to prevent out-of-sequence execution."""
        name = tool.name
        is_sandbox = int(self.permission_controller.is_sandbox)

        if name == "execute_data_workflow":
            client_name = arguments.get("client_name", "")
            with connect() as conn:
                row = conn.execute(
                    """
                    SELECT id FROM friday_accepted_orders
                    WHERE client_name = ? AND is_sandbox = ? AND status IN ('accepted', 'in_delivery')
                    LIMIT 1
                    """,
                    (client_name, is_sandbox),
                ).fetchone()
                if not row:
                    return f"No accepted customer order found for client '{client_name}'. Acceptance must be registered before execution."

        elif name == "audit_and_verify_deliverables":
            task_id = arguments.get("task_id")
            if task_id is not None:
                t = get_task(task_id)
                if not t:
                    return f"Task #{task_id} does not exist."

        elif name == "release_completed_deliverables":
            task_id = arguments.get("task_id")
            if task_id is not None:
                t = get_task(task_id)
                if not t:
                    return f"Task #{task_id} does not exist."

        return None

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> ToolExecutionResult:
        tool = self.get_tool(name)
        if not tool:
            return ToolExecutionResult(
                tool_name=name,
                success=False,
                error_message=f"Tool '{name}' is not registered in FRIDAY's capability catalogue.",
            )

        # 1. Parameter Schema Validation
        val_err = self._validate_arguments(tool, arguments)
        if val_err:
            return ToolExecutionResult(
                tool_name=name,
                success=False,
                error_message=f"Schema validation error: {val_err}",
            )

        # 2. Workflow Sequence Precondition Validation
        pre_err = self._validate_tool_preconditions(tool, arguments)
        if pre_err:
            return ToolExecutionResult(
                tool_name=name,
                success=False,
                error_message=f"Precondition failed: {pre_err}",
            )

        allowed, decision, reason = self.permission_controller.evaluate_tool_call(tool, arguments)
        if not allowed:
            return ToolExecutionResult(
                tool_name=name,
                success=False,
                blocked_by_permission=True,
                escalation_reason=reason,
                data={"decision": decision, "reason": reason},
            )

        try:
            result_data = tool.handler(**arguments)
            return ToolExecutionResult(
                tool_name=name,
                success=True,
                data=result_data,
            )
        except Exception as e:
            safe_err = redact_secrets_from_text(str(e))
            logger.exception(f"Tool {name} failed execution: {safe_err}")
            return ToolExecutionResult(
                tool_name=name,
                success=False,
                error_message=safe_err,
            )

    def _register_default_tools(self) -> None:
        # Tool 1: Discover & evaluate market opportunities
        self.register(
            ToolDefinition(
                name="discover_and_score_prospects",
                description="Discovers public commercial prospects, evaluates data bottleneck fit, and returns scored targets.",
                parameters_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
                permission_tier=TIER_AUTONOMOUS,
                handler=self._tool_discover_prospects,
            )
        )

        # Tool 2: Draft tailored value pitch
        self.register(
            ToolDefinition(
                name="prepare_client_pitch",
                description="Generates an individual, problem-led business pitch highlighting verified data services.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "company_name": {"type": "string"},
                        "contact_name": {"type": "string"},
                        "service_tier": {"type": "string"},
                    },
                    "required": ["company_name", "service_tier"],
                },
                permission_tier=TIER_AUTONOMOUS,
                handler=self._tool_prepare_pitch,
            )
        )

        # Tool 3: Dispatch outreach message (Gate 1 Guarded)
        self.register(
            ToolDefinition(
                name="dispatch_outreach_pitch",
                description="Transmits approved outreach pitch via designated commercial channel.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "client_name": {"type": "string"},
                        "channel": {"type": "string"},
                        "pitch_text": {"type": "string"},
                    },
                    "required": ["client_name", "channel", "pitch_text"],
                },
                permission_tier=TIER_GUARDED_BUSINESS,
                handler=self._tool_dispatch_outreach,
            )
        )

        # Tool 4: Draft structured commercial proposal (Margin & Capacity Enforced)
        self.register(
            ToolDefinition(
                name="draft_commercial_proposal",
                description="Calculates margin-compliant pricing and generates a formal proposal with standard SLA.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "client_name": {"type": "string"},
                        "service_tier": {"type": "string"},
                        "price_inr": {"type": "number"},
                        "sla_hours": {"type": "number"},
                    },
                    "required": ["client_name", "service_tier"],
                },
                permission_tier=TIER_GUARDED_BUSINESS,
                handler=self._tool_draft_proposal,
            )
        )

        # Tool 4.5: Register formal customer order acceptance & verify uploaded dataset
        self.register(
            ToolDefinition(
                name="register_customer_order_acceptance",
                description="Registers formal client acceptance of an issued proposal with contract/PO reference and verifies input dataset.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "client_name": {"type": "string"},
                        "proposal_id": {"type": "integer"},
                        "acceptance_ref": {"type": "string"},
                        "input_csv_content": {"type": "string"},
                    },
                    "required": ["client_name", "proposal_id", "acceptance_ref", "input_csv_content"],
                },
                permission_tier=TIER_GUARDED_BUSINESS,
                handler=self._tool_register_order_acceptance,
            )
        )

        # Tool 5: Convert order to job and execute workflow (real dataset strictly required)
        self.register(
            ToolDefinition(
                name="execute_data_workflow",
                description="Executes data cleanup, reconciliation, ETL pipeline, or dashboard workflow in an isolated workspace.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "opportunity_id": {"type": "integer"},
                        "client_name": {"type": "string"},
                        "service_tier": {"type": "string"},
                        "input_csv_content": {"type": "string"},
                    },
                    "required": ["client_name", "service_tier", "input_csv_content"],
                },
                permission_tier=TIER_AUTONOMOUS,
                handler=self._tool_execute_workflow,
            )
        )

        # Tool 6: Independent QA verification & revision loop
        self.register(
            ToolDefinition(
                name="audit_and_verify_deliverables",
                description="Audits deliverables for calculation accuracy, formula safety, and formatting; executes repairs if needed.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "integer"},
                        "max_revisions": {"type": "integer"},
                    },
                    "required": ["task_id"],
                },
                permission_tier=TIER_AUTONOMOUS,
                handler=self._tool_audit_deliverables,
            )
        )

        # Tool 7: Issue statutory invoice with UPI QR
        self.register(
            ToolDefinition(
                name="generate_invoice_upi",
                description="Issues statutory invoice with embedded Mode 01 QR and Mode 04 Intent links.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "opportunity_id": {"type": "integer"},
                        "client_name": {"type": "string"},
                        "amount_inr": {"type": "number"},
                    },
                    "required": ["client_name", "amount_inr"],
                },
                permission_tier=TIER_AUTONOMOUS,
                handler=self._tool_generate_invoice,
            )
        )

        # Tool 8: Release deliverable archive to client (Gate 3 Guarded)
        self.register(
            ToolDefinition(
                name="release_completed_deliverables",
                description="Releases deliverable zip package to client upon verified cleared payment.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "integer"},
                        "invoice_number": {"type": "string"},
                        "recipient_email": {"type": "string"},
                    },
                    "required": ["task_id", "invoice_number"],
                },
                permission_tier=TIER_OWNER_APPROVAL,
                handler=self._tool_release_deliverables,
            )
        )

    # Tool Handlers binding directly to SOLOEARN core engines
    def _tool_discover_prospects(self, limit: int = 5) -> dict[str, Any]:
        prospects = OpportunityEngine.find_and_score_prospects(is_sandbox=self.permission_controller.is_sandbox)
        return {
            "total_found": len(prospects),
            "prospects": [asdict(p) for p in prospects[:limit]],
        }

    def _tool_prepare_pitch(self, company_name: str, contact_name: str = "Founder", service_tier: str = "data_analytics") -> dict[str, Any]:
        pitch = OpportunityEngine.prepare_personalized_approach(
            company_name=company_name,
            contact_name=contact_name,
            service_tier=service_tier,
        )
        return {"company_name": company_name, "pitch_text": pitch}

    def _tool_dispatch_outreach(self, client_name: str, channel: str, pitch_text: str, recipient: str = "", subject: str = "") -> dict[str, Any]:
        """Dispatches outreach pitch via configured outreach transport."""
        supported_channels = {"email", "linkedin"}
        clean_channel = channel.lower().strip()
        if clean_channel not in supported_channels:
            raise ValueError(
                f"Unsupported outreach channel '{channel}'. Supported channels: {sorted(supported_channels)}"
            )

        from core.outreach_dispatcher import dispatch_client_outreach

        res = dispatch_client_outreach(
            client_name=client_name,
            channel=clean_channel,
            pitch_text=pitch_text,
            recipient=recipient,
            subject=subject,
            is_sandbox=self.permission_controller.is_sandbox,
        )
        out_dict = res.to_dict()
        out_dict["dispatch_id"] = out_dict.get("message_id", "")
        if out_dict.get("status") == "delivered":
            out_dict["status"] = "dispatched"
        return out_dict

    def _tool_draft_proposal(
        self,
        client_name: str,
        service_tier: str,
        price_inr: float | None = None,
        sla_hours: float | None = None,
    ) -> dict[str, Any]:
        prop, requires_approval, reason = SalesEngine.prepare_proposal(
            lead_id=None,
            client_name=client_name,
            service_tier=service_tier,
            price_inr=price_inr,
            custom_sla_hours=sla_hours,
            is_sandbox=self.permission_controller.is_sandbox,
        )
        return {
            "proposal_id": prop["id"],
            "quoted_price_inr": prop.get("price_quote_inr", price_inr or 0.0),
            "requires_owner_approval": requires_approval,
            "approval_reason": reason,
            "draft_text": prop.get("proposal_text", ""),
            "status": "proposal_delivered",
        }

    def _tool_register_order_acceptance(
        self,
        client_name: str,
        proposal_id: int,
        acceptance_ref: str,
        input_csv_content: str,
    ) -> dict[str, Any]:
        """Registers formal customer acceptance of an issued proposal before scheduling work."""
        if not acceptance_ref or not acceptance_ref.strip():
            raise ValueError("Formal acceptance reference or client PO required.")
        if not input_csv_content or not input_csv_content.strip():
            raise ValueError("Customer input dataset required upon order acceptance.")

        is_sandbox = int(self.permission_controller.is_sandbox)
        with connect() as conn:
            prop = conn.execute(
                """
                SELECT p.*, o.title as opp_title, o.is_sandbox as opp_sandbox, o.amount_inr as opp_amount
                FROM proposals p
                JOIN opportunities o ON p.opportunity_id = o.id
                WHERE p.id = ?
                """,
                (proposal_id,),
            ).fetchone()
            if not prop:
                raise ValueError(f"Proposal #{proposal_id} not found.")

            if bool(prop["opp_sandbox"]) != bool(self.permission_controller.is_sandbox):
                raise ValueError(
                    f"Proposal #{proposal_id} belongs to a different environment "
                    f"({'sandbox' if prop['opp_sandbox'] else 'production'})."
                )

            # Invariant: Proposal must belong to this client!
            clean_client = client_name.strip().lower()
            prop_text = (prop["draft_text"] or "").lower()
            opp_title = (prop["opp_title"] or "").lower()
            if clean_client not in prop_text and clean_client not in opp_title:
                raise ValueError(
                    f"Customer acceptance rejected: Proposal #{proposal_id} does not belong to client '{client_name}'."
                )

            dataset_bytes = input_csv_content.encode("utf-8")
            dataset_sha256 = hashlib.sha256(dataset_bytes).hexdigest().upper()
            now = utc_now()

            agreed_price = float(prop["opp_amount"] or 0.0)
            service_tier = "data_analytics"
            for tier_spec in SERVICE_PROFIT_SPECS:
                if tier_spec.tier_id.lower() in prop_text:
                    service_tier = tier_spec.tier_id
                    break

            cursor = conn.execute(
                """
                INSERT INTO friday_accepted_orders (
                    client_name, proposal_id, opportunity_id, service_tier,
                    agreed_price_inr, acceptance_ref, dataset_sha256, dataset_bytes,
                    status, is_sandbox, accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                """,
                (
                    client_name,
                    proposal_id,
                    prop["opportunity_id"],
                    service_tier,
                    agreed_price,
                    acceptance_ref.strip(),
                    dataset_sha256,
                    len(dataset_bytes),
                    is_sandbox,
                    now,
                ),
            )
            order_id = cursor.lastrowid
            conn.commit()

        self.memory.update_client_session(
            client_name=client_name,
            stage="order_accepted",
            opportunity_id=prop["opportunity_id"],
            context_updates={
                "order_id": order_id,
                "proposal_id": proposal_id,
                "acceptance_ref": acceptance_ref,
                "order_accepted": True,
                "dataset_sha256": dataset_sha256,
                "service_tier": service_tier,
                "price_inr": agreed_price,
                "input_csv_content": input_csv_content,
            },
        )
        return {
            "status": "order_accepted",
            "order_id": order_id,
            "client_name": client_name,
            "proposal_id": proposal_id,
            "opportunity_id": prop["opportunity_id"],
            "acceptance_ref": acceptance_ref,
            "dataset_sha256": dataset_sha256,
            "service_tier": service_tier,
            "agreed_price_inr": agreed_price,
            "accepted_at": now,
        }

    def _tool_execute_workflow(
        self,
        client_name: str,
        service_tier: str,
        opportunity_id: int | None = None,
        input_csv_content: str = "",
    ) -> dict[str, Any]:
        # Strict invariant: Never execute fake customer work with sample data fallback
        if not input_csv_content or not input_csv_content.strip():
            raise ValueError(
                "Cannot execute delivery workflow: No customer dataset provided. "
                "Client must upload their real dataset via the secure portal before processing."
            )

        is_sandbox = int(self.permission_controller.is_sandbox)

        # Invariant: Verify customer order acceptance and dataset sha256
        with connect() as conn:
            order = conn.execute(
                """
                SELECT * FROM friday_accepted_orders
                WHERE client_name = ? AND is_sandbox = ? AND status IN ('accepted', 'in_delivery')
                ORDER BY id DESC LIMIT 1
                """,
                (client_name, is_sandbox),
            ).fetchone()

        if not order:
            raise ValueError(
                f"Cannot execute delivery workflow: No accepted customer order found for client '{client_name}'. "
                "Client must formally accept a proposal before work begins."
            )

        csv_bytes = input_csv_content.encode("utf-8")
        current_sha256 = hashlib.sha256(csv_bytes).hexdigest().upper()
        if not hmac.compare_digest(current_sha256, order["dataset_sha256"].upper()):
            raise ValueError(
                f"Dataset integrity verification failed: Input CSV SHA-256 ({current_sha256}) "
                f"does not match the accepted order dataset ({order['dataset_sha256']}). "
                "Direct execution with fabricated or mismatched data is prohibited."
            )

        target_opp_id = opportunity_id or order["opportunity_id"]
        if target_opp_id is not None:
            opp = get_opportunity(target_opp_id)
            if opp and opp.get("status") == "new":
                update_opportunity_status(target_opp_id, "approved_for_work")

        with connect() as conn:
            conn.execute(
                "UPDATE friday_accepted_orders SET status = 'in_delivery' WHERE id = ?",
                (order["id"],),
            )
            conn.commit()

        job = DeliveryEngine.convert_order_to_job(
            opportunity_id=target_opp_id,
            client_name=client_name,
            service_tier=service_tier,
            is_sandbox=self.permission_controller.is_sandbox,
        )

        completed_job, _ = DeliveryEngine.execute_delivery_workflow(
            job,
            file_bytes=csv_bytes,
            filename="input_data.csv",
        )

        with connect() as conn:
            conn.execute(
                "UPDATE friday_accepted_orders SET status = 'completed' WHERE id = ?",
                (order["id"],),
            )
            conn.commit()

        return asdict(completed_job)

    def _tool_audit_deliverables(self, task_id: int, max_revisions: int = 3) -> dict[str, Any]:
        t = get_task(task_id)
        if not t:
            raise ValueError(f"Task #{task_id} not found")
        job = DeliveryJob(
            task_id=task_id,
            opportunity_id=t.get("opportunity_id"),
            client_name=t.get("title", ""),
            service_tier=t.get("task_type", "").replace("autonomous_", ""),
            workspace=t.get("workspace", ""),
            status=t.get("status", ""),
        )
        qa_result = QualityEngine.inspect_and_verify(job, max_revisions=max_revisions)
        return asdict(qa_result)

    def _tool_generate_invoice(self, client_name: str, amount_inr: float, opportunity_id: int | None = None) -> dict[str, Any]:
        inv = FinanceEngine.create_job_invoice(
            opportunity_id=opportunity_id,
            client_name=client_name,
            amount_inr=amount_inr,
            is_sandbox=self.permission_controller.is_sandbox,
        )
        return {
            "invoice_id": inv["id"],
            "invoice_number": inv["invoice_number"],
            "amount_total": inv["amount_total"],
            "upi_uri": inv.get("upi_qr", {}).get("upi_uri", ""),
        }

    def _tool_release_deliverables(self, task_id: int, invoice_number: str, recipient_email: str = "") -> dict[str, Any]:
        t = get_task(task_id)
        if not t:
            raise ValueError(f"Task #{task_id} not found")

        is_sandbox = int(self.permission_controller.is_sandbox)

        # Invariant: Invoice verification matching task
        with connect() as conn:
            inv = conn.execute(
                "SELECT * FROM invoices WHERE invoice_number = ? AND is_sandbox = ?",
                (invoice_number, is_sandbox),
            ).fetchone()
            if not inv:
                raise ValueError(f"Invoice {invoice_number} not found in this environment")
            if inv["status"] != "paid" or float(inv["amount_paid"] or 0) < float(inv["amount_total"]):
                raise ValueError(f"Invoice {invoice_number} has not received verified cleared payment")
            if inv["task_id"] is not None and int(inv["task_id"]) != int(task_id):
                raise ValueError(
                    f"Invoice mismatch: Invoice {invoice_number} belongs to Task #{inv['task_id']}, "
                    f"not requested Task #{task_id}."
                )

        artifact_path = t.get("artifact_path", "")
        if not artifact_path or not Path(artifact_path).exists():
            raise ValueError(f"Deliverable package not found for task #{task_id}")

        # Check path containment within WORKSPACES_DIR
        real_artifact = Path(artifact_path).resolve()
        real_ws_root = Path(core.config.WORKSPACES_DIR).resolve()
        try:
            real_artifact.relative_to(real_ws_root)
        except ValueError:
            raise ValueError(f"Path traversal detected: Deliverable file {artifact_path} is outside allowed workspaces.")

        file_bytes = real_artifact.read_bytes()
        file_sha256 = hashlib.sha256(file_bytes).hexdigest().upper()
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(hours=24)).isoformat()

        raw_token = f"dlv_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

        client_name = t.get("title", "").replace("Autonomous delivery: ", "").strip() or inv["client_name"]

        with connect() as conn:
            conn.execute(
                """
                INSERT INTO friday_deliverable_tokens (
                    token, token_hash, task_id, opportunity_id, invoice_number,
                    client_name, artifact_path, file_sha256, file_size_bytes,
                    is_downloaded, download_count, max_downloads, expires_at,
                    created_at, is_sandbox
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1, ?, ?, ?)
                """,
                (
                    raw_token,
                    token_hash,
                    task_id,
                    inv["opportunity_id"],
                    invoice_number,
                    client_name,
                    str(real_artifact),
                    file_sha256,
                    len(file_bytes),
                    expires_at,
                    now,
                    is_sandbox,
                ),
            )
            conn.commit()

        return {
            "status": "released",
            "task_id": task_id,
            "invoice_number": invoice_number,
            "artifact_path": str(real_artifact),
            "file_sha256": file_sha256,
            "download_token": raw_token,
            "recipient_email": recipient_email or "",
            "transfer_method": "signed_portal_download",
            "expires_at": expires_at,
            "released_at": now,
        }


def download_client_deliverable(
    token: str,
    is_sandbox: bool = False,
    request_ip: str = "127.0.0.1",
) -> tuple[bytes, str, str]:
    """
    Validates deliverable download token, enforces expiration, atomic single-use,
    and returns (file_bytes, filename, file_sha256) while logging the delivery event.
    """
    if not token or not token.strip():
        raise ValueError("Download token required.")

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    clean_token = token.strip()

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            UPDATE friday_deliverable_tokens
            SET is_downloaded = 1, download_count = download_count + 1, downloaded_at = ?
            WHERE token = ? AND is_downloaded = 0 AND expires_at > ? AND is_sandbox = ?
            """,
            (now, clean_token, now, int(is_sandbox)),
        )
        if cur.rowcount == 0:
            rec = conn.execute(
                "SELECT * FROM friday_deliverable_tokens WHERE token = ? AND is_sandbox = ?",
                (clean_token, int(is_sandbox)),
            ).fetchone()
            conn.rollback()
            if not rec:
                raise ValueError("Invalid or tampered deliverable download token.")
            if rec["expires_at"] <= now:
                raise ValueError(f"Download token expired at {rec['expires_at']}.")
            if rec["is_downloaded"] or rec["download_count"] >= rec["max_downloads"]:
                raise ValueError("Download token has already been used. Deliverable tokens are single-use.")
            raise ValueError("Download token rejected.")

        rec = conn.execute(
            "SELECT * FROM friday_deliverable_tokens WHERE token = ? AND is_sandbox = ?",
            (clean_token, int(is_sandbox)),
        ).fetchone()

        # Invariant: Verify download token hash constant-time against database record
        expected_token_hash = hashlib.sha256(clean_token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(expected_token_hash, rec["token_hash"]):
            conn.rollback()
            raise ValueError("Invalid or tampered deliverable download token.")

        artifact_path = Path(rec["artifact_path"]).resolve()
        ws_root = Path(core.config.WORKSPACES_DIR).resolve()
        if not artifact_path.is_relative_to(ws_root):
            conn.rollback()
            raise ValueError("Access denied: Deliverable path outside workspaces directory (CWE-22).")
        if not artifact_path.exists():
            conn.rollback()
            raise ValueError(f"Deliverable package not found at {artifact_path}")

        file_bytes = artifact_path.read_bytes()
        calculated_sha = hashlib.sha256(file_bytes).hexdigest().upper()
        if not hmac.compare_digest(calculated_sha, rec["file_sha256"].upper()):
            conn.rollback()
            raise ValueError("Deliverable integrity check failed: SHA-256 mismatch.")

        conn.execute(
            """
            INSERT INTO friday_delivery_events (
                token, task_id, client_name, delivery_method, status,
                bytes_transferred, file_sha256, ip_or_recipient, delivered_at, is_sandbox
            ) VALUES (?, ?, ?, 'portal_download', 'success', ?, ?, ?, ?, ?)
            """,
            (
                clean_token,
                rec["task_id"],
                rec["client_name"],
                len(file_bytes),
                calculated_sha,
                request_ip,
                now,
                int(is_sandbox),
            ),
        )
        conn.commit()

    return file_bytes, artifact_path.name, calculated_sha


# ===========================================================================
# 4. Long-Term Persistent Memory & CRM Intelligence
# ===========================================================================

class FridayMemory:
    """Manages long-term knowledge, client histories, and context isolation."""

    def __init__(self, is_sandbox: bool = False):
        self.is_sandbox = is_sandbox
        init_friday_tables()

    def store_memory(self, entity_type: str, entity_id: str, key: str, value: Any, confidence: float = 1.0) -> None:
        now = utc_now()
        val_str = json.dumps(value) if not isinstance(value, str) else value
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO friday_memories (
                    entity_type, entity_id, memory_key, memory_value, confidence, is_sandbox, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_type, entity_id, memory_key, is_sandbox) DO UPDATE SET
                    memory_value = excluded.memory_value,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                (entity_type, entity_id, key, val_str, confidence, int(self.is_sandbox), now, now),
            )
            conn.commit()

    def retrieve_memory(self, entity_type: str, entity_id: str, key: str) -> Any | None:
        with connect() as conn:
            row = conn.execute(
                """SELECT memory_value FROM friday_memories 
                WHERE entity_type = ? AND entity_id = ? AND memory_key = ? 
                AND is_sandbox = ?""",
                (entity_type, entity_id, key, int(self.is_sandbox)),
            ).fetchone()
            if not row:
                return None
            val_str = row["memory_value"]
            try:
                return json.loads(val_str)
            except Exception:
                return val_str

    def get_client_session(self, client_name: str) -> dict[str, Any]:
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM friday_client_sessions WHERE client_name = ? AND is_sandbox = ?",
                (client_name, int(self.is_sandbox)),
            ).fetchone()
            if row:
                d = dict(row)
                d["context"] = json.loads(d.get("context_json") or "{}")
                return d
            return {}

    def update_client_session(
        self,
        client_name: str,
        stage: str,
        contact_channel: str = "linkedin",
        opportunity_id: int | None = None,
        task_id: int | None = None,
        invoice_number: str | None = None,
        context_updates: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        existing = self.get_client_session(client_name)
        ctx = existing.get("context", {}) if existing else {}
        if context_updates:
            ctx.update(context_updates)

        with connect() as conn:
            conn.execute(
                """
                INSERT INTO friday_client_sessions (
                    client_name, contact_channel, stage, opportunity_id, task_id, invoice_number,
                    last_interaction_at, context_json, is_sandbox, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_name, is_sandbox) DO UPDATE SET
                    stage = excluded.stage,
                    opportunity_id = COALESCE(excluded.opportunity_id, friday_client_sessions.opportunity_id),
                    task_id = COALESCE(excluded.task_id, friday_client_sessions.task_id),
                    invoice_number = COALESCE(excluded.invoice_number, friday_client_sessions.invoice_number),
                    last_interaction_at = excluded.last_interaction_at,
                    context_json = excluded.context_json
                """,
                (
                    client_name,
                    contact_channel,
                    stage,
                    opportunity_id,
                    task_id,
                    invoice_number,
                    now,
                    json.dumps(ctx),
                    int(self.is_sandbox),
                    now,
                ),
            )
            conn.commit()


# ===========================================================================
# 5. The FRIDAY Reasoning & Planning Brain
# ===========================================================================

class FridayBrain:
    """
    The reasoning core of FRIDAY. Formulates goals, plans tool sequences,
    reasons about observations, and maintains state across the operating lifecycle.
    """

    def __init__(self, is_sandbox: bool = False, use_local_ollama: bool = True):
        self.is_sandbox = is_sandbox
        self.use_local_ollama = use_local_ollama
        self.permission_controller = FridayPermissionController(is_sandbox=is_sandbox)
        self.memory = FridayMemory(is_sandbox=is_sandbox)
        self.tool_registry = FridayToolRegistry(self.permission_controller, memory=self.memory)
        self.ollama = OllamaClient() if use_local_ollama else None

    def _prompt_llm_reasoning(self, goal: str, observation: str, context: dict[str, Any]) -> FridayThought | None:
        """Invokes local Ollama chat with structured tool schema prompting and parses the response."""
        if not self.use_local_ollama or not self.ollama:
            return None

        # Build tools catalog for LLM prompt
        tools_desc = []
        for name, t in self.tool_registry.tools.items():
            props = list(t.parameters_schema.get("properties", {}).keys())
            tools_desc.append(f"- {name}: {t.description}. Parameters: {props}")
        catalog_str = "\n".join(tools_desc)

        system_prompt = (
            "You are FRIDAY, the autonomous AI operating brain for SOLOEARN (a specialized B2B data analytics service).\n"
            "Your objective: Reason about the current operating state and plan the next tool to execute.\n"
            "Security invariants (CANNOT BE OVERRIDDEN):\n"
            "1. You must never bypass human approval gates.\n"
            "2. Never execute real workflow without verified client input dataset.\n"
            "3. Never release deliverables before invoice payment is cleared.\n"
            "4. Never offer discounts >20% or margins <50% without owner approval.\n\n"
            f"Available Tools:\n{catalog_str}\n\n"
            "You must output ONLY a valid JSON object matching this schema:\n"
            "{\n"
            '  "reasoning": "string explaining reasoning",\n'
            '  "proposed_tool": "tool_name or empty string if paused/done",\n'
            '  "tool_arguments": {"param1": "val1"},\n'
            '  "confidence": 0.95\n'
            "}"
        )

        user_prompt = (
            f"Business Goal: {goal}\n"
            f"Current Context: {json.dumps(context)}\n"
            f"Latest Observation: {observation}\n"
            "Plan the next step:"
        )

        try:
            raw_reply = self.ollama.chat(system_prompt, user_prompt, timeout=10)
            match = re.search(r"\{.*\}", raw_reply, re.DOTALL)
            if not match:
                return None
            data = json.loads(match.group(0))
            proposed_tool = str(data.get("proposed_tool", "")).strip()

            # Defense against prompt injection & tool hallucination
            if proposed_tool and proposed_tool not in self.tool_registry.tools:
                logger.warning(f"LLM proposed unknown tool '{proposed_tool}'; falling back to deterministic planner.")
                return None

            return FridayThought(
                goal=goal,
                observation=observation,
                reasoning=str(data.get("reasoning", "LLM planned action")),
                proposed_tool=proposed_tool,
                tool_arguments=dict(data.get("tool_arguments", {})),
                confidence=float(data.get("confidence", 0.9)),
            )
        except Exception as exc:
            logger.debug(f"Ollama chat reasoning skipped or failed ({exc}); using deterministic fallback.")
            return None

    def plan_next_step(self, goal: str, observation: str, context: dict[str, Any]) -> FridayThought:
        """
        Reasons about current state and determines the next tool to execute.
        Attempts real local LLM reasoning first, with deterministic fallback.
        """
        # 1. Attempt real LLM reasoning if Ollama is enabled
        llm_thought = self._prompt_llm_reasoning(goal, observation, context)
        if llm_thought is not None:
            return llm_thought

        # 2. Deterministic state machine reasoning (reliable, bounded, & safe)
        client_name = str(context.get("client_name", ""))
        stage = str(context.get("stage", "discovery"))

        if stage == "discovery":
            return FridayThought(
                goal=goal,
                observation=observation,
                reasoning="Goal requires scouting business prospects; discovering active leads.",
                proposed_tool="discover_and_score_prospects",
                tool_arguments={"limit": 5},
            )

        if stage == "prospect_identified":
            tier = context.get("service_tier", "data_analytics")
            return FridayThought(
                goal=goal,
                observation=observation,
                reasoning=f"Lead identified for {client_name}; drafting tailored pitch for {tier}.",
                proposed_tool="prepare_client_pitch",
                tool_arguments={"company_name": client_name, "service_tier": tier},
            )

        if stage == "pitch_prepared":
            pitch = str(context.get("pitch_text", ""))
            channel = str(context.get("channel", "linkedin"))
            return FridayThought(
                goal=goal,
                observation=observation,
                reasoning="Pitch ready; submitting to permission controller for dispatch.",
                proposed_tool="dispatch_outreach_pitch",
                tool_arguments={"client_name": client_name, "channel": channel, "pitch_text": pitch},
            )

        if stage == "enquiry_received":
            tier = context.get("service_tier", "data_analytics")
            price = context.get("price_inr")
            sla = context.get("sla_hours", 48.0)
            return FridayThought(
                goal=goal,
                observation=observation,
                reasoning=f"Enquiry confirmed from {client_name}; drafting structured proposal under margin floor.",
                proposed_tool="draft_commercial_proposal",
                tool_arguments={"client_name": client_name, "service_tier": tier, "price_inr": price, "sla_hours": sla},
            )

        if stage == "proposal_delivered":
            if context.get("order_accepted"):
                stage = "order_accepted"
            else:
                return FridayThought(
                    goal=goal,
                    observation=observation,
                    reasoning=f"Proposal #{context.get('proposal_id')} delivered to {client_name}; awaiting formal customer acceptance and dataset upload.",
                    proposed_tool="",
                    tool_arguments={},
                )

        if stage == "order_accepted":
            tier = context.get("service_tier", "data_analytics")
            opp_id = context.get("opportunity_id")
            csv_in = context.get("input_csv_content", "")
            if not csv_in:
                return FridayThought(
                    goal=goal,
                    observation=observation,
                    reasoning=f"Order accepted by {client_name}; awaiting client dataset upload via secure portal.",
                    proposed_tool="",
                    tool_arguments={},
                )
            return FridayThought(
                goal=goal,
                observation=observation,
                reasoning=f"Order accepted with verified client dataset; executing {tier} delivery workflow in isolated workspace.",
                proposed_tool="execute_data_workflow",
                tool_arguments={"client_name": client_name, "service_tier": tier, "opportunity_id": opp_id, "input_csv_content": csv_in},
            )

        if stage == "workflow_completed":
            task_id = context.get("task_id", 1)
            return FridayThought(
                goal=goal,
                observation=observation,
                reasoning=f"Deliverables created for Task #{task_id}; running Independent QA Agent verification.",
                proposed_tool="audit_and_verify_deliverables",
                tool_arguments={"task_id": task_id, "max_revisions": 3},
            )

        if stage == "qa_passed":
            opp_id = context.get("opportunity_id")
            amount = float(context.get("price_inr", 5000.0))
            return FridayThought(
                goal=goal,
                observation=observation,
                reasoning=f"QA passed for {client_name}; generating Mode 01/04 statutory UPI invoice.",
                proposed_tool="generate_invoice_upi",
                tool_arguments={"client_name": client_name, "amount_inr": amount, "opportunity_id": opp_id},
            )

        if stage == "payment_cleared":
            task_id = context.get("task_id", 1)
            inv_num = context.get("invoice_number", "")
            return FridayThought(
                goal=goal,
                observation=observation,
                reasoning=f"Payment verified for invoice {inv_num}; releasing deliverable zip package to client.",
                proposed_tool="release_completed_deliverables",
                tool_arguments={"task_id": task_id, "invoice_number": inv_num},
            )

        return FridayThought(
            goal=goal,
            observation=observation,
            reasoning="Current state is stable; awaiting external client action or webhook event.",
            proposed_tool="",
            tool_arguments={},
        )

    def execute_reasoning_step(self, thought: FridayThought, cycle_id: str, step_num: int) -> ToolExecutionResult:
        """Executes a tool call through the permission controller and logs every action durably."""
        if not thought.proposed_tool:
            return ToolExecutionResult(tool_name="none", success=True, data={"status": "idle"})

        res = self.tool_registry.execute_tool(thought.proposed_tool, thought.tool_arguments)
        
        # Determine permission decision status
        decision = "approved_auto"
        if res.blocked_by_permission:
            decision = "escalated_owner" if "owner" in res.escalation_reason.lower() else "rejected_safety"

        # Log action durably in friday_action_logs with sensitive data redacted
        safe_args = redact_sensitive_dict(thought.tool_arguments)
        safe_result = redact_sensitive_dict(res.data if res.success else {"error": res.error_message, "escalation": res.escalation_reason})
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO friday_action_logs (
                    cycle_id, step_number, thought, tool_name, tool_args, tool_result,
                    permission_decision, is_sandbox, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_id,
                    step_num,
                    thought.reasoning,
                    thought.proposed_tool,
                    json.dumps(safe_args),
                    json.dumps(safe_result),
                    decision,
                    int(self.is_sandbox),
                    utc_now(),
                ),
            )
            conn.commit()

        return res

    def run_goal_cycle(self, goal: str = "drive_revenue", client_name: str = "") -> FridayCycleReport:
        """Runs an autonomous multi-step reasoning cycle toward a target business objective."""
        cycle_id = f"FRIDAY-{int(time.time() * 1000) % 1000000}"
        ts = utc_now()
        steps = 0
        tools_called: list[str] = []
        approved_count = 0
        escalated_count = 0
        errors_count = 0

        # Retrieve existing client session or initialize default discovery
        session = self.memory.get_client_session(client_name) if client_name else {}
        current_stage = session.get("stage", "discovery" if not client_name else "enquiry_received")
        context = {
            "client_name": client_name or "Apex Retailers Ltd",
            "stage": current_stage,
            "service_tier": session.get("context", {}).get("service_tier", "data_analytics"),
            "price_inr": session.get("context", {}).get("price_inr", 5000.0),
            "opportunity_id": session.get("opportunity_id"),
            "task_id": session.get("task_id"),
            "invoice_number": session.get("invoice_number"),
            "order_accepted": session.get("context", {}).get("order_accepted", False),
            "input_csv_content": session.get("context", {}).get("input_csv_content", ""),
        }

        observation = f"Initial cycle observation: stage={current_stage}"
        max_steps = 10

        while steps < max_steps:
            steps += 1
            thought = self.plan_next_step(goal, observation, context)
            if not thought.proposed_tool:
                break

            tools_called.append(thought.proposed_tool)
            res = self.execute_reasoning_step(thought, cycle_id, steps)

            if res.blocked_by_permission:
                escalated_count += 1
                observation = f"Tool {thought.proposed_tool} halted: {res.escalation_reason}"
                break
            elif not res.success:
                errors_count += 1
                observation = f"Tool {thought.proposed_tool} failed: {res.error_message}"
                break
            else:
                approved_count += 1
                observation = f"Tool {thought.proposed_tool} succeeded."

                # State progression in context
                if thought.proposed_tool == "discover_and_score_prospects":
                    prospects = res.data.get("prospects", [])
                    if prospects:
                        context["client_name"] = prospects[0]["company_name"]
                        context["service_tier"] = prospects[0]["service_tier"]
                    context["stage"] = "prospect_identified"

                elif thought.proposed_tool == "prepare_client_pitch":
                    context["pitch_text"] = res.data.get("pitch_text")
                    context["stage"] = "pitch_prepared"

                elif thought.proposed_tool == "dispatch_outreach_pitch":
                    context["stage"] = "enquiry_received"

                elif thought.proposed_tool == "draft_commercial_proposal":
                    context["proposal_id"] = res.data.get("proposal_id")
                    context["stage"] = "proposal_delivered"

                elif thought.proposed_tool == "register_customer_order_acceptance":
                    context["stage"] = "order_accepted"
                    context["order_accepted"] = True
                    context["input_csv_content"] = res.data.get("input_csv_content", context.get("input_csv_content", ""))

                elif thought.proposed_tool == "execute_data_workflow":
                    context["task_id"] = res.data.get("task_id")
                    context["opportunity_id"] = res.data.get("opportunity_id")
                    context["stage"] = "workflow_completed"

                elif thought.proposed_tool == "audit_and_verify_deliverables":
                    context["stage"] = "qa_passed" if res.data.get("passed") else "needs_manual_review"

                elif thought.proposed_tool == "generate_invoice_upi":
                    context["invoice_number"] = res.data.get("invoice_number")
                    # In real operation, payment arrives asynchronously; we pause here for webhook settlement
                    break

            # Persist updated client session after each step
            if context.get("client_name"):
                self.memory.update_client_session(
                    client_name=context["client_name"],
                    stage=context.get("stage", "discovery"),
                    opportunity_id=context.get("opportunity_id"),
                    task_id=context.get("task_id"),
                    invoice_number=context.get("invoice_number"),
                    context_updates={
                        "proposal_id": context.get("proposal_id"),
                        "order_accepted": context.get("order_accepted", False),
                        "service_tier": context.get("service_tier"),
                        "price_inr": context.get("price_inr"),
                        "input_csv_content": context.get("input_csv_content", ""),
                    },
                )

        if errors_count > 0:
            cycle_status = "failed"
        elif escalated_count > 0:
            cycle_status = "escalated"
        else:
            cycle_status = "completed"

        return FridayCycleReport(
            cycle_id=cycle_id,
            timestamp=ts,
            goal=goal,
            steps_executed=steps,
            tools_called=tools_called,
            actions_approved=approved_count,
            actions_escalated=escalated_count,
            cycle_status=cycle_status,
            summary_notes=f"Executed {steps} reasoning steps; {approved_count} approved, {escalated_count} escalated, {errors_count} failed.",
        )
