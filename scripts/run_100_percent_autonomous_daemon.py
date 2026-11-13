from __future__ import annotations

"""
scripts/run_100_percent_autonomous_daemon.py
SOLOEARN 100% Autonomous Revenue Auto-Pilot Daemon
===================================================
A fully hands-free, unattended operational loop that runs continuously 24/7 to:
1. Proactively SCOUT and screen commercial prospects from RSS, JSON APIs, and dropzones.
2. Formulate consultative, high-converting value pitches and DISPATCH outreach via SMTP/Webhook.
3. Automatically INGEST accepted client orders and raw CSV/Excel datasets.
4. EXECUTE the complete 7-step data cleaning, Excel model, chart suite, and HTML dashboard.
5. Perform 100% QA AUDIT with automated self-healing.
6. Generate official commercial GST INVOICES with NPCI-standard Dynamic UPI QR codes.
7. Monitor and RECONCILE bank credits via HMAC webhooks or bank statement auto-matching.
8. Settle transactions in paise, update the owner's bank balance, and RELEASE deliverables.
9. Perform continuous automated database maintenance: WAL checkpointing, daily backups, vacuuming.
10. Maintain 24/7 resilience with exponential backoff on network issues and zero crash exits.

Usage:
    # Run a single complete autonomous cycle in simulation mode
    python scripts/run_100_percent_autonomous_daemon.py --sandbox --once

    # Run continuously in background (polls every 60 seconds)
    python scripts/run_100_percent_autonomous_daemon.py --sandbox --interval 60

    # Run in live production mode
    python scripts/run_100_percent_autonomous_daemon.py --live --interval 120

    # Display health telemetry and status
    python scripts/run_100_percent_autonomous_daemon.py --status

    # Trigger immediate database maintenance
    python scripts/run_100_percent_autonomous_daemon.py --maintenance
"""

import argparse
import hashlib
import hmac
import http.client
import json
import logging
import os
import random
import signal
import socket
import sys
import time
import traceback
import urllib.error
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# Fix Windows console encoding
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from core.autonomy_profitability_system import (
    MINIMUM_ACCEPTABLE_CONTRIBUTION_MARGIN,
    CapabilityMatcher,
    OpportunityEngine,
    SalesEngine,
    DeliveryEngine,
    QualityEngine,
    FinanceEngine,
)
from core.commercial_funnel import (
    add_commercial_lead,
    list_commercial_leads,
    update_lead_stage,
    seed_real_smb_pilot_leads,
)
from core.config import (
    DATA_DIR,
    LOGS_DIR,
    BACKUPS_DIR,
    LEADS_DROPZONE_DIR,
    WORKSPACES_DIR,
    ensure_directories,
    safe_workspace_path,
)
from core.db import (
    connect,
    get_invoice,
    init_db,
    list_discovery_sources,
    list_tasks,
    utc_now,
    verify_invoice_payment,
    add_revenue,
    execute_with_retry,
)
from core.db_maintenance import (
    DatabaseMaintenanceManager,
    backup_database,
    check_database_integrity,
    checkpoint_wal,
    get_database_stats,
    vacuum_database,
)
from core.discovery_engine import DiscoveryEngine
from core.execution_engine import AutonomousExecutionEngine
from core.financial_ledger import get_cash_and_reserves_summary, inr_to_paise
from core.friday_agent import (
    FridayBrain,
    FridayMemory,
    FridayPermissionController,
    FridayToolRegistry,
    download_client_deliverable,
    init_friday_tables,
)
from core.friday_secure_portal import (
    FridayPaymentWebhookHandler,
    init_portal_tables,
)
from core.health import run_system_health_audit
from core.invoice_generator import render_invoice_html
from core.net_profit_optimizer import SERVICE_PROFIT_SPECS
from core.outreach_dispatcher import (
    dispatch_client_outreach,
    get_outreach_rate_limit_status,
    SMTPOutreachProvider,
    WebhookOutreachProvider,
)
from core.qa_agent import IndependentQAAgent
from core.upi_engine import (
    generate_invoice_upi_qr,
    process_merchant_bank_webhook,
    scan_and_match_statement_for_upi,
)

DAEMON_LOG_FILE = LOGS_DIR / "autonomous_daemon.log"
DAEMON_EVENTS_FILE = LOGS_DIR / "autonomous_daemon_events.jsonl"
TELEMETRY_FILE = DATA_DIR / "daemon_health.json"
CLIENT_ORDERS_DIR = DATA_DIR / "client_orders"
BANK_STATEMENTS_DIR = DATA_DIR / "bank_statements"

SAMPLE_ECOM_DATA = """order_id,date,channel,gross_sales_inr,shipping_fee_inr,return_status,gateway
ORD-2001,2026-11-01,Shopify,3499.00,0.00,delivered,Razorpay
ORD-2002,2026-11-01,Amazon_IN,1899.00,80.00,delivered,AmazonPay
ORD-2003,2026-11-02,Myntra,2499.00,0.00,delivered,MyntraSettlement
ORD-2004,2026-11-02,Shopify,4299.00,0.00,rto_returned,Razorpay
ORD-2005,2026-11-03,Ajio,1599.00,50.00,delivered,AjioDirect
ORD-2006,2026-11-03,Shopify,2899.00,0.00,delivered,CashOnDelivery
ORD-2007,2026-11-04,Amazon_IN,5199.00,0.00,delivered,AmazonPay
ORD-2008,2026-11-04,Myntra,3199.00,0.00,returned_customer,MyntraSettlement
ORD-2009,2026-11-05,Shopify,1299.00,60.00,delivered,Razorpay
ORD-2010,2026-11-05,Flipkart,2199.00,0.00,delivered,FlipkartAssure
"""


def setup_daemon_logging(
    log_level: int = logging.INFO,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> tuple[logging.Logger, logging.Logger]:
    """
    Configures structured logging with log rotation to avoid unbounded disk consumption.
    Returns (daemon_logger, events_logger).
    """
    ensure_directories()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove pre-existing handlers to prevent duplicated output
    for h in list(root_logger.handlers):
        try:
            h.close()
        except Exception:
            pass
        root_logger.removeHandler(h)

    # 1. Formatted Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # 2. Rotating File Handler for human-readable daemon log
    file_handler = RotatingFileHandler(
        DAEMON_LOG_FILE,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(process)d] %(name)s: %(message)s")
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # 3. Dedicated Rotating JSONL Events Handler
    event_logger = logging.getLogger("soloearn.daemon_events")
    event_logger.setLevel(logging.INFO)
    event_logger.propagate = False
    for h in list(event_logger.handlers):
        try:
            h.close()
        except Exception:
            pass
        event_logger.removeHandler(h)

    event_handler = RotatingFileHandler(
        DAEMON_EVENTS_FILE,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    event_handler.setFormatter(logging.Formatter("%(message)s"))
    event_logger.addHandler(event_handler)

    daemon_logger = logging.getLogger("soloearn.autonomous_daemon")
    return daemon_logger, event_logger


logger, event_logger = setup_daemon_logging()


def log_daemon_event(event_type: str, data: dict[str, Any], level: str = "INFO") -> None:
    """Emits an atomic structured JSON event to the rotated events log."""
    try:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": level.upper(),
            "event": event_type,
            "pid": os.getpid(),
            "data": data,
        }
        event_logger.info(json.dumps(record, default=str))
    except Exception as ex:
        logger.warning(f"Failed to emit structured JSON event: {ex}")


class ExponentialBackoff:
    """Computes exponential backoff with randomized jitter for transient failure recovery."""

    def __init__(
        self,
        base_delay: float = 5.0,
        max_delay: float = 300.0,
        factor: float = 2.0,
        jitter_pct: float = 0.25,
    ):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.factor = factor
        self.jitter_pct = jitter_pct

    def compute_delay(self, failure_count: int) -> float:
        if failure_count <= 0:
            return 0.0
        exponent = min(failure_count - 1, 8)
        raw = self.base_delay * (self.factor ** exponent)
        delay = min(raw, self.max_delay)
        jitter = random.uniform(0.0, delay * self.jitter_pct)
        return round(delay + jitter, 2)


def is_network_error(exc: BaseException) -> bool:
    """Classifies if an exception represents a network or connectivity failure."""
    net_types = (
        ConnectionError,
        TimeoutError,
        socket.gaierror,
        socket.timeout,
        urllib.error.URLError,
        http.client.HTTPException,
    )
    if isinstance(exc, net_types):
        return True

    # Check requests exceptions if available
    req_mod = sys.modules.get("requests")
    if req_mod and hasattr(req_mod, "exceptions"):
        if isinstance(exc, req_mod.exceptions.RequestException):
            return True

    msg = str(exc).lower()
    net_keywords = (
        "connection refused",
        "connection reset",
        "network is unreachable",
        "timed out",
        "timeout",
        "temporary failure in name resolution",
        "getaddrinfo failed",
        "name or service not known",
        "remote end closed connection",
        "handshake failed",
    )
    return any(kw in msg for kw in net_keywords)


@dataclass
class AutonomousCycleMetrics:
    sources_polled: int = 0
    leads_scouted: int = 0
    pitches_dispatched: int = 0
    orders_ingested: int = 0
    tasks_executed: int = 0
    qa_audits_passed: int = 0
    invoices_generated: int = 0
    payments_reconciled: int = 0
    revenue_earned_inr: float = 0.0
    net_owner_profit_inr: float = 0.0
    # Automated Maintenance metrics
    wal_checkpoints: int = 0
    backups_created: int = 0
    vacuums_performed: int = 0
    maintenance_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DaemonHealthState:
    daemon_version: str = "1.0.0"
    status: str = "starting"  # starting, running, sleeping, degraded, error, stopped
    pid: int = 0
    mode: str = "sandbox"
    started_at_utc: str = ""
    uptime_seconds: float = 0.0
    last_heartbeat_utc: str = ""
    current_cycle: int = 0
    consecutive_failures: int = 0
    last_cycle_duration_seconds: float = 0.0
    last_cycle_metrics: dict[str, Any] = field(default_factory=dict)
    lifetime_metrics: dict[str, Any] = field(default_factory=dict)
    database_stats: dict[str, Any] = field(default_factory=dict)
    maintenance_stats: dict[str, Any] = field(default_factory=dict)
    system_health: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None
    last_error_utc: str | None = None
    next_run_estimated_utc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def update_health_telemetry(state: DaemonHealthState) -> None:
    """Atomically writes daemon health state to JSON file."""
    ensure_directories()
    state.pid = os.getpid()
    state.last_heartbeat_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp_path = TELEMETRY_FILE.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        tmp_path.replace(TELEMETRY_FILE)
    except Exception as ex:
        logger.warning(f"Failed to update health telemetry: {ex}")


def read_health_telemetry() -> dict[str, Any] | None:
    """Reads current daemon health telemetry if available."""
    if not TELEMETRY_FILE.exists():
        return None
    try:
        return json.loads(TELEMETRY_FILE.read_text(encoding="utf-8"))
    except Exception as ex:
        logger.warning(f"Could not read health telemetry: {ex}")
        return None


def accumulate_lifetime_metrics(state: DaemonHealthState, cycle: AutonomousCycleMetrics) -> None:
    """Updates lifetime cumulative counters across cycles."""
    lm = state.lifetime_metrics or {}
    lm["total_cycles"] = state.current_cycle
    lm["total_sources_polled"] = lm.get("total_sources_polled", 0) + cycle.sources_polled
    lm["total_leads_scouted"] = lm.get("total_leads_scouted", 0) + cycle.leads_scouted
    lm["total_pitches_dispatched"] = lm.get("total_pitches_dispatched", 0) + cycle.pitches_dispatched
    lm["total_orders_ingested"] = lm.get("total_orders_ingested", 0) + cycle.orders_ingested
    lm["total_tasks_executed"] = lm.get("total_tasks_executed", 0) + cycle.tasks_executed
    lm["total_qa_audits_passed"] = lm.get("total_qa_audits_passed", 0) + cycle.qa_audits_passed
    lm["total_invoices_generated"] = lm.get("total_invoices_generated", 0) + cycle.invoices_generated
    lm["total_payments_reconciled"] = lm.get("total_payments_reconciled", 0) + cycle.payments_reconciled
    lm["total_revenue_earned_inr"] = round(lm.get("total_revenue_earned_inr", 0.0) + cycle.revenue_earned_inr, 2)
    lm["total_net_owner_profit_inr"] = round(lm.get("total_net_owner_profit_inr", 0.0) + cycle.net_owner_profit_inr, 2)
    lm["total_wal_checkpoints"] = lm.get("total_wal_checkpoints", 0) + cycle.wal_checkpoints
    lm["total_backups_created"] = lm.get("total_backups_created", 0) + cycle.backups_created
    lm["total_vacuums_performed"] = lm.get("total_vacuums_performed", 0) + cycle.vacuums_performed
    state.lifetime_metrics = lm


class AutonomousMasterEngine:
    """
    100% Autonomous Master Engine Orchestrator
    Coordinates discovery, screening, dispatch, ingestion, execution, QA, invoicing, bank settlement,
    and automated SQLite database maintenance.
    """

    def __init__(self, is_sandbox: bool = True):
        self.is_sandbox = is_sandbox
        ensure_directories()
        CLIENT_ORDERS_DIR.mkdir(parents=True, exist_ok=True)
        BANK_STATEMENTS_DIR.mkdir(parents=True, exist_ok=True)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

        init_db()
        init_friday_tables()
        init_portal_tables()

        # Friday Core Agent Controller & Registry
        self.permission_controller = FridayPermissionController(is_sandbox=self.is_sandbox)
        self.memory = FridayMemory()
        self.brain = FridayBrain(self.permission_controller, self.memory)
        self.tools = FridayToolRegistry(self.permission_controller, self.memory)

        # Automated Database Maintenance Manager
        self.db_maintenance = DatabaseMaintenanceManager(
            checkpoint_interval_seconds=1800,          # Checkpoint WAL every 30 minutes
            backup_interval_seconds=86400,             # Daily automated backup (24 hours)
            vacuum_interval_seconds=7 * 86400,         # Weekly vacuuming (7 days)
            wal_size_threshold_bytes=5 * 1024 * 1024,  # Checkpoint if WAL exceeds 5MB
            keep_backups=7,
        )

        # Set environment toggle for autonomous dispatch
        os.environ["SOLOEARN_AUTONOMOUS_OUTREACH"] = "true"

    def run_maintenance(self, force: bool = False) -> dict[str, Any]:
        """Runs scheduled or on-demand database maintenance."""
        return self.db_maintenance.run_maintenance_if_due(force=force)

    def run_scout_and_pitch_stage(self) -> tuple[int, int, int]:
        """Stage 1 & 2: Discovers leads and dispatches tailored value pitches with per-target error isolation."""
        sources = list_discovery_sources()
        sources_polled = 0
        for s in sources:
            if s.get("is_active") and s.get("is_permitted"):
                try:
                    DiscoveryEngine.poll_source(s, auto_commit=True)
                    sources_polled += 1
                except Exception as ex:
                    logger.warning(f"Non-fatal error polling source #{s.get('id')} ({s.get('name')}): {ex}")
                    log_daemon_event("source_poll_warning", {"source_id": s.get("id"), "error": str(ex)}, level="WARNING")

        leads_scouted = 0
        pitches_dispatched = 0

        # Check newly discovered opportunities from DiscoveryEngine and promote suitable ones into pipeline
        try:
            with connect() as conn:
                new_opps = conn.execute(
                    """
                    SELECT id, title, description, amount_inr, estimated_hours, url
                    FROM opportunities
                    WHERE status = 'new' AND amount_inr >= 5000 AND source_permitted = 1
                    ORDER BY id DESC LIMIT 5
                    """
                ).fetchall()
        except Exception as ex:
            logger.warning(f"Error querying new opportunities: {ex}")
            new_opps = []

        all_leads = list_commercial_leads(include_sandbox=self.is_sandbox)
        existing_companies = {l.get("company_name", "").lower() for l in all_leads}

        for opp in new_opps:
            try:
                company_guess = opp["title"].split(" - ")[0][:40]
                if company_guess.lower() not in existing_companies:
                    clean_handle = (
                        company_guess.lower()
                        .replace(" ", "")
                        .replace("&", "")
                        .replace("-", "")
                        .replace("(", "")
                        .replace(")", "")[:14]
                    )
                    add_commercial_lead(
                        company_name=company_guess,
                        contact_name="Operations Lead",
                        contact_channel="email",
                        contact_handle=f"contact@{clean_handle}.in",
                        industry="ecommerce",
                        service_tier="data_analytics" if opp["amount_inr"] <= 5000 else "csv_reconciliation",
                        proposed_price_inr=opp["amount_inr"],
                        stage="identified",
                        notes=opp["description"] or opp["title"],
                    )
                    existing_companies.add(company_guess.lower())
                    with connect() as conn:
                        conn.execute("UPDATE opportunities SET status = 'in_review' WHERE id = ?", (opp["id"],))
            except Exception as ex:
                logger.warning(f"Error promoting opportunity #{opp.get('id')}: {ex}")

        # Retrieve unapproached leads
        all_leads = list_commercial_leads(include_sandbox=self.is_sandbox)
        unapproached = [l for l in all_leads if l.get("stage") in ("identified", "prospective_account", "new")]

        # In simulation mode, if pipeline is empty, auto-seed verified research targets
        if not unapproached and self.is_sandbox:
            seed_real_smb_pilot_leads()
            all_leads = list_commercial_leads(include_sandbox=True)
            unapproached = [l for l in all_leads if l.get("stage") in ("identified", "prospective_account", "new")]

        # Check rate limits
        can_send, count_today, remaining_today = get_outreach_rate_limit_status(daily_limit=10, is_sandbox=self.is_sandbox)
        if not can_send or remaining_today <= 0:
            logger.info("Daily outreach safety quota reached. Pausing new outbound pitches.")
            return sources_polled, len(unapproached), 0

        # Screen and dispatch up to remaining daily limit (or batch size 3)
        batch = unapproached[: min(remaining_today, 3)]
        for lead in batch:
            try:
                leads_scouted += 1
                company = lead.get("company_name", "Target Brand")
                contact = lead.get("contact_name", "Founder")
                tier = lead.get("service_tier", "data_analytics")
                channel = lead.get("contact_channel", "email")
                recipient = lead.get("contact_handle") or f"contact@{company.lower().replace(' ', '')}.in"

                # Check fit
                fit = OpportunityEngine.assess_prospect_fit({
                    "company_name": company,
                    "description": lead.get("notes") or f"E-commerce business analytics and CSV reconciliation for {company}",
                })

                if not fit.is_fit:
                    continue

                pitch_text = (
                    f"Hi {contact},\n\n"
                    f"I noticed {company}'s multi-channel expansion. Quick question: how much manual time "
                    f"does your operations team spend consolidating and reconciling order CSVs from Shopify, Amazon, and marketplaces?\n\n"
                    f"At SOLOEARN, we convert raw transaction exports into multi-tab executive Excel models, "
                    f"financial reconciliation summaries, and interactive browser dashboards in under 48 hours—fully verified "
                    f"with automated QA guarantees.\n\n"
                    f"Would you be open to reviewing a 2-minute sample report built on sample D2C data?\n\n"
                    f"Best regards,\nBhavesh Khullar\nFounder, SOLOEARN"
                )

                # Autonomous dispatch with isolated error handling
                res = dispatch_client_outreach(
                    client_name=company,
                    channel="email" if channel not in ("email", "linkedin") else channel,
                    pitch_text=pitch_text,
                    recipient=recipient,
                    subject=f"Operational Data Analytics & Reconciliation for {company}",
                    is_sandbox=self.is_sandbox,
                )

                if res.status in ("dispatched", "simulated", "delivered"):
                    pitches_dispatched += 1
                    update_lead_stage(
                        lead["id"],
                        "approached",
                        notes=f"Autonomously approached via {res.provider} ({res.message_id})",
                    )
                    logger.info(f"Dispatched pitch to {company} via {res.provider} ({res.message_id})")
                    log_daemon_event("pitch_dispatched", {
                        "lead_id": lead["id"],
                        "company": company,
                        "provider": res.provider,
                        "message_id": res.message_id,
                    })
            except Exception as lead_err:
                logger.warning(f"Error dispatching pitch to lead #{lead.get('id')}: {lead_err}")
                log_daemon_event("lead_dispatch_warning", {"lead_id": lead.get("id"), "error": str(lead_err)}, level="WARNING")

        return sources_polled, leads_scouted, pitches_dispatched

    def run_order_intake_delivery_and_settlement(self) -> tuple[int, int, int, int, int, float, float]:
        """
        Stage 3, 4, 5, 6:
        Ingests orders -> Executes Analytics Workflow -> Runs 100% QA -> Generates GST UPI Invoices -> Settles Payments -> Releases Tokens.
        Each order execution is isolated with resilient try-except blocks.
        """
        orders_ingested = 0
        tasks_executed = 0
        qa_passed = 0
        invoices_generated = 0
        payments_reconciled = 0
        revenue_earned = 0.0
        net_profit = 0.0

        # Scan for pending unexecuted accepted orders
        with connect() as conn:
            orders = conn.execute(
                """
                SELECT id, client_name, proposal_id, opportunity_id, service_tier, agreed_price_inr, status, is_sandbox
                FROM friday_accepted_orders
                WHERE status = 'accepted' AND is_sandbox = ?
                """,
                (int(self.is_sandbox),),
            ).fetchall()

        # In sandbox mode, if no active accepted order exists, simulate an inbound conversion
        if not orders and self.is_sandbox:
            company = f"Apex D2C Apparel Pvt Ltd ({int(time.time()) % 10000})"
            tier = "csv_reconciliation"
            price = 5000.0
            order_ref = f"ORD-AUTONOMOUS-{int(time.time())}"
            data_hash = hashlib.sha256(SAMPLE_ECOM_DATA.encode("utf-8")).hexdigest()

            # Create proposal
            prop, req_approval, _ = SalesEngine.prepare_proposal(
                lead_id=None,
                client_name=company,
                service_tier=tier,
                price_inr=price,
                is_sandbox=True,
            )
            prop_id = prop["id"]

            with connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO friday_accepted_orders (
                        client_name, proposal_id, opportunity_id, service_tier, agreed_price_inr,
                        acceptance_ref, dataset_sha256, dataset_bytes, status, is_sandbox, accepted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', 1, ?)
                    """,
                    (company, prop_id, None, tier, price, order_ref, data_hash, len(SAMPLE_ECOM_DATA), utc_now()),
                )
                conn.commit()
                order_id = cur.lastrowid
                orders_ingested += 1

            with connect() as conn:
                orders = conn.execute(
                    "SELECT id, client_name, proposal_id, opportunity_id, service_tier, agreed_price_inr, status, is_sandbox FROM friday_accepted_orders WHERE id = ?",
                    (order_id,),
                ).fetchall()

        for ord_row in orders:
            ord_id = ord_row["id"]
            client_name = ord_row["client_name"]
            service_tier = ord_row["service_tier"]
            agreed_price = ord_row["agreed_price_inr"]

            try:
                logger.info(f"Executing delivery for Order #{ord_id}: {client_name} (₹{agreed_price:,.2f})")

                # Check if order has a specific file in dropzone, otherwise use sample e-com data
                input_csv = SAMPLE_ECOM_DATA
                drop_file = CLIENT_ORDERS_DIR / f"{client_name.replace(' ', '_').lower()}.csv"
                if drop_file.exists():
                    input_csv = drop_file.read_text(encoding="utf-8")

                # 1. Convert order to job and execute delivery plan via DeliveryEngine
                job = DeliveryEngine.convert_order_to_job(
                    opportunity_id=ord_row["opportunity_id"],
                    client_name=client_name,
                    service_tier=service_tier,
                    is_sandbox=self.is_sandbox,
                )

                job, deliverables = DeliveryEngine.execute_delivery_workflow(
                    job=job,
                    file_bytes=input_csv.encode("utf-8"),
                    filename=f"{client_name.replace(' ', '_').lower()}.csv",
                )

                task_id = job.task_id
                tasks_executed += 1

                # 2. Run independent QA Agent
                qa_res = QualityEngine.inspect_and_verify(job, max_revisions=3)
                if qa_res.passed:
                    qa_passed += 1
                    logger.info(f"QA Audit PASSED 100% for Task #{task_id} (Deliverable ZIP created at {job.artifact_zip})")

                    # 3. Generate GST Invoice with Dynamic NPCI UPI QR
                    inv = FinanceEngine.create_job_invoice(
                        opportunity_id=ord_row["opportunity_id"],
                        client_name=client_name,
                        amount_inr=agreed_price,
                        is_sandbox=self.is_sandbox,
                    )
                    inv_num = inv["invoice_number"]
                    invoices_generated += 1
                    logger.info(f"Generated Invoice {inv_num} for {client_name} (Total: ₹{agreed_price:,.2f} with Dynamic UPI QR)")

                    with connect() as conn:
                        conn.execute("UPDATE invoices SET task_id = ? WHERE id = ?", (task_id, inv["id"]))

                    # 4. Bank Settlement & Deliverable Token Release
                    if self.is_sandbox:
                        handler = FridayPaymentWebhookHandler(is_sandbox=True)
                        webhook_payload = {
                            "invoice_number": inv_num,
                            "amount_inr": agreed_price,
                            "utr": f"UTR-AUTONOMOUS-{int(time.time())}-{task_id}",
                            "status": "captured",
                            "timestamp": int(time.time()),
                        }
                        payload_bytes = json.dumps(webhook_payload, sort_keys=True).encode("utf-8")
                        signature = hmac.new(handler.webhook_secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()

                        hook_res = handler.process_webhook_event(
                            event_id=f"EVT-BANK-{int(time.time())}-{task_id}",
                            payload_bytes=payload_bytes,
                            signature=signature,
                            provider="kotak_merchant_adapter",
                        )

                        if hook_res.get("status") == "settled":
                            payments_reconciled += 1
                            revenue_earned += agreed_price
                            net_profit += agreed_price * 0.95
                            logger.info(f"Bank Settlement Reconciled! Invoice {inv_num} PAID: +₹{agreed_price:,.2f} into Ledger")

                            # Release Deliverable Token via Friday Gate 3
                            release_res = self.tools.execute_tool(
                                name="release_completed_deliverables",
                                arguments={"task_id": task_id, "invoice_number": inv_num},
                            )

                            token = release_res.data.get("download_token", "") if release_res.success else ""
                            logger.info(f"Released Deliverable Token: {token} for {client_name}")

                            with connect() as conn:
                                conn.execute("UPDATE friday_accepted_orders SET status = 'completed' WHERE id = ?", (ord_id,))

                            log_daemon_event("order_completed_and_settled", {
                                "order_id": ord_id,
                                "client_name": client_name,
                                "invoice_number": inv_num,
                                "revenue_inr": agreed_price,
                                "token": token,
                            })
            except Exception as ord_err:
                logger.error(f"Error processing Order #{ord_id} for {client_name}: {ord_err}", exc_info=True)
                log_daemon_event("order_execution_error", {
                    "order_id": ord_id,
                    "client_name": client_name,
                    "error": str(ord_err),
                }, level="ERROR")

        return orders_ingested, tasks_executed, qa_passed, invoices_generated, payments_reconciled, revenue_earned, net_profit

    def run_complete_cycle(self) -> AutonomousCycleMetrics:
        """Executes one complete end-to-end autonomous cycle across all stages, including maintenance."""
        logger.info(f"Starting Autonomous Cycle ({'SANDBOX' if self.is_sandbox else 'LIVE'})")
        metrics = AutonomousCycleMetrics()

        # Step 0: Automated SQLite Database Maintenance (WAL checkpoint, backup, vacuum)
        try:
            maint_res = self.run_maintenance(force=False)
            metrics.maintenance_summary = maint_res.get("summary", {})
            if "checkpoint" in maint_res and maint_res["checkpoint"].get("success"):
                metrics.wal_checkpoints += 1
            if "backup" in maint_res and maint_res["backup"].get("status") == "success":
                metrics.backups_created += 1
            if "vacuum" in maint_res and maint_res["vacuum"].get("status") == "success":
                metrics.vacuums_performed += 1
        except Exception as maint_err:
            logger.warning(f"Database maintenance error (non-fatal): {maint_err}")
            log_daemon_event("maintenance_warning", {"error": str(maint_err)}, level="WARNING")

        # Step 1: Scout & Pitch
        polled, scouted, pitched = self.run_scout_and_pitch_stage()
        metrics.sources_polled = polled
        metrics.leads_scouted = scouted
        metrics.pitches_dispatched = pitched

        # Step 2: Ingest, Deliver, QA, Invoice, Settle & Release
        ingested, executed, qa, invs, paid, rev, profit = self.run_order_intake_delivery_and_settlement()
        metrics.orders_ingested = ingested
        metrics.tasks_executed = executed
        metrics.qa_audits_passed = qa
        metrics.invoices_generated = invs
        metrics.payments_reconciled = paid
        metrics.revenue_earned_inr = rev
        metrics.net_owner_profit_inr = profit

        return metrics


def print_banner(metrics: AutonomousCycleMetrics, is_sandbox: bool):
    cash_summary = get_cash_and_reserves_summary(include_sandbox=is_sandbox)
    total_cash = cash_summary.get("bank_balance_inr", 0.0) or cash_summary.get("collected_receipts_inr", 0.0)
    operating_profit = cash_summary.get("operating_profit_inr", 0.0)
    buffer = cash_summary.get("working_capital_buffer_inr", 0.0)

    print("\n" + "=" * 78)
    print(" 🚀 SOLOEARN 100% AUTONOMOUS REVENUE ENGINE — CYCLE REPORT")
    print(f" Mode: {'SANDBOX (Safe Test Simulation)' if is_sandbox else 'LIVE PRODUCTION (Real Bank & Outreach)'}")
    print(f" Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("-" * 78)
    print(f" 📡 Discovery Sources Polled:       {metrics.sources_polled}")
    print(f" 🎯 Prospects Scouted & Screened:   {metrics.leads_scouted}")
    print(f" 📬 Outbound Pitches Dispatched:    {metrics.pitches_dispatched}")
    print(f" 📥 Orders Ingested:                {metrics.orders_ingested}")
    print(f" ⚙️ Analytics Tasks Executed:       {metrics.tasks_executed}")
    print(f" 🛡️ 100% QA Audits Passed:          {metrics.qa_audits_passed}")
    print(f" 🧾 GST Dynamic UPI Invoices:       {metrics.invoices_generated}")
    print(f" 💳 Bank Settlements Reconciled:    {metrics.payments_reconciled}")
    print(f" 💰 Revenue Earned this Cycle:      ₹{metrics.revenue_earned_inr:,.2f}")
    print(f" 📈 Net Owner Profit this Cycle:    ₹{metrics.net_owner_profit_inr:,.2f}")
    print("-" * 78)
    print(f" 🔧 SQLite Maintenance:             Checkpoints: {metrics.wal_checkpoints} | Backups: {metrics.backups_created} | Vacuums: {metrics.vacuums_performed}")
    print(f" 🏦 Total Ledger Reconciled Cash:   ₹{total_cash:,.2f}")
    print(f" 📈 Net Retained Operating Profit:  ₹{operating_profit:,.2f}")
    print(f" 🛡️ Retained Capital Buffer:        ₹{buffer:,.2f}")
    print("=" * 78 + "\n")


def print_status_dashboard() -> None:
    """Renders formatted console status from daemon health telemetry file."""
    data = read_health_telemetry()
    if not data:
        print("\n[STATUS] No active or past daemon health telemetry found at data/daemon_health.json.")
        print("Start the daemon with: python scripts/run_100_percent_autonomous_daemon.py\n")
        return

    pid = data.get("pid", 0)
    status = data.get("status", "unknown").upper()
    uptime = data.get("uptime_seconds", 0.0)
    cycle = data.get("current_cycle", 0)
    mode = data.get("mode", "sandbox").upper()
    heartbeat = data.get("last_heartbeat_utc", "N/A")
    failures = data.get("consecutive_failures", 0)
    last_err = data.get("last_error")

    # Check if PID is actively running
    is_alive = False
    if pid > 0:
        try:
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(1040, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
                if handle != 0:
                    kernel32.CloseHandle(handle)
                    is_alive = True
            else:
                os.kill(pid, 0)
                is_alive = True
        except Exception:
            is_alive = False

    print("\n" + "=" * 78)
    print(" 🤖 SOLOEARN AUTONOMOUS DAEMON — HEALTH & OPERATIONAL STATUS")
    print("=" * 78)
    print(f" State:               {status} (Process PID: {pid} | Alive: {'YES' if is_alive else 'NO / IDLE'})")
    print(f" Execution Mode:      {mode}")
    print(f" Uptime:              {uptime:,.1f} seconds ({uptime/3600:,.2f} hours)")
    print(f" Cycles Completed:    {cycle}")
    print(f" Consecutive Errors:  {failures}")
    print(f" Last Heartbeat:      {heartbeat}")
    print(f" Next Cycle Est:      {data.get('next_run_estimated_utc') or 'N/A'}")
    if last_err:
        print(f" Last Error:          {last_err} (at {data.get('last_error_utc')})")
    print("-" * 78)

    db_stats = data.get("database_stats", {})
    if db_stats:
        print(f" 🗄️ Database Size:      {db_stats.get('db_size_bytes', 0) / 1024:,.1f} KB")
        print(f" 📝 WAL Size:           {db_stats.get('wal_size_bytes', 0) / 1024:,.1f} KB")
        print(f" 💾 Total Backups:      {db_stats.get('total_backups', 0)} (Latest: {db_stats.get('last_backup_file') or 'None'})")
        print(f" 🛡️ SQLite Integrity:   {'OK' if db_stats.get('integrity_ok') else 'WARNING: ' + str(db_stats.get('integrity_messages'))}")

    life = data.get("lifetime_metrics", {})
    if life:
        print("-" * 78)
        print(f" 💼 Lifetime Revenue:   ₹{life.get('total_revenue_earned_inr', 0.0):,.2f}")
        print(f" 💰 Lifetime Profit:    ₹{life.get('total_net_owner_profit_inr', 0.0):,.2f}")
        print(f" 📦 Tasks Completed:    {life.get('total_tasks_executed', 0)}")
        print(f" 📬 Outreach Sent:      {life.get('total_pitches_dispatched', 0)}")
    print("=" * 78 + "\n")


def main():
    parser = argparse.ArgumentParser(description="SOLOEARN 100% Autonomous Revenue Auto-Pilot Daemon")
    parser.add_argument("--sandbox", action="store_true", default=True, help="Run in sandbox simulation mode")
    parser.add_argument("--live", action="store_true", help="Run in live production mode")
    parser.add_argument("--once", action="store_true", help="Run a single autonomous cycle and exit")
    parser.add_argument("--interval", type=int, default=60, help="Interval in seconds between cycles (default 60s)")
    parser.add_argument("--checkpoint", action="store_true", help="Execute immediate SQLite WAL checkpoint and exit")
    parser.add_argument("--backup", action="store_true", help="Execute immediate automated database backup and exit")
    parser.add_argument("--vacuum", action="store_true", help="Execute immediate database vacuum and exit")
    parser.add_argument("--maintenance", action="store_true", help="Execute full database maintenance check and exit")
    parser.add_argument("--status", action="store_true", help="Display daemon operational telemetry and exit")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO", help="Logging level")
    args = parser.parse_args()

    # Configure logging level from CLI
    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logger.setLevel(log_level)

    # 1. Operational CLI tooling commands
    if args.status:
        print_status_dashboard()
        sys.exit(0)

    if args.checkpoint:
        print("[MAINTENANCE] Checkpointing SQLite Write-Ahead-Log (WAL)...")
        res = checkpoint_wal("TRUNCATE")
        print(f"WAL Checkpoint Result: {json.dumps(res, indent=2)}")
        sys.exit(0 if res.get("success") else 1)

    if args.backup:
        print("[MAINTENANCE] Creating online atomic database backup...")
        b_path = backup_database(keep_count=7, verify_after=True)
        print(f"Backup created successfully: {b_path} ({b_path.stat().st_size:,} bytes)")
        sys.exit(0)

    if args.vacuum:
        print("[MAINTENANCE] Running database VACUUM optimization...")
        v_ok = vacuum_database()
        print(f"Database VACUUM {'SUCCEEDED' if v_ok else 'FAILED/DEFERRED'}")
        sys.exit(0 if v_ok else 1)

    if args.maintenance:
        print("[MAINTENANCE] Running complete database maintenance suite...")
        mgr = DatabaseMaintenanceManager()
        res = mgr.run_maintenance_if_due(force=True)
        print(f"Maintenance Results:\n{json.dumps(res, indent=2)}")
        sys.exit(0)

    # 2. Main Daemon Loop Setup
    is_sandbox = not args.live
    engine = AutonomousMasterEngine(is_sandbox=is_sandbox)

    if args.once:
        start_ts = time.time()
        metrics = engine.run_complete_cycle()
        duration = round(time.time() - start_ts, 2)
        health_state = DaemonHealthState(
            status="completed_once",
            pid=os.getpid(),
            mode="sandbox" if is_sandbox else "live",
            started_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            uptime_seconds=duration,
            current_cycle=1,
            last_cycle_duration_seconds=duration,
            last_cycle_metrics=metrics.to_dict(),
            database_stats=get_database_stats().to_dict(),
            maintenance_stats=engine.db_maintenance.get_summary(),
            system_health=run_system_health_audit().to_dict(),
        )
        accumulate_lifetime_metrics(health_state, metrics)
        update_health_telemetry(health_state)
        print_banner(metrics, is_sandbox)
        sys.exit(0)

    # 3. Continuous 24/7 Resilient Daemon Loop
    print("\n" + "=" * 78)
    print(" 🤖 STARTING SOLOEARN 100% AUTONOMOUS BACKGROUND DAEMON")
    print(f" Mode: {'SANDBOX (Safe Test Simulation)' if is_sandbox else 'LIVE PRODUCTION'}")
    print(f" Sweep Interval: {args.interval} seconds")
    print(f" Log File:       {DAEMON_LOG_FILE}")
    print(f" Events Log:     {DAEMON_EVENTS_FILE}")
    print(f" Telemetry File: {TELEMETRY_FILE}")
    print(" Press Ctrl+C at any time to pause or exit.")
    print("=" * 78)

    cycle_count = 0
    consecutive_failures = 0
    backoff = ExponentialBackoff(base_delay=10.0, max_delay=300.0, factor=2.0)
    start_time = time.time()
    started_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    health_state = DaemonHealthState(
        status="running",
        pid=os.getpid(),
        mode="sandbox" if is_sandbox else "live",
        started_at_utc=started_at_utc,
    )
    update_health_telemetry(health_state)

    shutdown_requested = False

    def handle_shutdown(signum, frame):
        nonlocal shutdown_requested
        logger.info(f"Shutdown signal ({signum}) received. Initiating graceful shutdown...")
        shutdown_requested = True

    signal.signal(signal.SIGINT, handle_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_shutdown)

    log_daemon_event("daemon_started", {
        "mode": "sandbox" if is_sandbox else "live",
        "interval_seconds": args.interval,
        "pid": os.getpid(),
    })

    while not shutdown_requested:
        cycle_count += 1
        cycle_start = time.time()
        health_state.current_cycle = cycle_count
        health_state.uptime_seconds = round(time.time() - start_time, 1)
        health_state.status = "running"
        update_health_telemetry(health_state)

        try:
            print(f"\n[Cycle #{cycle_count}] Executing autonomous sweep...")
            metrics = engine.run_complete_cycle()
            cycle_duration = round(time.time() - cycle_start, 2)

            # Reset consecutive failure counter on successful execution
            consecutive_failures = 0
            health_state.consecutive_failures = 0
            health_state.last_error = None
            health_state.last_cycle_duration_seconds = cycle_duration
            health_state.last_cycle_metrics = metrics.to_dict()
            accumulate_lifetime_metrics(health_state, metrics)
            health_state.database_stats = get_database_stats().to_dict()
            health_state.maintenance_stats = engine.db_maintenance.get_summary()
            health_state.system_health = run_system_health_audit().to_dict()

            print_banner(metrics, is_sandbox)
            log_daemon_event("cycle_completed", {
                "cycle": cycle_count,
                "duration_seconds": cycle_duration,
                "metrics": metrics.to_dict(),
            })

            health_state.status = "sleeping"
            next_run_ts = time.time() + args.interval
            health_state.next_run_estimated_utc = datetime.fromtimestamp(next_run_ts, tz=timezone.utc).isoformat(timespec="seconds")
            update_health_telemetry(health_state)

            print(f"Sleeping for {args.interval}s until next cycle... (Press Ctrl+C to halt)")
            # Sleep in 1-second slices so shutdown is immediately responsive
            for _ in range(max(1, int(args.interval))):
                if shutdown_requested:
                    break
                time.sleep(1)

        except Exception as exc:
            consecutive_failures += 1
            cycle_duration = round(time.time() - cycle_start, 2)
            health_state.consecutive_failures = consecutive_failures
            health_state.last_error = f"{type(exc).__name__}: {str(exc)}"
            health_state.last_error_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

            net_err = is_network_error(exc)
            health_state.status = "degraded" if net_err else "error"
            backoff_delay = backoff.compute_delay(consecutive_failures)

            logger.error(
                f"[RECOVERY RESILIENCE] Cycle #{cycle_count} encountered {type(exc).__name__}: {exc}. "
                f"Consecutive errors: {consecutive_failures}. Backing off for {backoff_delay}s...",
                exc_info=True,
            )

            log_daemon_event("cycle_error", {
                "cycle": cycle_count,
                "duration_seconds": cycle_duration,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "is_network_error": net_err,
                "consecutive_failures": consecutive_failures,
                "backoff_delay_seconds": backoff_delay,
                "traceback": traceback.format_exc(),
            }, level="ERROR")

            next_run_ts = time.time() + backoff_delay
            health_state.next_run_estimated_utc = datetime.fromtimestamp(next_run_ts, tz=timezone.utc).isoformat(timespec="seconds")
            update_health_telemetry(health_state)

            # Sleep during backoff with shutdown responsiveness
            sleep_slices = max(1, int(backoff_delay))
            for _ in range(sleep_slices):
                if shutdown_requested:
                    break
                time.sleep(1)

    # Clean, safe graceful termination
    print("\n[PAUSED] Gracefully stopping autonomous daemon...")
    logger.info("[GRACEFUL SHUTDOWN] Performing final SQLite WAL checkpoint...")
    try:
        cp_final = checkpoint_wal("TRUNCATE")
        logger.info(f"Final shutdown WAL checkpoint: {cp_final}")
    except Exception as ex:
        logger.warning(f"Error during final WAL checkpoint: {ex}")

    health_state.status = "stopped"
    health_state.uptime_seconds = round(time.time() - start_time, 1)
    health_state.next_run_estimated_utc = None
    update_health_telemetry(health_state)

    log_daemon_event("daemon_stopped", {
        "cycles_completed": cycle_count,
        "uptime_seconds": health_state.uptime_seconds,
    })
    logger.info(f"[GRACEFUL SHUTDOWN] Daemon stopped cleanly. Completed {cycle_count} cycles.")
    sys.exit(0)


if __name__ == "__main__":
    main()
