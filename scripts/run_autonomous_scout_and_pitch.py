from __future__ import annotations

"""
scripts/run_autonomous_scout_and_pitch.py
SOLOEARN Fully Autonomous Scout & Outreach Daemon
=================================================
Runs unattended in the background to:
1. Poll discovery sources & dropzones for new e-commerce & SMB leads.
2. Evaluate data bottleneck fit & capability match.
3. Automatically draft consultative value pitches without human intervention.
4. Autonomously dispatch outreach via configured SMTP/Webhook transport (rate-limited).
5. Advance lead status to 'approached' and log communication records.

Usage:
    # Run a single autonomous scout & pitch cycle
    python scripts/run_autonomous_scout_and_pitch.py

    # Run in sandbox mode (simulates delivery without sending real emails)
    python scripts/run_autonomous_scout_and_pitch.py --sandbox

    # Run continuously as a background daemon (every N minutes)
    python scripts/run_autonomous_scout_and_pitch.py --loop --interval 60
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure root directory is on PYTHONPATH
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from core.autonomy_profitability_system import OpportunityEngine
from core.commercial_funnel import add_commercial_lead, list_commercial_leads, update_lead_stage
from core.config import DATA_DIR, LEADS_DROPZONE_DIR
from core.db import connect, list_discovery_sources, utc_now
from core.discovery_engine import DiscoveryEngine
from core.outreach_dispatcher import dispatch_client_outreach, get_outreach_rate_limit_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("soloearn.autonomous_scout")


def run_autonomous_scout_cycle(is_sandbox: bool = False, max_pitches_per_run: int = 5) -> dict[str, Any]:
    print("=" * 76)
    print(" SOLOEARN AUTONOMOUS SCOUT & OUTREACH ENGINE")
    print(f" Mode: {'SANDBOX (Safe Simulation)' if is_sandbox else 'LIVE PRODUCTION'}")
    print(f" Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 76)

    # 1. Check Rate Limit Status
    can_send, count_today, remaining = get_outreach_rate_limit_status(is_sandbox=is_sandbox)
    print(f"\n[RATE LIMIT CHECK] Sent today: {count_today} | Daily safety limit: 10 | Remaining capacity: {remaining}")
    if not can_send:
        print("[HALTED] Daily outreach rate limit reached. Halted to protect domain reputation and anti-spam compliance.")
        return {"status": "rate_limited", "sent_count": 0, "prospects_found": 0}

    # 2. Autonomous Lead Discovery (Poll all active discovery sources)
    print("\n[PHASE 1: DISCOVERY] Scanning authorized sources and dropzones for new leads...")
    sources = list_discovery_sources(active_only=True)
    total_found = 0
    total_imported = 0

    for s in sources:
        try:
            res = DiscoveryEngine.poll_source(s, auto_commit=True)
            total_found += res.total_found
            total_imported += res.leads_imported
            if res.leads_imported > 0:
                print(f"  --> Source '{s['name']}': Imported {res.leads_imported} new opportunities")
        except Exception as exc:
            logger.warning(f"Error polling source '{s['name']}': {exc}")

    print(f"  --> Discovery Complete: {total_found} scanned, {total_imported} newly imported.")

    # 3. Prospect Evaluation & Automated Approach
    print("\n[PHASE 2: FIT EVALUATION & AUTONOMOUS OUTREACH] Screening candidate accounts...")
    candidates = list_commercial_leads(include_sandbox=is_sandbox)
    unapproached = [c for c in candidates if c.get("stage") in ("identified", "prospective_account")]

    print(f"  --> Found {len(unapproached)} unapproached accounts in pipeline.")

    sent_count = 0
    results_summary = []

    for lead in unapproached:
        if sent_count >= max_pitches_per_run:
            print(f"\n  --> Reached per-run batch cap of {max_pitches_per_run} pitches. Remaining deferred to next cycle.")
            break

        # Check today's safety headroom
        can_send, _, remaining = get_outreach_rate_limit_status(is_sandbox=is_sandbox)
        if not can_send or remaining <= 0:
            print("  --> Daily safety cap reached. Halted further dispatches for today.")
            break

        client_name = lead.get("company_name", "Target Brand")
        contact_name = lead.get("contact_name", "Founder / Head of Operations")
        channel = lead.get("contact_channel", "email")
        service_tier = lead.get("service_tier", "data_analytics")

        # A. Assess Fit
        fit = OpportunityEngine.assess_prospect_fit({
            "company_name": client_name,
            "description": lead.get("notes") or lead.get("notes_text") or "Multi-channel retail operations",
            "budget_inr": lead.get("target_budget_inr", 5000.0),
        })

        if not fit.is_fit or fit.fit_score < 0.60:
            print(f"  [SKIP] {client_name}: Low fit score ({fit.fit_score*100:.0f}%). Rejected by capability filter.")
            continue

        # B. Check Cooldown (ensure not already contacted in last 30 days)
        with connect() as conn:
            prior = conn.execute(
                """
                SELECT id, created_at FROM friday_communication_logs
                WHERE client_name = ? AND status IN ('dispatched', 'delivered') AND is_sandbox = ?
                ORDER BY id DESC LIMIT 1
                """,
                (client_name, int(is_sandbox)),
            ).fetchone()
            if prior:
                print(f"  [SKIP] {client_name}: Contacted on {prior['created_at'][:10]}. Under active 30-day cooldown.")
                continue

        # C. Synthesize Pitch Autonomously
        print(f"\n  [PITCHING] Generating tailored approach for {client_name} ({service_tier})...")
        pitch_body = OpportunityEngine.prepare_personalized_approach(
            company_name=client_name,
            contact_name=contact_name,
            service_tier=service_tier,
            channel=channel,
        )

        # D. Autonomous Dispatch
        dispatch_res = dispatch_client_outreach(
            client_name=client_name,
            channel=channel,
            pitch_text=pitch_body,
            recipient=lead.get("contact_info", ""),
            is_sandbox=is_sandbox,
        )

        status = dispatch_res.status
        msg_id = dispatch_res.message_id or "(draft)"
        print(f"  --> Result: [{status.upper()}] via {dispatch_res.provider} | Message-ID: {msg_id}")

        if status in ("dispatched", "delivered", "simulated"):
            sent_count += 1
            # Advance lead stage in CRM
            lead_id = lead.get("id")
            if lead_id:
                try:
                    update_lead_stage(lead_id, "approached")
                except Exception:
                    pass

            results_summary.append({
                "client_name": client_name,
                "status": status,
                "provider": dispatch_res.provider,
                "message_id": msg_id,
            })
            # Polite interval between dispatches
            time.sleep(2)
        else:
            print(f"  --> Note: {dispatch_res.error_message}")

    print("\n" + "=" * 76)
    print(f" AUTONOMOUS CYCLE FINISHED: {sent_count} pitches autonomously dispatched.")
    print("=" * 76)

    return {
        "status": "success",
        "sent_count": sent_count,
        "dispatched_accounts": results_summary,
        "completed_at": utc_now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="SOLOEARN Autonomous Scout & Outreach Engine")
    parser.add_argument("--sandbox", action="store_true", help="Run in sandbox simulation mode (safe, zero external emails)")
    parser.add_argument("--loop", action="store_true", help="Run continuously in a background loop")
    parser.add_argument("--interval", type=int, default=60, help="Loop interval in minutes (default: 60)")
    parser.add_argument("--max-pitches", type=int, default=5, help="Maximum pitches to dispatch per cycle (default: 5)")
    args = parser.parse_args()

    # Enable autonomous outreach mode in environment for this process
    os.environ["SOLOEARN_AUTONOMOUS_OUTREACH"] = "true"

    if args.loop:
        print(f"Starting Autonomous Daemon loop (polling every {args.interval} minutes). Press Ctrl+C to stop.")
        while True:
            try:
                run_autonomous_scout_cycle(is_sandbox=args.sandbox, max_pitches_per_run=args.max_pitches)
                print(f"\nSleeping for {args.interval} minutes until next autonomous scout cycle...")
                time.sleep(args.interval * 60)
            except KeyboardInterrupt:
                print("\nDaemon stopped by user.")
                break
            except Exception as e:
                logger.exception(f"Unhandled error in autonomous scout cycle: {e}")
                time.sleep(60)
    else:
        res = run_autonomous_scout_cycle(is_sandbox=args.sandbox, max_pitches_per_run=args.max_pitches)
        return 0 if res["status"] in ("success", "rate_limited") else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
