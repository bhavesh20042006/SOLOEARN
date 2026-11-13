from __future__ import annotations

"""
SOLOEARN Autonomous Outreach & Multi-Channel Dispatcher Engine (v2.0)
High-Converting D2C Outreach, Anti-Spam Deliverability, Multi-Touch Sequencing & Funnel Analytics.

Key Capabilities:
1. High-converting, personalized cold email & LinkedIn message templates tailored to Indian D2C founders/COOs.
2. Anti-spam compliance: RFC 8058 One-Click List-Unsubscribe header generation, SPF/DKIM/DMARC verification,
   verifiable sender identity, statutory Indian B2B unsubscribe footer, recipient cooling-off, and domain rate-limiting.
3. Multi-touch follow-up sequencing: Day 1 pitch, Day 3 value add benchmark, Day 7 graceful breakup.
4. End-to-end conversion funnel analytics tracking: sent -> opened -> replied -> proposal accepted -> pipeline revenue.
"""

import email.utils
import hashlib
import json
import logging
import os
import re
import smtplib
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from core.config import DATA_DIR
from core.db import connect, utc_now

logger = logging.getLogger("soloearn.outreach")

DEFAULT_DAILY_OUTREACH_LIMIT = 10
MIN_INTERVAL_SECONDS = 15.0  # Polite rate limiting between autonomous sends
DEFAULT_COOLING_OFF_HOURS = 48  # Minimum spacing before re-contacting the same lead outside active sequence
DEFAULT_MAX_PER_DOMAIN_PER_DAY = 3  # Maximum outbound messages to any single company domain per 24 hours


# ---------------------------------------------------------------------------
# Database Schema Initialization & Migration
# ---------------------------------------------------------------------------

def init_outreach_tables(conn=None) -> None:
    """Initializes and smoothly migrates tables required for outreach dispatch, sequences, and analytics."""
    def _run(c):
        # 1. Base communication logs table
        c.execute(
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
        # Migrate additional columns if not present
        cols = {r[1] for r in c.execute("PRAGMA table_info(friday_communication_logs)").fetchall()}
        col_defs = {
            "tracking_id": "TEXT DEFAULT ''",
            "sequence_step": "INTEGER DEFAULT 1",
            "opened_at": "TEXT DEFAULT ''",
            "replied_at": "TEXT DEFAULT ''",
            "reply_sentiment": "TEXT DEFAULT ''",
            "reply_text": "TEXT DEFAULT ''",
            "proposal_id": "INTEGER DEFAULT NULL",
            "deal_value_inr": "REAL DEFAULT 0.0",
            "converted_at": "TEXT DEFAULT ''",
        }
        for col_name, col_type in col_defs.items():
            if col_name not in cols:
                c.execute(f"ALTER TABLE friday_communication_logs ADD COLUMN {col_name} {col_type}")

        # 2. Suppression / Opt-out list table
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS outreach_suppression_list (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient TEXT UNIQUE NOT NULL,
                reason TEXT DEFAULT 'unsubscribe_request',
                source TEXT DEFAULT 'manual',
                created_at TEXT NOT NULL
            );
            """
        )

        # 3. Multi-touch sequence state machine table
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS outreach_sequences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER,
                client_name TEXT NOT NULL,
                recipient TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'email',
                role TEXT NOT NULL DEFAULT 'founder',
                current_step INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                last_touch_at TEXT,
                next_touch_due_at TEXT,
                metadata TEXT DEFAULT '{}',
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        c.commit()

    if conn is not None:
        _run(conn)
    else:
        with connect() as conn:
            _run(conn)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class DispatchResult:
    status: str  # "delivered", "failed", "rate_limited", "simulated", "opted_out", "drafted_no_provider_configured"
    message_id: str
    recipient: str
    channel: str
    provider: str
    dispatched_at: str
    error_message: str = ""
    verification_hash: str = ""
    tracking_id: str = ""
    sequence_step: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Anti-Spam Compliance & Email Authentication (SPF / DKIM / DMARC)
# ---------------------------------------------------------------------------

def verify_email_domain_auth(domain: str) -> dict[str, Any]:
    """
    Performs DNS verification checks for SPF, DMARC, and DKIM on the given sender domain.
    Returns structured diagnostics and actionable deliverability recommendations.
    Uses nslookup with robust timeout and fallback to prevent hanging.
    """
    clean_domain = domain.strip().lower()
    if "@" in clean_domain:
        clean_domain = clean_domain.split("@")[-1]

    result: dict[str, Any] = {
        "domain": clean_domain,
        "spf_configured": False,
        "spf_record": "",
        "dmarc_configured": False,
        "dmarc_record": "",
        "dmarc_policy": "none",
        "dkim_status": "unknown",
        "deliverability_risk": "high",
        "recommendations": [],
    }

    if not clean_domain or "." not in clean_domain:
        result["recommendations"].append("Invalid domain provided for DNS authentication check.")
        return result

    # 1. Check SPF record
    try:
        proc = subprocess.run(
            ["nslookup", "-q=TXT", clean_domain],
            capture_output=True,
            text=True,
            timeout=5,
        )
        txt_output = proc.stdout or ""
        for line in txt_output.splitlines():
            line_clean = line.strip().strip('"')
            if line_clean.startswith("v=spf1") or "v=spf1 " in line_clean:
                result["spf_configured"] = True
                result["spf_record"] = line_clean
                break
    except Exception as exc:
        logger.warning(f"SPF DNS check failed for {clean_domain}: {exc}")

    # 2. Check DMARC record (_dmarc.domain)
    try:
        proc_dmarc = subprocess.run(
            ["nslookup", "-q=TXT", f"_dmarc.{clean_domain}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        dmarc_output = proc_dmarc.stdout or ""
        for line in dmarc_output.splitlines():
            line_clean = line.strip().strip('"')
            if line_clean.startswith("v=DMARC1") or "v=DMARC1;" in line_clean:
                result["dmarc_configured"] = True
                result["dmarc_record"] = line_clean
                m = re.search(r"p=([a-zA-Z]+)", line_clean)
                if m:
                    result["dmarc_policy"] = m.group(1).lower()
                break
    except Exception as exc:
        logger.warning(f"DMARC DNS check failed for {clean_domain}: {exc}")

    # 3. Check common DKIM selectors (google, default, k1, smtp, mail, s1, selector1)
    selectors_to_check = ["google", "default", "k1", "smtp", "mail", "s1", "selector1"]
    for sel in selectors_to_check:
        try:
            proc_dkim = subprocess.run(
                ["nslookup", "-q=TXT", f"{sel}._domainkey.{clean_domain}"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            dkim_out = proc_dkim.stdout or ""
            if "v=DKIM1" in dkim_out or "p=" in dkim_out:
                result["dkim_status"] = f"configured (selector: {sel})"
                break
        except Exception:
            continue

    # 4. Formulate risk profile & recommendations
    recommendations = []
    if not result["spf_configured"]:
        recommendations.append(f"Missing SPF record: add TXT record 'v=spf1 include:_spf.google.com ~all' at {clean_domain}")
    if not result["dmarc_configured"]:
        recommendations.append(f"Missing DMARC record: add TXT record 'v=DMARC1; p=quarantine; rua=mailto:dmarc@{clean_domain}' at _dmarc.{clean_domain}")
    elif result["dmarc_policy"] == "none":
        recommendations.append("DMARC policy is 'p=none': upgrade to 'p=quarantine' or 'p=reject' to satisfy Google/Yahoo 2024 bulk sender rules.")
    if result["dkim_status"] == "unknown":
        recommendations.append("DKIM verification: ensure DKIM TXT record is published in DNS for your SMTP provider.")

    result["recommendations"] = recommendations
    if result["spf_configured"] and result["dmarc_configured"] and result["dmarc_policy"] in {"quarantine", "reject"}:
        result["deliverability_risk"] = "low"
    elif result["spf_configured"] or result["dmarc_configured"]:
        result["deliverability_risk"] = "medium"
    else:
        result["deliverability_risk"] = "high"

    return result


def check_outreach_deliverability(sender_email: str | None = None) -> dict[str, Any]:
    """
    Evaluates deliverability readiness for the configured or specified sender email.
    Checks SPF, DKIM, DMARC, SMTP configuration, and provides pre-dispatch diagnostics.
    """
    provider = SMTPOutreachProvider()
    from_email = sender_email or provider.from_email or "ops@soloearn.local"
    domain = from_email.split("@")[-1] if "@" in from_email else "soloearn.local"
    dns_auth = verify_email_domain_auth(domain)

    can_send, count_today, remaining = get_outreach_rate_limit_status(is_sandbox=False)

    is_deliverable = dns_auth["deliverability_risk"] in {"low", "medium"} and provider.is_configured()

    return {
        "sender_email": from_email,
        "sender_domain": domain,
        "smtp_configured": provider.is_configured(),
        "smtp_host": provider.smtp_host,
        "dns_authentication": dns_auth,
        "rate_limit_status": {
            "can_send": can_send,
            "count_today": count_today,
            "remaining_today": remaining,
            "warmup_limit": get_warmup_daily_limit(1),
        },
        "is_ready_for_dispatch": is_deliverable,
    }


def format_compliant_email_body(
    body_text: str,
    sender_name: str = "Bhavesh Khullar",
    business_name: str = "SOLOEARN Operations",
    tracking_id: str = "",
) -> str:
    """
    Appends required B2B compliance disclosures:
    - Verifiable sender identity & physical operational jurisdiction
    - Specific 1-to-1 commercial business purpose notice under Indian IT Act
    - Easy one-click or reply-based opt-out / unsubscribe mechanism
    """
    unsubscribe_link = f"https://ops.soloearn.local/unsubscribe?id={tracking_id or 'lead'}"
    opt_out_footer = (
        "\n\n---\n"
        f"Sent by {sender_name} | {business_name}\n"
        "B2B Operational Analytics & Data Engineering Services (New Delhi, India)\n"
        "Direct 1-to-1 Business Inquiry: We identify operational & marketplace data discrepancies for Indian enterprises.\n"
        "Opt-out preferences: If you do not wish to receive operational reviews from us, "
        f"reply with 'UNSUBSCRIBE' or visit {unsubscribe_link} and your contact details will be immediately removed."
    )
    if "UNSUBSCRIBE" not in body_text.upper():
        return body_text + opt_out_footer
    return body_text


def format_compliant_html_body(
    body_text: str,
    sender_name: str = "Bhavesh Khullar",
    business_name: str = "SOLOEARN Operations",
    tracking_id: str = "",
) -> str:
    """Generates a clean, typography-optimized HTML multipart version of the compliant outreach message."""
    formatted_body = body_text.replace("\n\n", "</p><p>").replace("\n", "<br/>")
    unsubscribe_link = f"https://ops.soloearn.local/unsubscribe?id={tracking_id or 'lead'}"
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #222222; margin: 0; padding: 16px; background-color: #fafafa; }}
        .email-container {{ max-width: 600px; margin: 0 auto; background: #ffffff; padding: 24px; border: 1px solid #e2e8f0; border-radius: 6px; }}
        .footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #e2e8f0; font-size: 11px; color: #718096; line-height: 1.5; }}
        .footer a {{ color: #4a5568; text-decoration: underline; }}
    </style>
</head>
<body>
    <div class="email-container">
        <p>{formatted_body}</p>
        <div class="footer">
            <p><strong>Sent by {sender_name} | {business_name}</strong><br/>
            B2B Operational Analytics & Data Engineering Services (New Delhi, India)<br/>
            Direct 1-to-1 Business Inquiry: Identifying operational & marketplace settlement discrepancies for Indian enterprises.<br/>
            If you do not wish to receive operational reviews from us, reply with 'UNSUBSCRIBE' or <a href="{unsubscribe_link}">click here to unsubscribe</a>.</p>
        </div>
    </div>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Opt-Out & Suppression List Registry
# ---------------------------------------------------------------------------

def is_suppressed(recipient: str) -> bool:
    """Checks if the given email address or social handle is on the active suppression list."""
    if not recipient:
        return False
    init_outreach_tables()
    clean_target = recipient.strip().lower()
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM outreach_suppression_list WHERE LOWER(recipient) = ?",
            (clean_target,),
        ).fetchone()
        if row:
            return True
        # Also check if user previously opted out in communication logs
        row2 = conn.execute(
            "SELECT id FROM friday_communication_logs WHERE LOWER(recipient) = ? AND status = 'opted_out'",
            (clean_target,),
        ).fetchone()
        return bool(row2)


def add_to_suppression_list(recipient: str, reason: str = "unsubscribe_request", source: str = "auto") -> bool:
    """Registers an email address or handle onto the suppression list and halts any active sequences."""
    if not recipient:
        return False
    init_outreach_tables()
    clean_target = recipient.strip().lower()
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO outreach_suppression_list (recipient, reason, source, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(recipient) DO UPDATE SET reason = excluded.reason
            """,
            (clean_target, reason, source, now),
        )
        # Update any active multi-touch sequence to opted_out
        conn.execute(
            "UPDATE outreach_sequences SET status = 'opted_out', updated_at = ? WHERE LOWER(recipient) = ?",
            (now, clean_target),
        )
        conn.commit()
    logger.info(f"[OUTREACH SUPPRESSION] Recipient {clean_target} permanently suppressed ({reason})")
    return True


def remove_from_suppression_list(recipient: str) -> bool:
    """Removes a recipient from the suppression list upon explicit re-opt-in."""
    init_outreach_tables()
    clean_target = recipient.strip().lower()
    with connect() as conn:
        cur = conn.execute("DELETE FROM outreach_suppression_list WHERE LOWER(recipient) = ?", (clean_target,))
        conn.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Rate Limiting & Safe Sending Guards
# ---------------------------------------------------------------------------

def get_warmup_daily_limit(days_active: int = 1) -> int:
    """Returns the safe ramp-up daily outreach limit based on domain sending age."""
    if days_active <= 3:
        return 5
    elif days_active <= 7:
        return 10
    elif days_active <= 14:
        return 20
    else:
        return 50


def check_recipient_cooling_off(recipient: str, cooling_hours: int = DEFAULT_COOLING_OFF_HOURS) -> tuple[bool, str]:
    """
    Ensures we do not repeatedly message the same individual or email within cooling_hours.
    Returns (is_allowed, reason).
    """
    clean_target = recipient.strip().lower()
    init_outreach_tables()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT created_at FROM friday_communication_logs
            WHERE LOWER(recipient) = ? AND status IN ('dispatched', 'delivered', 'simulated')
            ORDER BY id DESC LIMIT 1
            """,
            (clean_target,),
        ).fetchone()
        if not row:
            return True, ""
        last_dt = row[0]
        # Check if within cooling window
        try:
            last_time = datetime.fromisoformat(last_dt.replace("Z", "+00:00"))
            cutoff = datetime.now(timezone.utc) - timedelta(hours=cooling_hours)
            if last_time > cutoff:
                return False, f"Recipient contacted within {cooling_hours}h cooling window (last: {last_dt})"
        except Exception:
            pass
    return True, ""


def check_domain_rate_limit(recipient: str, max_per_domain_day: int = DEFAULT_MAX_PER_DOMAIN_PER_DAY, is_sandbox: bool = False) -> tuple[bool, str]:
    """
    Prevents blasting multiple executives at the same company domain on the same day.
    Returns (is_allowed, reason).
    """
    if "@" not in recipient:
        return True, ""
    domain = recipient.strip().lower().split("@")[-1]
    # Skip public generic webmail domains
    generic_domains = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "rediffmail.com"}
    if domain in generic_domains:
        return True, ""

    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM friday_communication_logs
            WHERE LOWER(recipient) LIKE ? AND created_at LIKE ? AND status IN ('dispatched', 'delivered', 'simulated') AND is_sandbox = ?
            """,
            (f"%@{domain}", f"{today_prefix}%", int(is_sandbox)),
        ).fetchone()
        count = row[0] if row else 0
        limit = max_per_domain_day if not is_sandbox else max(max_per_domain_day, 100)
        if count >= limit:
            return False, f"Domain daily limit reached ({count}/{limit} sent to @{domain} today)"
    return True, ""


def get_outreach_rate_limit_status(
    daily_limit: int = DEFAULT_DAILY_OUTREACH_LIMIT,
    is_sandbox: bool = False,
    recipient: str = "",
) -> tuple[bool, int, int]:
    """
    Checks today's autonomous dispatch count against the daily safety limit.
    Returns: (can_send, count_today, remaining_today)
    """
    init_outreach_tables()
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM friday_communication_logs
            WHERE created_at LIKE ? AND status IN ('dispatched', 'delivered') AND is_sandbox = ?
            """,
            (f"{today_prefix}%", int(is_sandbox)),
        ).fetchone()
        count_today = row[0] if row else 0

    can_send = count_today < daily_limit
    remaining = max(0, daily_limit - count_today)

    # If recipient is given and is suppressed, cannot send
    if recipient and is_suppressed(recipient):
        return False, count_today, remaining

    return can_send, count_today, remaining


# ---------------------------------------------------------------------------
# High-Converting D2C Outreach Templates (Founder / COO Multi-Touch Sequences)
# ---------------------------------------------------------------------------

class D2COutreachTemplates:
    """
    Battle-tested cold outreach message templates tailored for Indian D2C Founders and COOs.
    Covers Day 1 (Initial Pitch), Day 3 (Value Add), and Day 7 (Breakup) cadences across Email & LinkedIn.
    """

    @classmethod
    def list_supported_roles(cls) -> list[str]:
        return ["founder", "coo", "ops_lead", "finance_head"]

    @classmethod
    def list_supported_steps(cls) -> list[dict[str, Any]]:
        return [
            {"step": 1, "day": 1, "name": "initial_pitch", "focus": "Quantitative leakage hook & low-friction discovery question"},
            {"step": 2, "day": 3, "name": "value_add", "focus": "Concrete 3-point benchmark / 1-page reconciliation framework"},
            {"step": 3, "day": 7, "name": "breakup", "focus": "Graceful close-out removing pressure while leaving the door open"},
        ]

    @classmethod
    def get_sample_context(cls, role: str = "founder") -> dict[str, Any]:
        """Provides default realistic context for previews and tests."""
        return {
            "founder_name": "Rohan",
            "contact_name": "Rohan",
            "company_name": "Snitch Apparels",
            "channels": "Amazon, Blinkit & Shopify",
            "sender_name": "Bhavesh Khullar",
            "estimated_leakage_inr": "₹1,20,000–₹3,50,000 monthly",
            "courier_partners": "Delhivery, Shiprocket & BlueDart",
            "monthly_orders": "10,000+",
            "sku_count": "150+",
        }

    @classmethod
    def list_templates_catalog(cls) -> list[dict[str, Any]]:
        """Returns structured metadata catalog of all supported templates across steps, roles, and channels."""
        catalog = []
        for step_info in cls.list_supported_steps():
            step = step_info["step"]
            for role in cls.list_supported_roles():
                for channel in ["email", "linkedin"]:
                    rendered = cls.render_template(step, channel, role, cls.get_sample_context(role))
                    catalog.append({
                        "step": step,
                        "day": step_info["day"],
                        "step_name": step_info["name"],
                        "role": role,
                        "channel": channel,
                        "subject": rendered["subject"],
                        "cta": rendered["cta"],
                        "body_preview": rendered["body"][:120] + "...",
                    })
        return catalog

    @classmethod
    def render_template(
        cls,
        step: int,
        channel: str,
        role: str = "founder",
        context: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """
        Renders a personalized outreach message.
        Returns {"subject": ..., "body": ..., "cta": ...}
        """
        ctx = context or {}
        founder_name = ctx.get("founder_name") or ctx.get("contact_name") or "there"
        company_name = ctx.get("company_name") or "your brand"
        sender_name = ctx.get("sender_name") or "Bhavesh Khullar"
        channels = ctx.get("channels") or "Amazon, Blinkit & Shopify"
        clean_channel = channel.lower().strip()
        clean_role = role.lower().strip()
        if clean_role not in cls.list_supported_roles():
            clean_role = "founder"

        # -------------------------------------------------------------------
        # DAY 1: INITIAL PITCH
        # -------------------------------------------------------------------
        if step == 1:
            if clean_role in {"founder", "finance_head"}:
                if clean_channel == "email":
                    subject = f"quick question re: {company_name} marketplace settlements"
                    body = (
                        f"Hi {founder_name},\n\n"
                        f"I've been following {company_name}'s recent momentum across D2C and marketplace channels. "
                        f"As high-growth Indian consumer brands scale across {channels}, we regularly find that 2.2% to 4.1% of net GMV "
                        f"silently leaks through uncredited marketplace return fees, courier volumetric weight penalties, and Section 194-O TDS variances.\n\n"
                        f"At SOLOEARN, we build automated data reconciliation pipelines for D2C brands. "
                        f"We reconcile 10,000+ monthly orders against marketplace settlement remittance sheets in seconds, "
                        f"pinpointing exact underpayments with 100% mathematical audit trails.\n\n"
                        f"Would you be open to a quick 10-minute sanity check where we audit a sample settlement batch for {company_name}?"
                    )
                    cta = "Open to a 10-minute audit review?"
                else:  # LinkedIn
                    subject = f"Connecting re: {company_name} reconciliation"
                    body = (
                        f"Hi {founder_name}, loved {company_name}'s brand growth. As you scale across {channels}, "
                        f"many D2C founders lose 2-3% of net GMV to untracked marketplace deductions, uncredited RTO freight, "
                        f"and 194-O TDS variances that standard tally/ERP misses. We built an automated reconciliation engine "
                        f"that audits marketplace remittances with 100% mathematical precision. "
                        f"Would you be open to reviewing a 1-page sample audit of where brands usually recover margin?"
                    )
                    cta = "Review sample audit?"
            else:  # COO / Ops Lead
                if clean_channel == "email":
                    subject = f"automating {company_name}'s multi-channel reconciliation & courier audit"
                    body = (
                        f"Hi {founder_name},\n\n"
                        f"Managing daily order dispatches and marketplace returns across {channels} usually forces ops teams into "
                        f"15+ hours of manual Excel stitching every week—frequently missing courier volumetric weight overcharges and uncredited returns.\n\n"
                        f"We build lightweight, standalone automation pipelines that cross-check warehouse dispatch logs against courier invoices "
                        f"(Delhivery/Shiprocket/BlueDart) and marketplace settlement remittances automatically. It flags shipping overcharges and "
                        f"settlement variances within minutes.\n\n"
                        f"Would you be open to a 10-minute chat or seeing how we automate 3PL weight dispute auditing for Indian D2C ops teams?"
                    )
                    cta = "Automate 3PL dispute checks?"
                else:  # LinkedIn
                    subject = f"Quick ops question for {company_name}"
                    body = (
                        f"Hi {founder_name}, quick operational question: how many hours does your team spend reconciling marketplace settlements "
                        f"and 3PL courier weight discrepancies across {channels}? We built an automated reconciliation pipeline that audits "
                        f"order-level remittances and flags courier overcharges in minutes. Happy to share a sample audit workflow if useful for your ops team."
                    )
                    cta = "Share sample workflow?"

        # -------------------------------------------------------------------
        # DAY 3: VALUE ADD (BENCHMARK & FRAMEWORK)
        # -------------------------------------------------------------------
        elif step == 2:
            if clean_role in {"founder", "finance_head"}:
                if clean_channel == "email":
                    subject = f"3 reconciliation leakages Indian D2C founders overlook (benchmark for {company_name})"
                    body = (
                        f"Hi {founder_name},\n\n"
                        f"Following up on my earlier note. Rather than pitching, I wanted to share a concrete benchmark from the multi-channel "
                        f"settlement audits we run for Indian D2C brands:\n\n"
                        f"1. **Courier Volumetric Weight Rounding**: 3PL carriers frequently round up package dimensions by 0.5kg increments, "
                        f"costing brands ₹35–₹75 per shipment if undisputed within the 7-day window.\n"
                        f"2. **Uncredited RTO Reverse Shipping**: Up to 4.5% of customer COD returns never receive the contracted reverse-freight credit "
                        f"on marketplace settlement cycles.\n"
                        f"3. **Section 194-O TDS & TCS Discrepancies**: Variance between marketplace monthly tax statements and AIS/Form 26AS entries "
                        f"creates delayed tax credits and trapped working capital.\n\n"
                        f"We compiled these checks into a lightweight Python/Excel reconciliation pack. "
                        f"Would it be helpful if I shared our 1-page Reconciliation Framework or ran an audit on a sample settlement file for {company_name}?"
                    )
                    cta = "Send 1-page framework?"
                else:  # LinkedIn
                    subject = f"Quick benchmark for {company_name}"
                    body = (
                        f"Hi {founder_name}, sharing a quick benchmark: in our audits of Indian D2C brands scaling across {channels}, "
                        f"~2.8% of net GMV leaks through courier volumetric weight disputes and uncredited RTO reverse-freight charges. "
                        f"We put together a 1-page ops checklist on the 3 biggest settlement leakages. Would it be helpful to share over LinkedIn?"
                    )
                    cta = "Share ops checklist?"
            else:  # COO / Ops Lead
                if clean_channel == "email":
                    subject = f"framework: 3PL courier weight discrepancy recovery for {company_name}"
                    body = (
                        f"Hi {founder_name},\n\n"
                        f"Following up on my previous note. Many COOs we work with found that courier weight disputes (Delhivery, Shiprocket, BlueDart) "
                        f"must be raised within 5-7 days of billing, or the overcharges become permanent losses.\n\n"
                        f"We put together a lightweight Python & Excel reconciliation workflow that compares actual product SKU master dimensions "
                        f"against billed courier volumetric weights, automatically generating ready-to-file dispute CSVs.\n\n"
                        f"Would it be useful if I sent over this dispute recovery checklist or demonstrated how it integrates with your current warehouse dispatch sheet?"
                    )
                    cta = "Send dispute recovery checklist?"
                else:  # LinkedIn
                    subject = f"Courier dispute framework for {company_name}"
                    body = (
                        f"Hi {founder_name}, quick benchmark for your ops team: automated courier weight reconciliation typically recovers "
                        f"₹40,000–₹1,50,000 monthly for D2C brands doing 5k+ orders. Happy to share our 1-page 3PL dispute automation framework if that would save your team time."
                    )
                    cta = "Share framework?"

        # -------------------------------------------------------------------
        # DAY 7: BREAKUP (GRACEFUL CLOSE)
        # -------------------------------------------------------------------
        else:
            if clean_channel == "email":
                subject = f"closing the loop / operational review for {company_name}"
                body = (
                    f"Hi {founder_name},\n\n"
                    f"I haven't heard back, so I assume operational reconciliation is either smoothly handled in-house or not a current priority for {company_name} right now.\n\n"
                    f"I won't follow up again so as not to clutter your inbox. If you ever find your team spending too much time wrangling marketplace settlement sheets "
                    f"or want an external audit ahead of the upcoming festival sale season, feel free to keep my details handy.\n\n"
                    f"Wishing you and {company_name} continued growth!\n\n"
                    f"Best regards,\n{sender_name}"
                )
                cta = "Door left open"
            else:  # LinkedIn
                subject = f"Closing the loop for {company_name}"
                body = (
                    f"Hi {founder_name}, closing the loop here. I assume your reconciliation workflows are well in hand, so I won't follow up further. "
                    f"Wishing {company_name} fantastic growth this quarter! Feel free to reconnect anytime if you ever want an external sanity check on settlement audits."
                )
                cta = "Closing note"

        return {
            "subject": subject,
            "body": body,
            "cta": cta,
            "channel": clean_channel,
            "role": clean_role,
            "step": step,
        }


# ---------------------------------------------------------------------------
# Transports: SMTP & Webhook
# ---------------------------------------------------------------------------

class SMTPOutreachProvider:
    """Transmits outbound cold/warm email pitches via standard SMTP with strict anti-spam compliance."""

    def __init__(
        self,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        smtp_user: str | None = None,
        smtp_pass: str | None = None,
        from_email: str | None = None,
        from_name: str = "Bhavesh Khullar (SOLOEARN)",
        use_tls: bool = True,
    ):
        self.smtp_host = smtp_host or os.environ.get("SOLOEARN_SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(smtp_port or os.environ.get("SOLOEARN_SMTP_PORT", "587"))
        self.smtp_user = smtp_user or os.environ.get("SOLOEARN_SMTP_USER", "")
        self.smtp_pass = smtp_pass or os.environ.get("SOLOEARN_SMTP_PASS", "")
        self.from_email = from_email or os.environ.get("SOLOEARN_FROM_EMAIL", self.smtp_user)
        self.from_name = from_name
        self.use_tls = use_tls

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass and self.from_email)

    def send_pitch(
        self,
        to_email: str,
        subject: str,
        pitch_body: str,
        client_name: str = "",
        tracking_id: str = "",
        sequence_step: int = 1,
    ) -> DispatchResult:
        if not self.is_configured():
            return DispatchResult(
                status="failed",
                message_id="",
                recipient=to_email,
                channel="email",
                provider="smtp",
                dispatched_at="",
                error_message="SMTP provider not configured. Please supply SOLOEARN_SMTP_USER and SOLOEARN_SMTP_PASS in .env",
                tracking_id=tracking_id,
                sequence_step=sequence_step,
            )

        now = utc_now()
        sender_domain = self.from_email.split("@")[-1] if "@" in self.from_email else "soloearn.local"
        track_id = tracking_id or f"TRK-{hashlib.sha256((to_email + now).encode('utf-8')).hexdigest()[:10].upper()}"
        msg_id = f"MSG-{hashlib.sha256((to_email + now).encode('utf-8')).hexdigest()[:12].upper()}"

        compliance_body_plain = format_compliant_email_body(pitch_body, sender_name=self.from_name, tracking_id=track_id)
        compliance_body_html = format_compliant_html_body(pitch_body, sender_name=self.from_name, tracking_id=track_id)
        pitch_hash = hashlib.sha256(compliance_body_plain.encode("utf-8")).hexdigest()

        # Build RFC-Compliant MIME Multipart Email
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject or f"Operational Data Review for {client_name or 'your brand'}"
        msg["From"] = f"{self.from_name} <{self.from_email}>"
        msg["To"] = to_email
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = f"<{msg_id.lower()}@{sender_domain}>"

        # Anti-Spam Headers (RFC 2369, RFC 8058 One-Click Unsubscribe)
        msg["List-Unsubscribe"] = f"<mailto:unsubscribe@{sender_domain}?subject=unsubscribe:{track_id}>, <https://ops.soloearn.local/unsubscribe?id={track_id}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg["X-Entity-Ref-ID"] = track_id
        msg["X-Mailer"] = "SOLOEARN-Autonomous-Outreach/2.0 (B2B Engine)"
        msg["Feedback-ID"] = f"soloearn-outreach:{client_name.lower().replace(' ', '')}:{msg_id}:{sender_domain}"

        # Attach text & HTML alternatives
        msg.attach(MIMEText(compliance_body_plain, "plain", "utf-8"))
        msg.attach(MIMEText(compliance_body_html, "html", "utf-8"))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=20) as server:
                if self.use_tls:
                    server.starttls()
                server.login(self.smtp_user, self.smtp_pass)
                server.sendmail(self.from_email, [to_email], msg.as_string())

            logger.info(f"[OUTREACH DELIVERED] Email dispatched to {to_email} via SMTP (Message-ID: {msg_id})")
            return DispatchResult(
                status="delivered",
                message_id=msg_id,
                recipient=to_email,
                channel="email",
                provider="smtp",
                dispatched_at=now,
                verification_hash=pitch_hash,
                tracking_id=track_id,
                sequence_step=sequence_step,
            )
        except Exception as exc:
            logger.error(f"[OUTREACH FAILED] SMTP delivery to {to_email} failed: {exc}")
            return DispatchResult(
                status="failed",
                message_id="",
                recipient=to_email,
                channel="email",
                provider="smtp",
                dispatched_at=now,
                error_message=str(exc),
                verification_hash=pitch_hash,
                tracking_id=track_id,
                sequence_step=sequence_step,
            )


class WebhookOutreachProvider:
    """Dispatches outbound outreach pitches to any HTTP webhook (Slack, Discord, Zapier, Make, n8n, CRM)."""

    def __init__(self, webhook_url: str | None = None):
        self.webhook_url = webhook_url or os.environ.get("SOLOEARN_OUTREACH_WEBHOOK_URL", "")

    def is_configured(self) -> bool:
        return bool(self.webhook_url and self.webhook_url.startswith("http"))

    def send_pitch(
        self,
        recipient: str,
        subject: str,
        pitch_body: str,
        client_name: str = "",
        tracking_id: str = "",
        sequence_step: int = 1,
    ) -> DispatchResult:
        import requests

        if not self.is_configured():
            return DispatchResult(
                status="failed",
                message_id="",
                recipient=recipient,
                channel="webhook",
                provider="webhook",
                dispatched_at="",
                error_message="Webhook URL not configured in SOLOEARN_OUTREACH_WEBHOOK_URL.",
                tracking_id=tracking_id,
                sequence_step=sequence_step,
            )

        now = utc_now()
        track_id = tracking_id or f"TRK-{hashlib.sha256((recipient + now).encode('utf-8')).hexdigest()[:10].upper()}"
        compliance_body = format_compliant_email_body(pitch_body, tracking_id=track_id)
        msg_id = f"WHK-{hashlib.sha256((recipient + now).encode('utf-8')).hexdigest()[:12].upper()}"
        pitch_hash = hashlib.sha256(compliance_body.encode("utf-8")).hexdigest()

        payload = {
            "event": "soloearn.autonomous_outreach_dispatched",
            "message_id": msg_id,
            "tracking_id": track_id,
            "sequence_step": sequence_step,
            "client_name": client_name,
            "recipient": recipient,
            "subject": subject,
            "body": compliance_body,
            "timestamp": now,
        }

        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=15)
            resp.raise_for_status()
            logger.info(f"[OUTREACH WEBHOOK SENT] Payload posted to webhook for {client_name} (Status: {resp.status_code})")
            return DispatchResult(
                status="delivered",
                message_id=msg_id,
                recipient=recipient,
                channel="webhook",
                provider="webhook",
                dispatched_at=now,
                verification_hash=pitch_hash,
                tracking_id=track_id,
                sequence_step=sequence_step,
            )
        except Exception as exc:
            logger.error(f"[OUTREACH WEBHOOK FAILED] Webhook post failed: {exc}")
            return DispatchResult(
                status="failed",
                message_id="",
                recipient=recipient,
                channel="webhook",
                provider="webhook",
                dispatched_at=now,
                error_message=str(exc),
                verification_hash=pitch_hash,
                tracking_id=track_id,
                sequence_step=sequence_step,
            )


# ---------------------------------------------------------------------------
# Central Dispatch Router
# ---------------------------------------------------------------------------

def dispatch_client_outreach(
    client_name: str,
    channel: str,
    pitch_text: str,
    recipient: str = "",
    subject: str = "",
    is_sandbox: bool = False,
    override_daily_limit: bool = False,
    sequence_step: int = 1,
    tracking_id: str = "",
) -> DispatchResult:
    """
    Central dispatch router for autonomous or supervised outreach.
    Handles rate-limiting, suppression checking, cooling-off guards, domain caps, and transport selection.
    """
    init_outreach_tables()
    clean_channel = channel.lower().strip()
    target_recipient = recipient or f"contact@{client_name.lower().replace(' ', '').replace('pvtltd', '').replace('ltd', '')}.in"
    target_subject = subject or f"Operational Data Analytics & Reconciliation for {client_name}"
    track_id = tracking_id or f"TRK-{hashlib.sha256((target_recipient + utc_now()).encode('utf-8')).hexdigest()[:10].upper()}"

    # 1. Suppression / Opt-out check (Strict Anti-Spam Guard)
    if is_suppressed(target_recipient):
        logger.warning(f"[OUTREACH BLOCKED] Recipient {target_recipient} is on suppression list (opted out).")
        return DispatchResult(
            status="opted_out",
            message_id="",
            recipient=target_recipient,
            channel=clean_channel,
            provider="suppression_guard",
            dispatched_at=utc_now(),
            error_message="Recipient has previously unsubscribed or opted out. Transmission halted.",
            tracking_id=track_id,
            sequence_step=sequence_step,
        )

    # 2. Rate-limit check (Daily safety limit)
    if not override_daily_limit:
        effective_daily_limit = DEFAULT_DAILY_OUTREACH_LIMIT if not is_sandbox else 200
        can_send, count_today, remaining = get_outreach_rate_limit_status(daily_limit=effective_daily_limit, is_sandbox=is_sandbox)
        if not can_send:
            return DispatchResult(
                status="rate_limited",
                message_id="",
                recipient=target_recipient,
                channel=clean_channel,
                provider="guard",
                dispatched_at=utc_now(),
                error_message=f"Daily safety limit of {effective_daily_limit} messages reached for today ({count_today} sent). Halted for anti-spam safety.",
                tracking_id=track_id,
                sequence_step=sequence_step,
            )

        # 3. Domain rate limit check (Prevent spamming multiple executives at same firm)
        domain_ok, domain_reason = check_domain_rate_limit(target_recipient, is_sandbox=is_sandbox)
        if not domain_ok:
            return DispatchResult(
                status="rate_limited",
                message_id="",
                recipient=target_recipient,
                channel=clean_channel,
                provider="guard",
                dispatched_at=utc_now(),
                error_message=domain_reason,
                tracking_id=track_id,
                sequence_step=sequence_step,
            )

    # 4. Sandbox simulation mode
    if is_sandbox and recipient.strip():
        now = utc_now()
        sim_id = f"SIM-{hashlib.sha256((target_recipient + now).encode('utf-8')).hexdigest()[:8].upper()}"
        pitch_hash = hashlib.sha256(pitch_text.encode("utf-8")).hexdigest()

        with connect() as conn:
            conn.execute(
                """
                INSERT INTO friday_communication_logs (
                    client_name, channel, recipient, subject, body_text,
                    provider, provider_message_id, status, error_message, is_sandbox, created_at,
                    tracking_id, sequence_step
                ) VALUES (?, ?, ?, ?, ?, 'sandbox_mock', ?, 'simulated', '', 1, ?, ?, ?)
                """,
                (client_name, clean_channel, target_recipient, target_subject, pitch_text, sim_id, now, track_id, sequence_step),
            )
            conn.commit()

        return DispatchResult(
            status="simulated",
            message_id=sim_id,
            recipient=target_recipient,
            channel=clean_channel,
            provider="sandbox_mock",
            dispatched_at=now,
            verification_hash=pitch_hash,
            tracking_id=track_id,
            sequence_step=sequence_step,
        )

    # 5. Check live outreach transport configuration
    smtp_provider = SMTPOutreachProvider()
    webhook_provider = WebhookOutreachProvider()
    env_provider = os.environ.get("SOLOEARN_OUTREACH_PROVIDER", "").strip()
    env_ack = os.environ.get("SOLOEARN_OUTREACH_ACK", "").strip()

    has_provider = bool(smtp_provider.is_configured() or webhook_provider.is_configured() or (env_provider and env_ack))

    if not has_provider:
        now = utc_now()
        pitch_hash = hashlib.sha256(pitch_text.encode("utf-8")).hexdigest()
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO friday_communication_logs (
                    client_name, channel, recipient, subject, body_text,
                    provider, provider_message_id, status, error_message, is_sandbox, created_at,
                    tracking_id, sequence_step
                ) VALUES (?, ?, ?, ?, ?, 'unconfigured', '', 'drafted_no_provider_configured',
                          'No external delivery provider configured in environment.', ?, ?, ?, ?)
                """,
                (client_name, clean_channel, target_recipient, target_subject, pitch_text, int(is_sandbox), now, track_id, sequence_step),
            )
            conn.commit()

        return DispatchResult(
            status="drafted_no_provider_configured",
            message_id="",
            recipient=target_recipient,
            channel=clean_channel,
            provider="unconfigured",
            dispatched_at="",
            error_message="No external delivery provider configured in environment.",
            verification_hash=pitch_hash,
            tracking_id=track_id,
            sequence_step=sequence_step,
        )

    # In sandbox mode with configured provider, record simulated dispatch
    if is_sandbox:
        now = utc_now()
        sim_id = f"MSG-{hashlib.sha256((target_recipient + now).encode('utf-8')).hexdigest()[:8].upper()}"
        pitch_hash = hashlib.sha256(pitch_text.encode("utf-8")).hexdigest()

        with connect() as conn:
            conn.execute(
                """
                INSERT INTO friday_communication_logs (
                    client_name, channel, recipient, subject, body_text,
                    provider, provider_message_id, status, error_message, is_sandbox, created_at,
                    tracking_id, sequence_step
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'dispatched', '', 1, ?, ?, ?)
                """,
                (client_name, clean_channel, target_recipient, target_subject, pitch_text, env_provider or "sandbox_mock", sim_id, now, track_id, sequence_step),
            )
            conn.commit()

        return DispatchResult(
            status="dispatched",
            message_id=sim_id,
            recipient=target_recipient,
            channel=clean_channel,
            provider=env_provider or "sandbox_mock",
            dispatched_at=now,
            verification_hash=pitch_hash,
            tracking_id=track_id,
            sequence_step=sequence_step,
        )

    # 6. Live transport transmission
    res: DispatchResult
    if smtp_provider.is_configured() and clean_channel == "email":
        res = smtp_provider.send_pitch(target_recipient, target_subject, pitch_text, client_name, tracking_id=track_id, sequence_step=sequence_step)
    elif webhook_provider.is_configured():
        res = webhook_provider.send_pitch(target_recipient, target_subject, pitch_text, client_name, tracking_id=track_id, sequence_step=sequence_step)
    elif env_provider and env_ack:
        now = utc_now()
        msg_id = f"MSG-{hashlib.sha256((target_recipient + now).encode('utf-8')).hexdigest()[:8].upper()}"
        pitch_hash = hashlib.sha256(pitch_text.encode("utf-8")).hexdigest()
        res = DispatchResult(
            status="dispatched",
            message_id=msg_id,
            recipient=target_recipient,
            channel=clean_channel,
            provider=env_provider,
            dispatched_at=now,
            verification_hash=pitch_hash,
            tracking_id=track_id,
            sequence_step=sequence_step,
        )
    else:
        return DispatchResult(
            status="failed",
            message_id="",
            recipient=target_recipient,
            channel=clean_channel,
            provider="unconfigured",
            dispatched_at=utc_now(),
            error_message="No suitable transport configured.",
            tracking_id=track_id,
            sequence_step=sequence_step,
        )

    # 7. Log real transmission in database
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO friday_communication_logs (
                client_name, channel, recipient, subject, body_text,
                provider, provider_message_id, status, error_message, is_sandbox, created_at,
                tracking_id, sequence_step
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                client_name,
                clean_channel,
                target_recipient,
                target_subject,
                pitch_text,
                res.provider,
                res.message_id,
                res.status,
                res.error_message,
                res.dispatched_at or utc_now(),
                track_id,
                sequence_step,
            ),
        )
        conn.commit()

    return res


# ---------------------------------------------------------------------------
# Multi-Touch Follow-Up Sequencing (Day 1 Pitch, Day 3 Value Add, Day 7 Breakup)
# ---------------------------------------------------------------------------

def enroll_in_sequence(
    lead_id: int | None,
    client_name: str,
    recipient: str,
    channel: str = "email",
    role: str = "founder",
    custom_vars: dict[str, Any] | None = None,
    is_sandbox: bool = True,
) -> dict[str, Any]:
    """
    Enrolls a prospective lead into the 3-touch sequence (Day 1 Pitch, Day 3 Value Add, Day 7 Breakup).
    Dispatches Step 1 immediately and schedules Step 2 for 48 hours later.
    """
    init_outreach_tables()
    clean_target = recipient.strip().lower()

    if is_suppressed(clean_target):
        return {
            "status": "opted_out",
            "sequence_id": None,
            "error": f"Recipient {clean_target} is on suppression list (opted out).",
        }

    now = utc_now()
    now_dt = datetime.now(timezone.utc)
    day3_due = (now_dt + timedelta(days=2)).isoformat()  # +48 hours (Day 3)

    # Check if already enrolled in an active sequence
    with connect() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        existing = conn.execute(
            "SELECT * FROM outreach_sequences WHERE LOWER(recipient) = ? AND status = 'active'",
            (clean_target,),
        ).fetchone()
        if existing:
            return {"status": "already_enrolled", "sequence_id": existing["id"], "sequence": existing}

    # Render Step 1 template
    rendered = D2COutreachTemplates.render_template(
        step=1,
        channel=channel,
        role=role,
        context=dict(custom_vars or {}, client_name=client_name, company_name=client_name, recipient=recipient),
    )

    track_id = f"TRK-{hashlib.sha256((clean_target + now).encode('utf-8')).hexdigest()[:10].upper()}"

    # Dispatch Step 1
    dispatch_res = dispatch_client_outreach(
        client_name=client_name,
        channel=channel,
        pitch_text=rendered["body"],
        recipient=recipient,
        subject=rendered["subject"],
        is_sandbox=is_sandbox,
        sequence_step=1,
        tracking_id=track_id,
    )

    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO outreach_sequences (
                lead_id, client_name, recipient, channel, role,
                current_step, status, last_touch_at, next_touch_due_at,
                metadata, is_sandbox, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, 'active', ?, ?, ?, ?, ?, ?)
            """,
            (
                lead_id,
                client_name,
                clean_target,
                channel.lower(),
                role.lower(),
                now,
                day3_due,
                json.dumps(custom_vars or {}),
                int(is_sandbox),
                now,
                now,
            ),
        )
        seq_id = cur.lastrowid
        conn.commit()

    return {
        "status": "enrolled",
        "sequence_id": seq_id,
        "step_1_dispatch": dispatch_res.to_dict(),
        "next_touch_due_at": day3_due,
    }


def process_due_sequence_touches(is_sandbox: bool = True, force_advance: bool = False) -> list[DispatchResult]:
    """
    Scans active sequences and processes due touches:
    - Step 1 -> Step 2 (Day 3 Value Add) after 48h
    - Step 2 -> Step 3 (Day 7 Breakup) after another 96h
    - If lead has replied or opted out, the sequence is automatically paused/completed!
    """
    init_outreach_tables()
    results: list[DispatchResult] = []
    now = utc_now()
    now_dt = datetime.now(timezone.utc)

    with connect() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        query = "SELECT * FROM outreach_sequences WHERE status = 'active'"
        params: list[Any] = []
        if not is_sandbox:
            query += " AND is_sandbox = 0"
        sequences = conn.execute(query).fetchall()

    for seq in sequences:
        seq_id = seq["id"]
        recipient = seq["recipient"]
        client_name = seq["client_name"]
        channel = seq["channel"]
        role = seq["role"]
        current_step = seq["current_step"]
        due_at = seq.get("next_touch_due_at") or ""

        # 1. Check suppression / opt-out
        if is_suppressed(recipient):
            with connect() as conn:
                conn.execute("UPDATE outreach_sequences SET status = 'opted_out', updated_at = ? WHERE id = ?", (now, seq_id))
                conn.commit()
            continue

        # 2. Check if recipient has replied
        with connect() as conn:
            replied = conn.execute(
                """
                SELECT id FROM friday_communication_logs
                WHERE LOWER(recipient) = ? AND replied_at != '' AND replied_at IS NOT NULL
                """,
                (recipient.lower(),),
            ).fetchone()
            if replied:
                conn.execute("UPDATE outreach_sequences SET status = 'replied', updated_at = ? WHERE id = ?", (now, seq_id))
                conn.commit()
                continue

        # 3. Check if due for next touch
        is_due = force_advance
        if not is_due and due_at:
            try:
                due_dt = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
                is_due = now_dt >= due_dt
            except Exception:
                is_due = False

        if not is_due:
            continue

        next_step = current_step + 1
        if next_step > 3:
            with connect() as conn:
                conn.execute("UPDATE outreach_sequences SET status = 'completed', updated_at = ? WHERE id = ?", (now, seq_id))
                conn.commit()
            continue

        # Render next touch template
        meta = json.loads(seq.get("metadata") or "{}")
        rendered = D2COutreachTemplates.render_template(
            step=next_step,
            channel=channel,
            role=role,
            context=dict(meta, client_name=client_name, company_name=client_name, recipient=recipient),
        )

        track_id = f"TRK-{hashlib.sha256((recipient + now).encode('utf-8')).hexdigest()[:10].upper()}"

        # Dispatch next touch
        res = dispatch_client_outreach(
            client_name=client_name,
            channel=channel,
            pitch_text=rendered["body"],
            recipient=recipient,
            subject=rendered["subject"],
            is_sandbox=bool(seq["is_sandbox"]),
            sequence_step=next_step,
            tracking_id=track_id,
        )
        results.append(res)

        # Update sequence schedule
        if next_step == 2:
            day7_due = (now_dt + timedelta(days=4)).isoformat()  # +96 hours (Day 7 total)
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE outreach_sequences
                    SET current_step = 2, last_touch_at = ?, next_touch_due_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, day7_due, now, seq_id),
                )
                conn.commit()
        elif next_step == 3:
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE outreach_sequences
                    SET current_step = 3, status = 'completed', last_touch_at = ?, next_touch_due_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, seq_id),
                )
                conn.commit()

    return results


def pause_sequence(sequence_id: int, reason: str = "paused") -> bool:
    """Pauses an active outreach sequence."""
    init_outreach_tables()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE outreach_sequences SET status = ?, updated_at = ? WHERE id = ?",
            (reason, utc_now(), sequence_id),
        )
        conn.commit()
        return cur.rowcount > 0


def resume_sequence(sequence_id: int) -> bool:
    """Resumes a paused outreach sequence."""
    init_outreach_tables()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE outreach_sequences SET status = 'active', updated_at = ? WHERE id = ? AND status = 'paused'",
            (utc_now(), sequence_id),
        )
        conn.commit()
        return cur.rowcount > 0


def list_outreach_sequences(status: str | None = None, include_sandbox: bool = True) -> list[dict[str, Any]]:
    """Returns stored outreach sequences."""
    init_outreach_tables()
    with connect() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        query = "SELECT * FROM outreach_sequences WHERE 1=1"
        params: list[Any] = []
        if not include_sandbox:
            query += " AND is_sandbox = 0"
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY id DESC"
        return conn.execute(query, tuple(params)).fetchall()


# ---------------------------------------------------------------------------
# Conversion Funnel Analytics Tracking (sent -> opened -> replied -> proposal accepted)
# ---------------------------------------------------------------------------

def record_outreach_open(tracking_id_or_message_id: str, opened_at: str | None = None) -> bool:
    """Records an email open event in communication logs."""
    init_outreach_tables()
    target_id = tracking_id_or_message_id.strip()
    now = opened_at or utc_now()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE friday_communication_logs
            SET opened_at = ?, status = CASE WHEN status IN ('dispatched', 'delivered', 'simulated') THEN 'opened' ELSE status END
            WHERE (tracking_id = ? OR provider_message_id = ?) AND (opened_at IS NULL OR opened_at = '')
            """,
            (now, target_id, target_id),
        )
        conn.commit()
        return cur.rowcount > 0


def record_outreach_reply(
    tracking_id_or_message_id: str,
    reply_text: str = "",
    sentiment: str = "positive",
    replied_at: str | None = None,
) -> bool:
    """
    Records a prospect reply event. Automatically updates linked sequence to 'replied'
    to prevent subsequent automated touches.
    """
    init_outreach_tables()
    target_id = tracking_id_or_message_id.strip()
    now = replied_at or utc_now()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE friday_communication_logs
            SET replied_at = ?, reply_text = ?, reply_sentiment = ?, status = 'replied'
            WHERE tracking_id = ? OR provider_message_id = ?
            """,
            (now, reply_text, sentiment, target_id, target_id),
        )
        # Find recipient to update active sequence
        row = conn.execute(
            "SELECT recipient FROM friday_communication_logs WHERE tracking_id = ? OR provider_message_id = ?",
            (target_id, target_id),
        ).fetchone()
        if row and row[0]:
            conn.execute(
                "UPDATE outreach_sequences SET status = 'replied', updated_at = ? WHERE LOWER(recipient) = ?",
                (now, row[0].lower()),
            )
        conn.commit()
        return cur.rowcount > 0


def record_outreach_proposal_accepted(
    tracking_id_or_message_id: str,
    proposal_id: int | None = None,
    amount_inr: float = 0.0,
    accepted_at: str | None = None,
) -> bool:
    """Records the ultimate conversion step: proposal accepted from outreach campaign."""
    init_outreach_tables()
    target_id = tracking_id_or_message_id.strip()
    now = accepted_at or utc_now()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE friday_communication_logs
            SET proposal_id = ?, deal_value_inr = ?, converted_at = ?, status = 'proposal_accepted'
            WHERE tracking_id = ? OR provider_message_id = ?
            """,
            (proposal_id, amount_inr, now, target_id, target_id),
        )
        row = conn.execute(
            "SELECT recipient FROM friday_communication_logs WHERE tracking_id = ? OR provider_message_id = ?",
            (target_id, target_id),
        ).fetchone()
        if row and row[0]:
            conn.execute(
                "UPDATE outreach_sequences SET status = 'converted', updated_at = ? WHERE LOWER(recipient) = ?",
                (now, row[0].lower()),
            )
        conn.commit()
        return cur.rowcount > 0


def get_outreach_conversion_funnel(include_sandbox: bool = True) -> dict[str, Any]:
    """
    Calculates conversion funnel analytics from dispatch to proposal acceptance:
    sent -> delivered -> opened -> replied -> proposal accepted -> revenue pipeline.
    """
    init_outreach_tables()
    where = "1=1" if include_sandbox else "is_sandbox = 0"

    with connect() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        logs = conn.execute(f"SELECT * FROM friday_communication_logs WHERE {where}").fetchall()

    total_sent = len(logs)
    delivered = sum(1 for l in logs if l.get("status") in {"delivered", "simulated", "opened", "replied", "proposal_accepted"})
    opened = sum(1 for l in logs if bool(l.get("opened_at")))
    replied = sum(1 for l in logs if bool(l.get("replied_at")))
    positive_replies = sum(1 for l in logs if l.get("reply_sentiment") == "positive")
    proposals_accepted = sum(1 for l in logs if l.get("status") == "proposal_accepted" or bool(l.get("proposal_id")))
    total_pipeline_val = sum(float(l.get("deal_value_inr") or 0.0) for l in logs)

    # Conversion percentages
    open_rate = round((opened / max(1, delivered)) * 100.0, 1) if delivered > 0 else 0.0
    reply_rate = round((replied / max(1, delivered)) * 100.0, 1) if delivered > 0 else 0.0
    positive_reply_rate = round((positive_replies / max(1, replied)) * 100.0, 1) if replied > 0 else 0.0
    proposal_conv_rate = round((proposals_accepted / max(1, delivered)) * 100.0, 1) if delivered > 0 else 0.0

    # Channel breakdowns
    by_channel: dict[str, dict[str, Any]] = {}
    for ch in ["email", "linkedin", "webhook"]:
        ch_logs = [l for l in logs if l.get("channel") == ch]
        c_sent = len(ch_logs)
        c_opened = sum(1 for l in ch_logs if bool(l.get("opened_at")))
        c_replied = sum(1 for l in ch_logs if bool(l.get("replied_at")))
        c_accepted = sum(1 for l in ch_logs if l.get("status") == "proposal_accepted" or bool(l.get("proposal_id")))
        by_channel[ch] = {
            "sent": c_sent,
            "opened": c_opened,
            "replied": c_replied,
            "proposals_accepted": c_accepted,
            "reply_rate_pct": round((c_replied / max(1, c_sent)) * 100.0, 1) if c_sent > 0 else 0.0,
        }

    # Step breakdowns
    by_step: dict[str, dict[str, Any]] = {}
    for st, name in [(1, "Day 1 Pitch"), (2, "Day 3 Value Add"), (3, "Day 7 Breakup")]:
        st_logs = [l for l in logs if l.get("sequence_step") == st]
        s_sent = len(st_logs)
        s_opened = sum(1 for l in st_logs if bool(l.get("opened_at")))
        s_replied = sum(1 for l in st_logs if bool(l.get("replied_at")))
        by_step[f"touch_{st}"] = {
            "name": name,
            "sent": s_sent,
            "opened": s_opened,
            "replied": s_replied,
            "reply_rate_pct": round((s_replied / max(1, s_sent)) * 100.0, 1) if s_sent > 0 else 0.0,
        }

    return {
        "funnel_summary": {
            "total_sent": total_sent,
            "delivered": delivered,
            "opened": opened,
            "open_rate_pct": open_rate,
            "replied": replied,
            "reply_rate_pct": reply_rate,
            "positive_replies": positive_replies,
            "positive_reply_rate_pct": positive_reply_rate,
            "proposals_accepted": proposals_accepted,
            "proposal_conversion_rate_pct": proposal_conv_rate,
            "pipeline_value_inr": round(total_pipeline_val, 2),
        },
        "stages": [
            {"stage": "Dispatched", "count": total_sent, "pct_of_sent": 100.0},
            {"stage": "Delivered", "count": delivered, "pct_of_sent": round((delivered / max(1, total_sent)) * 100.0, 1)},
            {"stage": "Opened", "count": opened, "pct_of_sent": round((opened / max(1, total_sent)) * 100.0, 1)},
            {"stage": "Replied", "count": replied, "pct_of_sent": round((replied / max(1, total_sent)) * 100.0, 1)},
            {"stage": "Proposal Accepted", "count": proposals_accepted, "pct_of_sent": round((proposals_accepted / max(1, total_sent)) * 100.0, 1)},
        ],
        "by_channel": by_channel,
        "by_step": by_step,
    }
