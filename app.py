from __future__ import annotations

import math
import os
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import streamlit as st

from core.analytics import create_analytics_bundle
from core.autonomous_worker import run_autonomous_job
from core.commercial_funnel import (
    REAL_SMB_PILOT_TARGETS,
    add_commercial_lead,
    calculate_project_unit_economics,
    get_commercial_performance_dashboard,
    get_commercial_pilot_milestones,
    get_funnel_metrics,
    init_commercial_tables,
    list_commercial_leads,
    seed_real_smb_pilot_leads,
    update_lead_stage,
)
from core.config import (
    DATA_DIR,
    MAX_UPLOAD_MB,
    OLLAMA_MODEL,
    PROJECT_ROOT,
    WORKSPACES_DIR,
    ensure_directories,
    safe_workspace_path,
)
from core.upi_engine import (
    build_upi_payment_url,
    generate_invoice_upi_qr,
    generate_upi_qr_bytes,
    get_active_upi_settings,
    get_upi_payment_centre_summary,
    process_merchant_bank_webhook,
    reconcile_upi_payment_from_bank_credit,
    save_upi_settings,
    scan_and_match_statement_for_upi,
)
from core.preflight import (
    get_kotak_onboarding_guide,
    run_m7_local_preflight,
)
from core.db import (
    VALID_OPPORTUNITY_TRANSITIONS,
    add_discovery_source,
    add_opportunity,
    add_revenue,
    complete_revision,
    complete_task,
    create_invoice,
    create_revision_request,
    create_task,
    delete_discovery_source,
    get_discovery_source,
    get_invoice,
    get_opportunity,
    get_proposal,
    init_db,
    list_discovery_sources,
    list_invoices,
    list_opportunities,
    list_proposals,
    list_revisions,
    list_revenue,
    list_tasks,
    log_event,
    metrics,
    recent_audit,
    save_proposal_draft,
    update_opportunity_status,
    verify_invoice_payment,
    get_active_tax_profile,
    get_bank_opening_balance,
    set_bank_opening_balance,
    upsert_tax_profile,
    log_owner_time,
    list_owner_time_logs,
    get_owner_time_summary,
    get_financial_states_summary,
)
from core.discovery_engine import DiscoveryEngine
from core.invoice_generator import render_invoice_html
from core.llm import OllamaClient, draft_proposal
from core.opportunity_import import (
    commit_imported_opportunities,
    parse_and_validate_opportunities_csv,
)
from core.proposal_generator import authorize_proposal, draft_tailored_proposal, generate_proposal_template
from core.revenue_optimizer import (
    MONTHLY_REVENUE_TARGET_INR,
    SERVICE_CATALOG,
    RevenueOptimizer,
)
from core.financial_ledger import (
    get_cash_and_reserves_summary,
    get_expenses_summary,
    import_bank_statement_csv,
    list_expenses,
    list_ledger_transactions,
    list_unreconciled_statement_rows,
    reconcile_statement_row,
    register_expense,
)
from core.net_profit_optimizer import (
    MONTHLY_BUFFER_TARGET_INR,
    MONTHLY_NET_PROFIT_TARGET_INR,
    MONTHLY_SAFETY_BUFFER_TARGET_INR,
    NetProfitOptimizer,
    SERVICE_PROFIT_SPECS,
)
from core.tax_engine import (
    GST_SERVICE_THRESHOLD_INR,
    GST_WARNING_THRESHOLD_INR,
    SECTION_393_TDS_SCHEDULE,
    compute_income_tax_india_2026,
    compute_required_pretax_profit,
    get_gst_status,
)
from core.scheduler import AutonomousDaemonScheduler
from core.scoring import opportunity_score, score_label

st.set_page_config(page_title="SOLOEARN — Private AI Workbench", page_icon="🧠", layout="wide")
ensure_directories()
init_db()

st.title("🧠 SOLOEARN")
st.caption("SOLOEARN v0.6.1.2 · private, local-first income-workflow workbench · single owner · zero paid API dependency")
st.warning(
    "This is an assisted-work MVP, not an unattended money machine. It does not scrape marketplaces, "
    "send proposals, accept contracts, submit work, make purchases, or access bank credentials. "
    "Only record money that has actually been paid as Paid."
)

client = OllamaClient()
ollama_ok, installed_models, ollama_message = client.status()
with st.sidebar:
    st.header("Local runtime")
    st.write("**Ollama:**", "Connected" if ollama_ok else "Not connected")
    st.caption(ollama_message)
    st.write("**Configured model:**", OLLAMA_MODEL)
    if ollama_ok:
        st.write("**Installed models:**")
        st.caption(", ".join(installed_models) if installed_models else "No local models pulled yet")
    st.divider()
    st.write("**Safety mode:** Approval required")
    st.write("**Workspace:** `workspaces/` only")
    st.write(f"**CSV limit:** {MAX_UPLOAD_MB} MB")
    st.caption("No cloud endpoint is configured by this app.")

m = metrics()
cols = st.columns(4)
cols[0].metric("Confirmed paid", f"₹{m['paid_inr']:,.0f}")
cols[1].metric("Expected / invoiced", f"₹{m['pipeline_inr']:,.0f}")
cols[2].metric("Opportunities", m["opportunities"])
cols[3].metric("Completed tasks", m["completed_tasks"])

tabs = st.tabs([
    "Overview",
    "Commercial Hub & Acquisition (M6.3)",
    "Autonomous Discovery (M4)",
    "Opportunity Pipeline",
    "Revenue Operations & Invoicing (M5)",
    "Net Profit Control Centre (M6)",
    "UPI Payment Centre (M7)",
    "CSV Workbench",
    "Earnings & audit",
])

# ----------------- Tab 0: Overview -----------------
with tabs[0]:
    st.subheader("What this version can do")
    left, right = st.columns(2)
    with left:
        st.markdown("**Working now**")
        st.markdown(
            "- **UPI Payment Centre & Dynamic QR Engine (Milestone 7)**: Direct zero-fee UPI collections via standard NPCI dynamic QR/Intent, automated bank statement matching, and fail-closed financial safeguards\n"
            "- **Autonomous Opportunity Discovery (Milestone 4)**: Proactive RSS/Atom XML & JSON API lead intake with zero paid subscriptions\n"
            "- **Capability Matching**: Automated screening against verified analytics capabilities (CSV cleaning, Excel, charts, reports)\n"
            "- **Fraud & Risk Scoring**: Identifies Telegram, crypto scams, unrealistic budgets, and payment bypass attempts\n"
            "- **Tailored Proposals & Authorization Gate**: Generates non-hallucinatory client proposals with explicit owner authorization\n"
            "- **Multi-Level Deduplication**: Fingerprints URLs and content hashes to eliminate duplicates\n"
            "- **Autonomous Execution Engine**: 7-step plan execution in isolated workspaces\n"
            "- **Independent QA Agent**: 6-suite verification (Excel, formulas, math consistency, PNG binaries, HTML)\n"
            "- **Self-Healing Revision Engine**: Targeted automated repairs capped at max 3 retries\n"
            "- **Financial Ledger**: Confirmed-paid tracking gated on owner submission"
        )
    with right:
        st.markdown("**Intentionally not automated**")
        st.markdown(
            "- No aggressive website scraping or platform-rule evasion\n"
            "- No automatic proposal dispatch without explicit owner authorization\n"
            "- No contract acceptance or unreviewed deliverable submissions\n"
            "- No bank login, money transfer, or purchasing capabilities\n"
            "- No unrestricted computer/file access"
        )
    st.subheader("Recent work")
    tasks = list_tasks()[:8]
    if tasks:
        st.dataframe(pd.DataFrame(tasks)[["id", "title", "task_type", "status", "created_at"]], use_container_width=True, hide_index=True)
    else:
        st.info("No tasks yet. Try the CSV Workbench or Autonomous Discovery.")


# ----------------- Tab 1: Commercial Hub & Client Acquisition (M6.3) -----------------
with tabs[1]:
    st.subheader("🎯 Commercial Hub & First Customer Acquisition Pilot (Milestone 6.3)")
    st.caption("Operational playbook for acquiring the first paying pilot customer through a controlled 5-prospect experiment. All prices quoted strictly ex-GST.")

    st.info(
        "**SOLOEARN v0.6.3 Commercial Pilot Readiness: 7.5 / 10**\n\n"
        "• **Technical Automation (10/10)**: 171 automated unit & integration tests passing on Windows; zero skips/errors in revenue loops.\n"
        "• **Statutory Tax & Accounting (10/10)**: Section 202 slabs, Section 156 rebate, Section 393 Table 6 payee-specific TDS, and Section 276 cash-method accounting verified.\n"
        "• **Market Proof & Paying Clients (0/10)**: Feature freeze active. Zero live paying clients acquired yet.\n"
        "• **Path to 10/10 Readiness**: Execute outreach to the 5 prospective accounts below, secure first paid pilot, deliver work, and reconcile receipt against bank statement."
    )

    with st.expander("🚀 M6.3 First Commercial Experiment: 5 Genuine Businesses (Click to View Outreach Scripts)"):
        st.markdown(
            "**Operational Goal:** Approach 5 real SMB businesses (small e-commerce sellers/retailers), offer a tightly-scoped "
            "**₹5,000 ex-GST Tier 1 Analytics Pack** using the verified sample deliverables in `workspaces/task_0469/deliverables.zip` as demonstration, "
            "and log actual collected money, platform fees, direct costs, and owner time across all 5 categories.\n\n"
            "📖 Full Playbook: [`commercial/M6_3_FIRST_CUSTOMER_ACQUISITION_PLAYBOOK.md`](file:///C:/Users/BHAVESH%20KHULLAR/Downloads/SOLOEARN_v0.1/SOLOEARN/commercial/M6_3_FIRST_CUSTOMER_ACQUISITION_PLAYBOOK.md)"
        )
        st.markdown("#### 🏢 5 Researched Real-World Indian D2C & Retail Targets")
        st.caption("Researched Indian consumer brands categorized into Strategic Accounts and Initial Pilot Prospects. Outreach pitches use a question-led, humble, non-presumptive script.")

        account_group = st.radio(
            "Account Group",
            options=["All Accounts (5)", "Strategic Accounts (3)", "Initial Pilot Prospects (2)"],
            horizontal=True,
            key="real_target_group_radio",
        )
        if "Strategic" in account_group:
            filtered_targets = [t for t in REAL_SMB_PILOT_TARGETS if t.get("account_classification") == "Strategic Account"]
        elif "Initial" in account_group:
            filtered_targets = [t for t in REAL_SMB_PILOT_TARGETS if t.get("account_classification") == "Initial Pilot Prospect"]
        else:
            filtered_targets = REAL_SMB_PILOT_TARGETS

        target_names = [t["company_name"] for t in filtered_targets]
        selected_target_name = st.selectbox("Select Researched Business Target", options=target_names, key="real_target_select")
        selected_target = next(t for t in filtered_targets if t["company_name"] == selected_target_name)

        rt_c1, rt_c2 = st.columns([1, 1])
        with rt_c1:
            st.markdown(
                f"- **Account Tier:** `{selected_target.get('account_classification', 'Prospective Account')}`\n"
                f"- **Verified Co-Founders:** {selected_target.get('verified_founders', selected_target['contact_name'])}\n"
                f"- **Primary Contact:** {selected_target['contact_name']} ({selected_target['contact_role']})\n"
                f"- **Headquarters:** {selected_target['city']}\n"
                f"- **Channel:** `{selected_target['contact_channel']}` ({selected_target['contact_handle']})\n"
                f"- **Official Website:** [{selected_target['website']}]({selected_target['website']})\n"
                f"- **Products & Channels:** {selected_target['products']}\n"
            )
        with rt_c2:
            st.markdown(
                f"- **Recommended Offering:** `{selected_target['service_tier']}` (@ ₹{selected_target['proposed_price_inr']:,.0f} ex-GST)\n"
                f"- **Research Context:** {selected_target['notes']}\n"
            )
            st.warning(
                f"⚠️ **{selected_target.get('hypothesis_label', 'Unverified Prospecting Hypothesis — Pending Discovery Conversation')}**:\n\n"
                f"{selected_target.get('pain_point_hypothesis', selected_target.get('pain_point', ''))}\n\n"
                f"*(Note: This is an unverified prospecting hypothesis based on public multi-channel retail expansion, not an established company admission. Use the question-led script below to validate during discovery.)*"
            )

        st.text_area(
            f"Question-Led Outreach Pitch for {selected_target['contact_name']} ({selected_target['company_name']})",
            value=selected_target["custom_pitch"],
            height=220,
            key=f"pitch_text_{selected_target_name}",
        )

        if st.button("📥 Import These 5 Researched Indian D2C Targets Into Commercial Pipeline"):
            added_cnt = seed_real_smb_pilot_leads()
            st.success(f"Successfully imported {added_cnt} new verified commercial leads into your pipeline!")
            st.rerun()

        st.divider()

        sample_script = st.selectbox(
            "Select General Outreach Script Template to Copy",
            options=[
                "Script A: Shopify / D2C Founder (Weekly Reporting)",
                "Script B: Amazon / Multi-Channel Seller (Channel Reconciliation)",
                "Script C: Retail / Wholesale Merchant (Order Pacing)",
                "Script D: Accounting / Finance Lead (CSV Deduplication & QA)",
                "Script E: Warm Network / Referral",
            ],
        )
        scripts_map = {
            "Script A: Shopify / D2C Founder (Weekly Reporting)": (
                "Hi [Name],\n\nI saw you're leading [Brand Name]—great work on the recent [product line/milestone].\n\n"
                "Quick question: how much time does your team spend manually pulling and cleaning Shopify/gateway CSVs every week for executive sales reporting?\n\n"
                "I build automated analytics and reporting models for D2C brands. In 48 hours, I turn raw e-commerce export CSVs into an executive interactive HTML dashboard and an Excel model with automated charts and customer regional breakdowns.\n\n"
                "I have a live sample report and 3-sheet model I can send over if you'd like to take a look. No commitment—just wanted to share how other founders are eliminating 4+ hours of manual spreadsheet work every week.\n\n"
                "Best,\n[Your Name]\nSolo Founder, SOLOEARN"
            ),
            "Script B: Amazon / Multi-Channel Seller (Channel Reconciliation)": (
                "Hi [Name],\n\nManaging sales across Amazon, Flipkart, and your own website usually means drowning in different CSV formats and trying to figure out which channel actually generates the highest net margins.\n\n"
                "I help e-commerce sellers automate their multi-channel sales and reconciliation reporting. I deliver:\n"
                "1. Normalized, cleaned sales CSVs with standardized columns.\n"
                "2. A multi-tab Excel model with automatic channel and return summaries.\n"
                "3. An executive HTML dashboard showing pacing and top metro hubs.\n\n"
                "Would you be open to seeing a 2-minute sample report built on sample e-commerce data?\n\n"
                "Best regards,\n[Your Name]"
            ),
            "Script C: Retail / Wholesale Merchant (Order Pacing)": (
                "Hello [Name],\n\nHope you're having a productive week.\n\n"
                "I work with growing retail businesses to turn weekly sales orders into clear visual dashboards. Instead of static invoices, you get an interactive dashboard showing daily order pacing, payment method mix, and top customer regions—delivered in under 48 hours.\n\n"
                "Would it be helpful if I shared a sample Excel and dashboard package so you can see how it looks?\n\n"
                "Regards,\n[Your Name]"
            ),
            "Script D: Accounting / Finance Lead (CSV Deduplication & QA)": (
                "Hi [Name],\n\nCleaning messy customer CSVs, fixing formula errors, and deduplicating customer statements is one of the biggest time-sinks in month-end financial reviews.\n\n"
                "I run a specialized analytics service that provides sanitized CSV data, formula-checked Excel workbooks, and independent QA verification reports within 48 hours.\n\n"
                "Happy to share a sample deliverable package if you're exploring ways to streamline your monthly data cleanup.\n\n"
                "Best,\n[Your Name]"
            ),
            "Script E: Warm Network / Referral": (
                "Hi [Name],\n\nI recently launched a fast-turnaround business analytics service that converts messy sales and transaction data into executive Excel models and interactive dashboards.\n\n"
                "I'm currently taking on two pilot clients at an introductory rate (₹5,000 ex-GST) to build verified case studies. If [Brand Name] or anyone in your network needs a clean, professional sales dashboard delivered this week, I'd love to help out.\n\n"
                "Here's a quick link to a sample report: [Link / Attached PDF]\n\n"
                "Thanks!\n[Your Name]"
            ),
        }
        st.text_area("Personalized Message Template (Customize before sending)", value=scripts_map[sample_script], height=180)

    # Section 1: Commercial Performance Dashboard (7 Core Metrics)
    st.markdown("### 📊 Commercial Performance Dashboard")
    st.caption("Distinguishes genuine acquisition activity, confirmed work, cash collected, and net take-home earnings. Never counts an interested customer or unpaid invoice as profit.")

    cp_dash = get_commercial_performance_dashboard()
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("1. Prospective Accounts", cp_dash["prospective_accounts"], "Researched public leads")
    d2.metric("2. Qualified Leads", cp_dash["qualified_leads"], "Confirmed pain point & budget")
    d3.metric("3. Proposals Sent", cp_dash["proposals_sent"], "Actual acquisition activity")
    d4.metric("4. Contracts Won", cp_dash["contracts_won"], "Confirmed customer work")

    d5, d6, d7, d8 = st.columns(4)
    d5.metric("5. Payments Collected", f"₹{cp_dash['payments_collected_inr']:,.2f}", "Actual bank cash received (ex-GST)")
    d6.metric("6. Project Contribution", f"₹{cp_dash['project_contribution_inr']:,.2f}", "Revenue after direct costs")
    d7.metric("7. Net Profit After Tax", f"₹{cp_dash['net_profit_after_tax_inr']:,.2f}", "Real take-home earnings")
    d8.metric("8. Owner Hours Invested", f"{cp_dash['owner_hours']} hrs", "Across 5 labor categories")

    with st.expander("🎯 Milestone 6.4: Commercial Validation Targets"):
        st.markdown(
            "The objective of Milestone 6.4 is to prove that SOLOEARN can repeatedly earn money in the real market:\n"
            f"- **5 genuine prospective businesses** (Current: {cp_dash['m64_validation_targets']['prospective_businesses_current']} / 5)\n"
            f"- **3 serious conversations** (Current: {cp_dash['m64_validation_targets']['serious_conversations_current']} / 3)\n"
            f"- **First paid customer** (Current: {'✅ Achieved' if cp_dash['m64_validation_targets']['first_paid_customer_current'] else 'Pending'})\n"
            f"- **3 completed paid pilots** (Current: {cp_dash['m64_validation_targets']['completed_paid_pilots_current']} / 3)\n"
            f"- **At least 2 repeat customers** (Current: {cp_dash['m64_validation_targets']['repeat_customers_current']} / 2)\n"
            "- **Verified project-level profit and delivery time tracking**\n"
        )

    # First-Customer Acquisition Scoreboard (Pilot Progress 0/7)
    st.markdown("### 🏆 First-Customer Acquisition Scoreboard")
    sb_data = cp_dash.get("scoreboard", {})
    score = sb_data.get("score", 0)
    total = sb_data.get("total", 7)
    st.write(f"**Your Pilot Progress: {score} / {total}** ({int(score / total * 100)}% complete)")
    st.progress(score / total)

    sb_cols = st.columns(2)
    items = sb_data.get("items", [])
    midpoint = (len(items) + 1) // 2
    with sb_cols[0]:
        for item in items[:midpoint]:
            icon = "✅" if item["completed"] else "⬜"
            st.markdown(f"{icon} **{item['title']}**")
    with sb_cols[1]:
        for item in items[midpoint:]:
            icon = "✅" if item["completed"] else "⬜"
            st.markdown(f"{icon} **{item['title']}**")

    with st.expander("🏆 Commercial Pilot Progression Milestones (Market Evidence Tracker)"):
        st.caption("Uncompromising market evidence: No milestone is declared achieved without verified bank deposits, invoices, or expense records.")
        milestones = get_commercial_pilot_milestones()
        for m in milestones:
            m_icon = "✅" if m["achieved"] else "⏳"
            status_text = "Verified in Financial Ledger" if m["achieved"] else "Pending Market Evidence"
            st.markdown(f"{m_icon} **{m['milestone']}** — *{m['evidence_required']}* (`{m['current_metric']}` · **{status_text}**)")

    st.divider()

    # 50/30/5/3 Campaign Progress Metrics
    f_metrics = get_funnel_metrics()
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric(
            "1. Identified Prospects",
            f"{f_metrics['identified_count']} / {f_metrics['identified_target']}",
            f"{f_metrics['identified_progress_pct']}% of goal",
        )
        st.progress(min(1.0, f_metrics['identified_progress_pct'] / 100.0))
    with c2:
        st.metric(
            "2. Personalized Approaches",
            f"{f_metrics['approached_count']} / {f_metrics['approached_target']}",
            f"{f_metrics['approached_progress_pct']}% of goal",
        )
        st.progress(min(1.0, f_metrics['approached_progress_pct'] / 100.0))
    with c3:
        st.metric(
            "3. In Conversation",
            f"{f_metrics['in_conversation_count']} / {f_metrics['in_conversation_target']}",
            f"{f_metrics['in_conversation_progress_pct']}% of goal",
        )
        st.progress(min(1.0, f_metrics['in_conversation_progress_pct'] / 100.0))
    with c4:
        st.metric(
            "4. Paid Pilot Clients",
            f"{f_metrics['paid_pilot_count']} / {f_metrics['paid_pilot_target']}",
            f"{f_metrics['paid_pilot_progress_pct']}% of goal",
        )
        st.progress(min(1.0, f_metrics['paid_pilot_progress_pct'] / 100.0))

    st.divider()

    # Section 2: Unit Economics & Quote Calculator
    st.markdown("### 🧮 Project Unit Economics & Profit Yield Calculator")
    st.caption("Calculates exact owner take-home profit after payment gateway fees, compute costs, and statutory Indian income tax.")

    ue_col1, ue_col2 = st.columns([1, 1])
    with ue_col1:
        calc_service = st.selectbox(
            "Select Service Offering",
            options=[
                ("Tier 1: Business Analytics & Excel Pack (Pilot)", 5000.0, 5.0, 150.0, 2.0),
                ("Tier 2: CSV Reconciliation Automation", 10000.0, 5.0, 250.0, 3.0),
                ("Tier 3: Data Engineering ETL Pipeline", 15000.0, 5.0, 500.0, 5.0),
                ("Tier 4: Executive Interactive Dashboard", 20000.0, 5.0, 600.0, 6.0),
            ],
            format_func=lambda x: f"{x[0]} (₹{x[1]:,.0f})",
        )
        quote_price = st.number_input("Gross Quote to Client (₹)", min_value=100.0, value=float(calc_service[1]), step=500.0)
        est_hours = st.number_input("Estimated Delivery Effort (Hours)", min_value=0.5, value=float(calc_service[4]), step=0.5)
        platform_fee = st.number_input("Payment Gateway / Platform Fee (%)", min_value=0.0, max_value=30.0, value=float(calc_service[2]), step=0.5)
        direct_cost = st.number_input("Direct Workspace / Compute Costs (₹)", min_value=0.0, value=float(calc_service[3]), step=50.0)

    with ue_col2:
        try:
            econ = calculate_project_unit_economics(
                gross_price_inr=quote_price,
                platform_fee_pct=platform_fee,
                direct_cost_inr=direct_cost,
                estimated_hours=est_hours,
            )
            st.markdown("#### Profit & Return Breakdown")
            kpi_c1, kpi_c2 = st.columns(2)
            kpi_c1.metric("Net Pre-Tax Margin", f"{econ['contribution_margin_pct']}%", f"₹{econ['net_pretax_receipt_inr']:,.0f}")
            kpi_c2.metric("Net Owner Profit", f"₹{econ['net_owner_profit_inr']:,.0f}")

            kpi_c3, kpi_c4 = st.columns(2)
            kpi_c3.metric("Tax Reserve", f"₹{econ['estimated_tax_reserve_inr']:,.0f}", f"Rate: {econ['effective_tax_rate_pct']}%")
            kpi_c4.metric("Hourly Yield", f"₹{econ['hourly_yield_inr']:,.0f} / hr", f"over {econ['estimated_hours']} hrs")

            if econ['net_owner_profit_inr'] <= 0:
                st.error("⚠️ This quote results in negative net owner profit after costs and taxes. Increase client price.")
            elif econ['contribution_margin_pct'] < 70.0:
                st.warning("⚠️ Contribution margin is below recommended 70% threshold.")
            else:
                st.success("✅ Profitable quote meeting SOLOEARN unit economic thresholds.")
        except Exception as e:
            st.error(f"Error calculating unit economics: {e}")

    st.divider()

    # Section 3: Prospect Management (50/30/5/3 Pipeline)
    st.markdown("### 📋 Prospect & Client Pipeline Management")
    lead_col1, lead_col2 = st.columns([1, 1])

    with lead_col1:
        st.markdown("#### ➕ Add Qualified Prospect")
        with st.form("add_commercial_lead_form"):
            c_name = st.text_input("Company / Brand Name", placeholder="e.g. Apex D2C Apparel")
            c_contact = st.text_input("Contact Person & Role", placeholder="e.g. Rohan Mehra (Founder / Head of Growth)")
            c_channel = st.selectbox("Outreach Channel", options=["linkedin", "email", "phone_whatsapp", "direct_network", "freelance_platform", "other"], format_func=lambda x: x.replace("_", " ").title())
            c_handle = st.text_input("Handle / Email / Profile URL", placeholder="e.g. linkedin.com/in/rohanmehra")
            c_ind = st.selectbox("Industry", options=["ecommerce", "retail", "wholesale", "accounting", "services", "other"], format_func=lambda x: x.title())
            c_tier = st.selectbox("Target Service Package", options=["data_analytics", "csv_reconciliation", "data_engineering", "interactive_dashboard"], format_func=lambda x: x.replace("_", " ").title())
            c_price = st.number_input("Proposed Pilot Price (₹)", min_value=500.0, value=5000.0, step=500.0)
            c_stage = st.selectbox("Initial Funnel Stage", options=["identified", "approached", "in_conversation", "paid_pilot"])
            c_notes = st.text_area("Initial Assessment Notes", placeholder="e.g. Active Shopify store; estimated 100 orders/day; handles reporting manually.")

            lead_submitted = st.form_submit_button("Add Prospect to Pipeline", use_container_width=True)
            if lead_submitted:
                if not c_name or not c_contact:
                    st.error("Company Name and Contact Person are required.")
                else:
                    new_id = add_commercial_lead(
                        company_name=c_name,
                        contact_name=c_contact,
                        contact_channel=c_channel,
                        contact_handle=c_handle,
                        industry=c_ind,
                        service_tier=c_tier,
                        proposed_price_inr=c_price,
                        stage=c_stage,
                        notes=c_notes,
                    )
                    st.success(f"Added prospect #{new_id}: {c_name} to '{c_stage}' stage.")
                    st.rerun()

    with lead_col2:
        st.markdown("#### 🔄 Transition Lead Stage")
        leads_list = list_commercial_leads()
        if leads_list:
            lead_options = {l["id"]: f"#{l['id']} - {l['company_name']} ({l['contact_name']}) [{l['stage'].title()}]" for l in leads_list}
            selected_lead_id = st.selectbox("Select Prospect", options=list(lead_options.keys()), format_func=lambda x: lead_options[x])
            sel_lead = next(l for l in leads_list if l["id"] == selected_lead_id)

            with st.form("update_lead_stage_form"):
                target_stage = st.selectbox("Update Stage To", options=["identified", "approached", "in_conversation", "paid_pilot", "delivered", "lost"], index=["identified", "approached", "in_conversation", "paid_pilot", "delivered", "lost"].index(sel_lead["stage"]))
                stage_notes = st.text_area("Update Notes / Conversation Log", value=sel_lead.get("notes", "") or "")
                update_submitted = st.form_submit_button("Update Funnel Stage", use_container_width=True)
                if update_submitted:
                    update_lead_stage(selected_lead_id, target_stage, notes=stage_notes)
                    st.success(f"Prospect #{selected_lead_id} updated to '{target_stage}'.")
                    st.rerun()
        else:
            st.info("No prospects registered yet. Use the form on the left to add your first qualified prospect.")

    st.markdown("#### 📊 Active Commercial Pipeline")
    if leads_list:
        df_leads = pd.DataFrame(leads_list)[["id", "company_name", "contact_name", "contact_channel", "industry", "proposed_price_inr", "stage", "updated_at"]]
        st.dataframe(df_leads, use_container_width=True, hide_index=True)
    else:
        st.caption("No leads in pipeline.")

    st.divider()

    # Section 4: Verified Pilot Deliverables Showcase
    st.markdown("### 📦 Verified Sample Deliverables Showcase (Task #0469)")
    st.caption("Demonstrates the verified 5-asset client deliverable package produced by SOLOEARN from e-commerce sales data in 2.14 seconds.")

    deliv_ws = Path(PROJECT_ROOT) / "workspaces" / "task_0469"
    if deliv_ws.exists():
        show_c1, show_c2, show_c3 = st.columns(3)
        show_c1.markdown("**1. Cleaned CSV & Excel**\n- `cleaned_data.csv` (12 rows, formula-sanitized)\n- `business_analysis.xlsx` (3 sheets, auto-filters)")
        show_c2.markdown("**2. Visual Charts Suite**\n- 6 distribution charts in `charts/`\n- Metro sales, price distributions, order pacing")
        show_c3.markdown("**3. Executive Report**\n- `report.html` (Standalone, zero-cloud)\n- `deliverables.zip` (104 KB verified package)")

        zip_file = deliv_ws / "deliverables.zip"
        if zip_file.exists():
            st.download_button(
                "⬇️ Download Sample Client Deliverables (deliverables.zip)",
                data=zip_file.read_bytes(),
                file_name="SOLOEARN_Pilot_Deliverables_Sample.zip",
                mime="application/zip",
                use_container_width=True,
            )
    else:
        st.info("Run `python scripts/run_sample_job_verification.py` to regenerate the verified sample deliverable package.")

    # Section 5: Owner Time & Operational Labor Tracking (5 Categories)
    st.divider()
    st.markdown("### ⏱️ Owner Labor & Time Tracking (5 Operational Categories)")
    st.caption("Delivery is only one component of solo operations. Track all 5 operational categories to monitor actual capacity.")

    time_summary = get_owner_time_summary()
    ot1, ot2, ot3, ot4, ot5 = st.columns(5)
    ot1.metric("1. Delivery", f"{time_summary['delivery_hours']} hrs")
    ot2.metric("2. Prospecting", f"{time_summary['prospecting_hours']} hrs")
    ot3.metric("3. Communication", f"{time_summary['client_communication_hours']} hrs")
    ot4.metric("4. Revisions", f"{time_summary['revisions_hours']} hrs")
    ot5.metric("5. Administration", f"{time_summary['administration_hours']} hrs")
    st.write(f"**Total Owner Time Logged:** **{time_summary['total_hours']} hours**")

    with st.expander("➕ Log Owner Working Hours"):
        with st.form("log_owner_time_form", clear_on_submit=True):
            t_cat = st.selectbox(
                "Activity Category",
                options=["delivery", "prospecting", "client_communication", "revisions", "administration"],
                format_func=lambda x: x.replace("_", " ").title(),
            )
            t_hours = st.number_input("Hours Spent", min_value=0.25, max_value=24.0, value=1.0, step=0.25)
            t_desc = st.text_input("Description / Notes", placeholder="e.g. Scoping call with client; normalizing CSV headers; preparing quarterly advance tax")
            t_sub = st.form_submit_button("Log Hours")
            if t_sub:
                try:
                    log_id = log_owner_time(category=t_cat, hours=t_hours, description=t_desc)
                    st.success(f"Logged {t_hours} hrs under {t_cat.replace('_', ' ').title()} (Log #{log_id}).")
                    st.rerun()
                except Exception as ex:
                    st.error(f"Error logging time: {ex}")


# ----------------- Tab 2: Autonomous Discovery (Milestone 4) -----------------
with tabs[2]:
    st.subheader("📡 Autonomous Opportunity Discovery (Milestone 4)")
    st.caption("Proactively discovers, screens, and ranks freelance leads from authorized RSS/Atom feeds, JSON APIs, and local dropzones.")

    col_disc1, col_disc2 = st.columns([1, 1])

    with col_disc1:
        st.markdown("#### 1. Registered Discovery Sources")
        sources = list_discovery_sources()
        if not sources:
            st.info("No discovery sources registered yet.")
            if st.button("Load Bundled Sample Feeds (RSS & JSON)", key="load_sample_feeds"):
                sample_rss = PROJECT_ROOT / "data" / "sample_feeds" / "sample_rss.xml"
                sample_json = PROJECT_ROOT / "data" / "sample_feeds" / "sample_leads.json"
                if sample_rss.exists():
                    add_discovery_source("Sample Analytics RSS Feed", "rss", str(sample_rss), is_permitted=True)
                if sample_json.exists():
                    add_discovery_source("Sample Partner JSON Leads", "json_api", str(sample_json), is_permitted=True)
                st.success("Sample discovery feeds registered successfully.")
                st.rerun()
        else:
            source_summary = [
                {
                    "ID": s["id"],
                    "Name": s["name"],
                    "Type": s["source_type"].upper(),
                    "Permitted": "Yes" if s["is_permitted"] else "No",
                    "Active": "Yes" if s["is_active"] else "No",
                    "Last Polled": s["last_polled_at"] or "Never",
                }
                for s in sources
            ]
            st.dataframe(pd.DataFrame(source_summary), use_container_width=True, hide_index=True)

            del_id = st.selectbox(
                "Select source to delete",
                [s["id"] for s in sources],
                format_func=lambda i: next(s["name"] for s in sources if s["id"] == i),
                key="sel_del_src",
            )
            if st.button("Delete Selected Source", key="btn_del_src"):
                delete_discovery_source(del_id)
                st.success(f"Source #{del_id} deleted.")
                st.rerun()

        st.markdown("#### 2. Trigger Discovery Run")
        poll_active = st.button("🚀 Discover & Screen Opportunities Now", type="primary", key="btn_poll_active")
        if poll_active:
            if not sources:
                st.warning("Please register at least one authorized source above first.")
            else:
                all_batches = []
                with st.spinner("Polling authorized feeds, matching capabilities, analyzing risk, and deduplicating…"):
                    for s in sources:
                        if s["is_active"] and s["is_permitted"]:
                            batch = DiscoveryEngine.poll_source(s, auto_commit=True)
                            all_batches.append(batch)
                total_found = sum(b.total_found for b in all_batches)
                total_imported = sum(b.leads_imported for b in all_batches)
                total_dup = sum(b.duplicates_skipped for b in all_batches)
                total_unsuitable = sum(b.unsuitable_skipped for b in all_batches)
                st.success(
                    f"Discovery run complete! Total Found: {total_found} | "
                    f"Novel Imported: {total_imported} | Duplicates: {total_dup} | Unsuitable/High Risk: {total_unsuitable}"
                )
                st.rerun()

    with col_disc2:
        st.markdown("#### ➕ Add New Permitted Discovery Source")
        with st.form("add_source_form", clear_on_submit=True):
            src_name = st.text_input("Source Name", placeholder="e.g., Client Analytics RSS Feed")
            src_type = st.selectbox("Source Type", ["rss", "atom", "json_api", "local_folder"])
            src_target = st.text_input("Feed URL or Local Path", placeholder="https://example.com/feed.xml or C:/path/to/leads.json")
            src_poll_mins = st.number_input("Poll Interval (minutes)", min_value=5, value=60, step=15)
            src_permitted = st.checkbox("I confirm this source is authorized, legal, and permitted for automated polling", value=True)
            submit_src = st.form_submit_button("Register Source")
            if submit_src:
                if not src_name.strip() or not src_target.strip():
                    st.error("Source name and URL/path are required.")
                else:
                    try:
                        new_sid = add_discovery_source(
                            name=src_name.strip(),
                            source_type=src_type,
                            url_or_path=src_target.strip(),
                            is_permitted=src_permitted,
                            is_active=True,
                            poll_interval_minutes=src_poll_mins,
                        )
                        st.success(f"Registered discovery source #{new_sid}: {src_name}")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Failed to register source: {exc}")

    st.markdown("---")
    st.subheader("✍️ Personalized Proposal Generator & Authorization Gate")
    st.caption("Generate grounded, non-hallucinatory proposals tailored to verified opportunity requirements. Human authorization is strictly required.")

    all_opps = list_opportunities()
    if all_opps:
        p_choice = st.selectbox(
            "Select Opportunity to Draft / Review Proposal",
            [o["id"] for o in all_opps],
            format_func=lambda i: f"#{i} — {next(o['title'] for o in all_opps if o['id'] == i)} (₹{next(o['amount_inr'] for o in all_opps if o['id'] == i):,.0f})",
            key="p_choice_box",
        )
        sel_opp = next(o for o in all_opps if o["id"] == p_choice)

        existing_prop = get_proposal(p_choice)
        prop_text = existing_prop["draft_text"] if existing_prop else ""
        prop_status = existing_prop["status"] if existing_prop else "not_generated"

        if prop_status == "approved_by_owner":
            st.success("🟢 Proposal Status: Approved by Owner (Ready for manual transmission)")
        elif prop_status == "draft":
            st.warning("🟡 Proposal Status: Draft (Pending Owner Authorization)")
        else:
            st.info("⚪ Proposal Status: Not yet drafted")

        col_p1, col_p2 = st.columns([1, 1])
        with col_p1:
            if st.button("Generate / Refresh Tailored Proposal", key="btn_gen_prop", type="primary"):
                llm_func = (lambda s, u: client.chat(s, u)) if (ollama_ok and OLLAMA_MODEL in installed_models) else None
                with st.spinner("Synthesizing personalized proposal based on verified capabilities…"):
                    pid, text, model = draft_tailored_proposal(p_choice, llm_func)
                st.success(f"Proposal draft generated using {model}.")
                st.rerun()

        if existing_prop:
            edited_text = st.text_area(
                "Proposal Text (Review and customize before approving):",
                value=prop_text,
                height=320,
                key="txt_prop_edit",
            )
            if edited_text != prop_text:
                if st.button("Save Edited Proposal", key="btn_save_prop"):
                    save_proposal_draft(p_choice, edited_text, existing_prop.get("model_used", "edited_by_owner"))
                    st.success("Proposal changes saved.")
                    st.rerun()

            if existing_prop["status"] == "draft":
                confirm_auth = st.checkbox(
                    "I have reviewed this proposal, verified its deliverables, and explicitly authorize it.",
                    key="chk_auth_prop",
                )
                if st.button("✅ Authorize & Approve Proposal", type="primary", disabled=not confirm_auth, key="btn_auth_prop"):
                    authorize_proposal(existing_prop["id"])
                    st.success("Proposal authorized by owner. Human approval is required before sending.")
                    st.rerun()
            elif existing_prop["status"] == "approved_by_owner":
                st.info("This proposal is formally approved. External communication remains manual and owner-controlled.")
    else:
        st.info("No opportunities in pipeline. Trigger discovery above or import opportunities.")


# ----------------- Tab 3: Opportunity Pipeline -----------------
with tabs[3]:
    st.subheader("Opportunity Pipeline & Management")

    entry_tab1, entry_tab2 = st.tabs(["Add single opportunity", "Import from CSV"])
    with entry_tab1:
        st.caption("Enter a lead you found through a permitted source. Do not add scraped or prohibited listings.")
        with st.form("opportunity_form", clear_on_submit=True):
            title = st.text_input("Opportunity title", placeholder="Example: Monthly sales dashboard for a local retailer")
            source = st.text_input("Source / client", placeholder="Example: Direct inbound enquiry")
            url = st.text_input("Reference URL (optional)")
            description = st.text_area("Requirements / scope", height=100)
            a, b, c = st.columns(3)
            amount = a.number_input("Offered amount (₹)", min_value=0.0, value=5000.0, step=500.0)
            hours = b.number_input("Estimated hours", min_value=0.5, value=3.0, step=0.5)
            skill_fit = c.slider("Skill match", min_value=0.0, max_value=1.0, value=0.8, step=0.05)
            d, e = st.columns(2)
            confidence = d.slider("Payment confidence", min_value=0.0, max_value=1.0, value=0.8, step=0.05)
            risk = e.slider("Risk score (10 = highest)", min_value=1.0, max_value=10.0, value=2.0, step=0.5)
            permitted = st.checkbox("Source is permitted and compliant with platform terms", value=True)
            save_opp = st.form_submit_button("Save opportunity")
            if save_opp:
                if not title.strip() or not source.strip():
                    st.error("Title and source/client are required.")
                else:
                    data = {
                        "title": title.strip(),
                        "source": source.strip(),
                        "url": url.strip(),
                        "description": description.strip(),
                        "amount_inr": amount,
                        "estimated_hours": hours,
                        "skill_match": skill_fit,
                        "payment_confidence": confidence,
                        "risk_level": risk,
                        "source_permitted": permitted,
                    }
                    score = opportunity_score(amount, hours, skill_fit, confidence, risk, permitted)
                    item_id = add_opportunity(data, score)
                    st.success(f"Opportunity #{item_id} saved. Risk-adjusted expected value: ₹{score:,.2f}/hour.")
                    st.caption(score_label(score))

    with entry_tab2:
        st.caption("Import structured opportunities from a user-selected CSV file.")
        sample_path = PROJECT_ROOT / "sample_opportunities.csv"
        if sample_path.exists():
            st.download_button(
                "Download sample opportunities CSV template",
                data=sample_path.read_bytes(),
                file_name="sample_opportunities.csv",
                mime="text/csv",
            )

        uploaded_opps = st.file_uploader("Select opportunity CSV file", type=["csv"], key="opps_csv_uploader")
        if uploaded_opps:
            try:
                preview = parse_and_validate_opportunities_csv(uploaded_opps.getvalue(), check_db_duplicates=True)
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Total rows", preview.total_rows)
                m2.metric("Valid novel rows", len(preview.importable_rows))
                m3.metric("Duplicate rows", len(preview.duplicate_rows))
                m4.metric("Invalid / rejected rows", len(preview.invalid_rows))

                if preview.invalid_rows:
                    st.error(f"Found {len(preview.invalid_rows)} invalid row(s).")
                    st.dataframe(
                        pd.DataFrame([
                            {"Row": r.row_num, "Title": r.raw_data.get("title", ""), "Errors": "; ".join(r.errors)}
                            for r in preview.invalid_rows
                        ]),
                        use_container_width=True,
                        hide_index=True,
                    )

                if preview.importable_rows:
                    confirm_box = st.checkbox("I confirm these opportunities are from permitted sources", key="confirm_import_opps")
                    if st.button("Confirm & import into pipeline", disabled=not confirm_box, key="btn_commit_opps"):
                        imported_ids = commit_imported_opportunities(preview.importable_rows)
                        st.success(f"Successfully imported {len(imported_ids)} opportunities.")
                        st.rerun()
            except Exception as exc:
                st.error(f"Failed to process CSV file: {exc}")

    opportunities = list_opportunities()
    st.subheader("Opportunity pipeline")
    if opportunities:
        view = pd.DataFrame(opportunities)
        view["score_label"] = view["score"].map(score_label)
        st.dataframe(
            view[["id", "title", "source", "amount_inr", "estimated_hours", "score", "score_label", "source_permitted", "status"]],
            use_container_width=True,
            hide_index=True,
        )
        choice = st.selectbox(
            "Select opportunity",
            options=[o["id"] for o in opportunities],
            format_func=lambda i: f"#{i} — {next(o['title'] for o in opportunities if o['id']==i)}",
        )
        selected = next(o for o in opportunities if o["id"] == choice)
        x, y, z = st.columns(3)
        current_status = selected.get("status", "new")
        allowed_next = VALID_OPPORTUNITY_TRANSITIONS.get(current_status, set())
        can_shortlist = "shortlisted" in allowed_next
        can_approve = "approved_for_work" in allowed_next and bool(selected.get("source_permitted"))
        can_reject = "rejected" in allowed_next

        if x.button("Shortlist", use_container_width=True, disabled=not can_shortlist):
            update_opportunity_status(choice, "shortlisted")
            st.rerun()
        if y.button("Approve for work", use_container_width=True, disabled=not can_approve):
            update_opportunity_status(choice, "approved_for_work")
            st.success("Marked approved for your work. This does not accept a contract or authorize submission.")
            st.rerun()
        if z.button("Reject", use_container_width=True, disabled=not can_reject):
            update_opportunity_status(choice, "rejected")
            st.rerun()

        if selected["status"] in {"approved_for_work", "submission_ready", "needs_manual_review"}:
            st.markdown("---")
            st.subheader("⚡ Autonomous Execution & QA Verification")
            st.caption("The AI plans the work, performs calculations, compiles deliverables, and runs the independent QA suite before declaring submission readiness.")

            data_source = st.radio(
                "Input dataset for this task:",
                ["Use bundled sample_sales.csv", "Upload client data CSV"],
                horizontal=True,
                key=f"data_src_{choice}",
            )
            job_file_bytes = None
            job_filename = "sample_sales.csv"
            if data_source == "Use bundled sample_sales.csv":
                sample_sales_path = PROJECT_ROOT / "sample_sales.csv"
                if sample_sales_path.exists():
                    job_file_bytes = sample_sales_path.read_bytes()
                    job_filename = "sample_sales.csv"
            else:
                custom_file = st.file_uploader("Upload client data CSV", type=["csv"], key=f"custom_csv_{choice}")
                if custom_file:
                    job_file_bytes = custom_file.getvalue()
                    job_filename = custom_file.name

            if job_file_bytes and st.button("🚀 Execute Work Plan & Run QA", type="primary", key=f"run_job_{choice}"):
                with st.spinner("Executing autonomous work plan and running independent QA checks..."):
                    llm_func = (lambda s, u: client.chat(s, u)) if (ollama_ok and OLLAMA_MODEL in installed_models) else None
                    job_result = run_autonomous_job(
                        opportunity=selected,
                        file_bytes=job_file_bytes,
                        filename=job_filename,
                        llm_call=llm_func,
                    )
                st.session_state[f"last_job_result_{choice}"] = job_result
                st.rerun()

            if f"last_job_result_{choice}" in st.session_state:
                jr = st.session_state[f"last_job_result_{choice}"]
                if jr.submission_ready:
                    st.success(f"✅ Deliverables Verified (QA Score: {jr.final_qa_report.score_pct}%) — Ready for Submission!")
                else:
                    st.warning(f"⚠️ QA Audit Flagged Issues (Score: {jr.final_qa_report.score_pct}%) — Manual Review Required.")

                st.download_button(
                    "📦 Download Verified Deliverables ZIP",
                    data=jr.artifact_zip.read_bytes(),
                    file_name=f"soloearn_task_{jr.task_id}_verified.zip",
                    mime="application/zip",
                    key=f"dl_verified_{jr.task_id}",
                )

                if jr.submission_ready:
                    confirm_submit = st.checkbox("I have reviewed the deliverables and approve marking this job as submitted", key=f"confirm_sub_{choice}")
                    if st.button("Authorize & Mark Submitted by Owner", disabled=not confirm_submit, key=f"btn_sub_{choice}"):
                        update_opportunity_status(choice, "submitted_by_owner")
                        st.success("Opportunity marked as submitted by owner.")
                        st.rerun()
    else:
        st.info("No opportunities have been added yet.")


# ----------------- Tab 4: Revenue Operations & Invoicing (M5) -----------------
with tabs[4]:
    st.subheader("🧾 Client Invoicing & Owner-Confirmed Receipts (Milestone 5)")
    st.caption("Generate local invoice drafts and record owner-confirmed receipts. SOLOEARN does not connect to a bank or independently verify UTRs.")

    # 6 Financial Lifecycle States
    st.markdown("### 📊 Financial Lifecycle States")
    f_states = get_financial_states_summary()
    fs1, fs2, fs3 = st.columns(3)
    fs1.metric("1. Estimated Pipeline", f"₹{f_states['estimated_pipeline_inr']:,.2f}", "Early leads/proposals")
    fs2.metric("2. Contracted Pipeline", f"₹{f_states['contracted_pipeline_inr']:,.2f}", "Approved for work")
    fs3.metric("3. Invoiced (Pending)", f"₹{f_states['invoiced_pending_inr']:,.2f}", "Awaiting client payment")

    fs4, fs5, fs6 = st.columns(3)
    fs4.metric("4. Cash Collected", f"₹{f_states['cash_collected_inr']:,.2f}", "Bank credits received")
    fs5.metric("5. TDS Receivable", f"₹{f_states['tds_receivable_inr']:,.2f}", "Client tax withheld")
    fs6.metric("6. Reconciled & Settled", f"₹{f_states['reconciled_settled_inr']:,.2f}", "Confirmed against bank")
    st.divider()

    inv_col1, inv_col2 = st.columns([1, 1])

    with inv_col1:
        st.markdown("#### 📝 Create Formal Client Invoice")
        submitted_opps = [o for o in list_opportunities() if o["status"] in {"submitted_by_owner", "won"}]
        if submitted_opps:
            opp_choice = st.selectbox(
                "Select Delivered Opportunity",
                options=[o["id"] for o in submitted_opps],
                format_func=lambda i: next(f"#{o['id']} - {o['title']} (₹{o['amount_inr']:,.0f})" for o in submitted_opps if o["id"] == i),
                key="inv_opp_choice",
            )
            selected_opp = next(o for o in submitted_opps if o["id"] == opp_choice)

            with st.form("create_invoice_form"):
                client_name = st.text_input("Client Organization / Name", value=f"Client for {selected_opp['title'][:30]}")
                service_tier = st.selectbox(
                    "Service Package Tier",
                    options=["data_analytics", "data_engineering", "interactive_dashboard", "business_automation"],
                    format_func=lambda t: t.replace("_", " ").title(),
                )
                amount_subtotal = st.number_input("Service Subtotal (₹)", min_value=100.0, value=float(selected_opp["amount_inr"]), step=500.0)
                tax_rate = st.number_input("GST / Tax Rate (%)", min_value=0.0, max_value=28.0, value=0.0, step=1.0)
                due_days = st.number_input("Payment Due Within (Days)", min_value=1, max_value=60, value=14)
                notes = st.text_area("Invoice Notes & Deliverable Reference", value="Deliverables verified and accepted via Independent QA audit.")

                st.caption("This creates a local invoice draft. Confirm the client agreement and applicable tax/invoice requirements before sending it.")
                create_inv_btn = st.form_submit_button("Generate Invoice Draft")
                if create_inv_btn:
                    try:
                        inv = create_invoice(
                            opportunity_id=opp_choice,
                            client_name=client_name,
                            service_tier=service_tier,
                            amount_subtotal=amount_subtotal,
                            tax_rate=tax_rate,
                            due_days=int(due_days),
                            notes=notes,
                            require_gst_registration=True,
                        )
                        # Render HTML invoice document
                        html_path = render_invoice_html(inv)
                        st.success(f"Invoice {inv['invoice_number']} created successfully!")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Error creating invoice: {exc}")
        else:
            st.info("No completed deliverables have been submitted by the owner yet. Submit a completed job in the Opportunity Pipeline first.")

    with inv_col2:
        st.markdown("#### 💳 Record Owner-Confirmed Payment")
        st.caption("SOLOEARN does not connect to your bank. This is an owner attestation after you independently check your bank statement.")
        pending_invoices = [i for i in list_invoices() if i["status"] in {"issued", "payment_pending"}]
        if pending_invoices:
            verify_inv_id = st.selectbox(
                "Select Invoice Awaiting Payment",
                options=[i["id"] for i in pending_invoices],
                format_func=lambda i: next(f"{inv['invoice_number']} — {inv['client_name']} (₹{inv['amount_total']:,.0f})" for inv in pending_invoices if inv["id"] == i),
                key="verify_inv_select",
            )
            target_inv = next(i for i in pending_invoices if i["id"] == verify_inv_id)

            with st.form("verify_payment_form"):
                st.write(f"**Invoice Total:** ₹{target_inv['amount_total']:,.2f}")
                st.write(f"**Billed To:** {target_inv['client_name']}")
                payment_ref = st.text_input("Bank Reference / UTR Number / Transaction ID", placeholder="e.g. HDFC9876543210 / UPI-REF-4567")
                received_amount = st.number_input("Verified Amount Credited (₹)", min_value=1.0, value=float(target_inv["amount_total"]), step=100.0)
                owner_confirm = st.checkbox("I independently checked my bank statement and confirm this exact payment was received", value=False)

                verify_btn = st.form_submit_button("Record Owner-Confirmed Receipt", type="primary")
                if verify_btn:
                    try:
                        paid_inv = verify_invoice_payment(
                            invoice_id=verify_inv_id,
                            payment_reference=payment_ref,
                            verified_amount=received_amount,
                            owner_confirmation=owner_confirm,
                        )
                        render_invoice_html(paid_inv)
                        st.success(f"Payment verified for {paid_inv['invoice_number']}! ₹{received_amount:,.2f} recorded in financial ledger.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Verification failed: {exc}")
        else:
            st.info("No invoices currently awaiting payment verification.")

    st.divider()
    st.markdown("#### 📋 Issued Invoices Ledger")
    invoices = list_invoices()
    if invoices:
        for inv in invoices:
            badge_emoji = "🟢" if inv["status"] == "paid" else "🟡"
            with st.expander(f"{badge_emoji} {inv['invoice_number']} — {inv['client_name']} · ₹{inv['amount_total']:,.2f} ({inv['status'].upper()})"):
                c1, c2, c3 = st.columns(3)
                c1.write(f"**Service Tier:** {inv['service_tier'].replace('_', ' ').title()}")
                c1.write(f"**Issued Date:** {inv['issued_at'][:10]}")
                c2.write(f"**Subtotal:** ₹{inv['amount_subtotal']:,.2f}")
                c2.write(f"**Tax:** ₹{inv['tax_amount']:,.2f}")
                c3.write(f"**Total:** ₹{inv['amount_total']:,.2f}")
                c3.write(f"**Payment Ref:** {inv['payment_reference'] or 'Pending'}")

                # Download HTML invoice
                inv_html_path = render_invoice_html(inv)
                if inv_html_path.exists():
                    st.download_button(
                        f"📄 Download Invoice {inv['invoice_number']} (HTML)",
                        data=inv_html_path.read_bytes(),
                        file_name=f"{inv['invoice_number']}.html",
                        mime="text/html",
                        key=f"dl_inv_{inv['id']}",
                    )
    else:
        st.write("No invoices generated yet.")

    st.divider()
    st.markdown("#### 🔄 Client Revisions & Daemon Health")
    rc1, rc2 = st.columns(2)
    with rc1:
        st.markdown("**Client Revision Requests**")
        all_revs = list_revisions()
        if all_revs:
            st.dataframe(pd.DataFrame(all_revs)[["id", "task_id", "status", "client_feedback", "created_at"]], use_container_width=True, hide_index=True)
        else:
            st.caption("No active revision requests.")

    with rc2:
        st.markdown("**Background Discovery Scheduler**")
        st.caption("For recurring feed polling, start START_SOLOEARN_SCHEDULER.bat. This scheduler discovers leads and flags interrupted tasks; it does not execute paid work or send proposals.")
        daemon = AutonomousDaemonScheduler()
        last_hb = daemon.get_last_heartbeat()
        if last_hb:
            st.write(f"**Status:** `{last_hb.get('status')}`")
            st.write(f"**Last Heartbeat:** {last_hb.get('last_run_at')}")
            st.write(f"**Active Sources Polled:** {last_hb.get('active_sources_count')}")
            st.write(f"**Interrupted Tasks Flagged for Review:** {last_hb.get('interrupted_tasks_recovered')}")
        else:
            st.caption("Daemon has not run yet.")

        if st.button("Run Daemon Maintenance & Recovery Now"):
            hb = daemon.run_once()
            st.success(f"Daemon cycle completed (PID {hb.pid}). Status: {hb.status}. Recovered: {hb.interrupted_tasks_recovered} jobs.")
            st.rerun()


# ----------------- Tab 5: Net Profit Control Centre (M6) -----------------
with tabs[5]:
    st.subheader("💎 Net Profit Intelligence Control Centre (Milestone 6)")
    st.caption("Engineered strictly around ₹1,00,000 / Month AFTER ALL business expenses and Indian income tax (Income-tax Act, 2025). Planning model only; consult a CA for official filing.")

    np_plan = NetProfitOptimizer.analyze_net_profit(MONTHLY_NET_PROFIT_TARGET_INR)

    st.markdown("### 🏆 Post-Tax Owner Profit North Star")
    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
    kpi1.metric("Take-Home Profit Target", f"₹{np_plan.monthly_net_profit_target_inr:,.0f}/mo", "₹12 Lakh / Yr")
    kpi2.metric("Estimated Net Profit MTD", f"₹{np_plan.mtd_net_profit_after_tax_inr:,.0f}", f"{np_plan.progress_pct}%")
    kpi3.metric("Operating Expenses MTD", f"₹{np_plan.mtd_operating_expenses_inr:,.0f}", f"{np_plan.observed_expense_ratio_pct}% cost ratio")
    
    if np_plan.distributable_cash_inr is not None:
        kpi4.metric("Distributable Cash In Bank", f"₹{np_plan.distributable_cash_inr:,.0f}", "After tax/GST reserves")
    else:
        kpi4.metric("Distributable Cash In Bank", "Unknown", "Opening balance unconfirmed")

    st.progress(
        min(1.0, np_plan.progress_pct / 100.0),
        text=f"Progress to ₹1 Lakh Post-Tax Profit: {np_plan.progress_pct}% (Net Gap: ₹{np_plan.net_profit_gap_inr:,.0f})"
    )

    st.info(f"💡 **Net Profit Intelligence:** {np_plan.recommended_action}")

    # Safety Buffer Model
    with st.expander("🛡️ Internal Safety Buffer Target (₹1,25,000 / Month Post-Tax)"):
        st.caption("Internal safety model designed to absorb inevitable real-world friction (delays, subscriptions, revisions) while keeping take-home profit strictly ≥ ₹1,00,000.")
        sb1, sb2, sb3 = st.columns(3)
        sb1.metric("Safety Buffer Target", f"₹{np_plan.monthly_safety_buffer_target_inr:,.0f}/mo", f"₹{np_plan.monthly_safety_buffer_target_inr * 12:,.0f}/yr")
        sb2.metric("Required Pre-Tax Business Profit", f"₹{np_plan.monthly_pretax_safety_buffer_required_inr:,.0f}/mo", f"₹{np_plan.annual_pretax_safety_buffer_required_inr:,.0f}/yr")
        sb3.metric("Safety Buffer Gap", f"₹{np_plan.safety_buffer_gap_inr:,.0f}/mo", "Effective Tax: 8.05%")
        st.write("**Job Capacities Needed for Safety Buffer:**")
        st.json(np_plan.safety_buffer_jobs_needed)

        st.markdown("#### 🎯 Illustrative Operating Safety Buffer Portfolio (18 Projects)")
        sb_model = NetProfitOptimizer.get_safety_buffer_portfolio_model()
        sb_col1, sb_col2, sb_col3, sb_col4 = st.columns(4)
        sb_col1.metric("Gross Billings", f"₹{sb_model['monthly_gross_billings_inr']:,.0f}/mo", "18 projects")
        sb_col2.metric("Pre-Tax Contribution", f"₹{sb_model['monthly_pretax_contribution_inr']:,.0f}/mo")
        sb_col3.metric("Monthly Tax Provision", f"₹{sb_model['monthly_tax_provision_inr']:,.0f}/mo", "Sec 202 slabs")
        sb_col4.metric("Post-Tax Net Profit", f"₹{sb_model['monthly_posttax_profit_inr']:,.0f}/mo", "+₹30,312 buffer")
        st.caption(
            f"Mix: 9 Analytics (@ ₹5k) + 5 Reconciliation (@ ₹10k) + 4 ETL (@ ₹15k). Delivery effort: ~53 hours/month. "
            f"Active Tax Accounting Method: **{np_plan.accounting_method.upper()}** (Sec 276). "
            "Note: Statutory GST registration threshold applies to aggregate Financial Year turnover (current FY invoice total + other PAN turnover), not an annualized forecast. "
            "All targets require deducting 100% of actual operating expenses and income tax before declaring targets achieved."
        )

    # Section 1: Financial Reserves & GST Separation
    st.divider()
    st.markdown("### ⚖️ Tax, GST & Cash Runway Reserves (C = B - G - T - U - W)")
    cash_info = get_cash_and_reserves_summary()
    rc1, rc2, rc3, rc4 = st.columns(4)
    
    if cash_info["bank_balance_inr"] is not None:
        rc1.metric("Bank Cash Balance (B)", f"₹{cash_info['bank_balance_inr']:,.2f}")
    else:
        rc1.metric("Bank Cash Balance (B)", "Unknown", "Awaiting confirmed opening balance")
        
    rc2.metric("GST Liability Reserve (G)", f"₹{cash_info['gst_liability_reserve_inr']:,.2f}")
    rc3.metric("Income Tax Reserve (T)", f"₹{cash_info['income_tax_reserve_inr']:,.2f}", f"Est. Tax MTD: ₹{np_plan.mtd_estimated_income_tax_inr:,.0f}")
    rc4.metric("Working Capital Buffer (W)", f"₹{cash_info['working_capital_buffer_inr']:,.2f}")

    gst_status = np_plan.gst_status
    if gst_status["status"] == "CA_REVIEW_REQUIRED":
        st.error(f"⚠️ **GST Alert:** {gst_status['message']}")
    elif gst_status["status"] == "APPROACHING_THRESHOLD":
        st.warning(f"⚠️ **GST Warning:** {gst_status['message']}")
    else:
        st.success(f"✅ **GST Status:** {gst_status['message']}")

    # Section 2: Required Customer Receipts Model
    st.divider()
    st.markdown("### 📈 Required Customer Receipts ex-GST to Net ₹1,00,000 / Month")
    st.caption("Under Income-tax Act, 2025 Section 202 & Section 156, ₹12.74 Lakh annual pre-tax profit yields ₹12.00 Lakh post-tax profit (₹1 Lakh/mo). Scenario table explicitly assumes all operating costs qualify as tax-deductible.")

    cost_ratios = [10, 20, 25, 30, 35, 40, 50]
    annual_pretax = np_plan.annual_pretax_profit_required_inr
    model_rows = []
    for cr in cost_ratios:
        frac = cr / 100.0
        req_annual = annual_pretax / (1.0 - frac)
        req_monthly = req_annual / 12.0
        model_rows.append({
            "Operating Cost Ratio": f"{cr}%",
            "Monthly Collected Receipts Needed": f"₹{req_monthly:,.0f}",
            "Annual Receipts Needed": f"₹{req_annual:,.0f}",
            "Required Jobs (at ~₹10k avg)": f"{math.ceil(req_monthly / 10000.0)} jobs/mo",
        })
    st.dataframe(pd.DataFrame(model_rows), use_container_width=True, hide_index=True)

    # Section 3: Expense Registration & Bank Reconciliation Forms
    st.divider()
    st.markdown("### 🧾 Business Expenses & Bank Statement Reconciliation")
    exp_col, bank_col = st.columns([1, 1])

    with exp_col:
        st.markdown("#### ➕ Record Business Expense")
        with st.form("add_expense_form"):
            vendor = st.text_input("Vendor / Payee Name", placeholder="e.g. Hetzner Cloud, Domain, Upwork Connects")
            cat = st.selectbox(
                "Expense Category",
                options=["software_tool", "hosting_infra", "marketing_acquisition", "direct_project_cost", "accounting_legal", "office_overhead", "other"],
                format_func=lambda x: x.replace("_", " ").title(),
            )
            exp_amount = st.number_input("Expense Amount (₹)", min_value=1.0, value=500.0, step=50.0)
            vendor_gst_input = st.number_input("Vendor GST on Invoice (Informational only) ₹", min_value=0.0, value=0.0, step=10.0, help="Stored for records; no automatic ITC is claimed.")
            deduct = st.selectbox("Tax Deductibility", options=["unknown", "deductible", "partly_deductible", "non_deductible"])
            ca_review_box = st.checkbox("CA Review Confirmed (Required if claiming deductible/partly deductible)")
            ca_notes_input = st.text_input("CA Review Notes / Reference", placeholder="e.g. Reviewed with CA Sharma on 2026-11-01")
            business_pct = st.slider("Business Use %", min_value=0.0, max_value=100.0, value=100.0, step=5.0)
            exp_notes = st.text_input("Reviewer Notes", placeholder="Receipt hash / reference / invoice number")

            add_exp_btn = st.form_submit_button("Record Expense in Ledger")
            if add_exp_btn:
                try:
                    eid = register_expense(
                        vendor=vendor,
                        category=cat,
                        amount_inr=exp_amount,
                        business_use_pct=business_pct,
                        gst_amount_inr=vendor_gst_input,
                        tax_deductibility=deduct,
                        reviewer_notes=exp_notes,
                        ca_review_confirmed=ca_review_box,
                        ca_review_notes=ca_notes_input,
                        reviewed_by="Owner with CA" if ca_review_box else "",
                    )
                    st.success(f"Expense EXP-{eid:05d} recorded and ledger updated!")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Error registering expense: {exc}")

        with st.expander("⚙️ Set / Confirm Bank Statement Opening Balance"):
            with st.form("bank_balance_form"):
                b_amount = st.number_input("Confirmed Bank Balance (₹)", min_value=0.0, value=50000.0, step=1000.0)
                b_date = st.date_input("As-of Statement Date")
                b_confirm = st.checkbox("I attest that this matches my official bank statement closing balance")
                if st.form_submit_button("Save Confirmed Bank Balance"):
                    if b_confirm:
                        set_bank_opening_balance(b_amount, as_of_date=str(b_date), is_confirmed=True)
                        st.success("Bank opening balance confirmed!")
                        st.rerun()
                    else:
                        st.error("Owner confirmation is required.")

        with st.expander("👤 Personal Tax Profile (Income-tax Act, 2025)"):
            st.caption("Current estimate support is limited to a resident individual sole proprietor using the default new regime for tax year 2026–27. Special-rate income (including capital gains), non-resident status, other entity types, and other regimes require CA review and are not modelled.")
            active_prof = get_active_tax_profile()
            with st.form("tax_profile_form"):
                other_pan_turnover = st.number_input(
                    "Additional FY Aggregate Turnover under Same PAN Outside SOLOEARN (₹, ex-GST)",
                    min_value=0.0,
                    value=float(active_prof.get("other_pan_aggregate_turnover_inr", 0.0)) if active_prof else 0.0,
                    step=10000.0,
                    help="Include other proprietorship/business turnover under the same PAN and supplies not invoiced through SOLOEARN. This is needed for a more complete GST threshold estimate.",
                )
                other_inc = st.number_input(
                    "Other Annual Taxable Income (₹)",
                    min_value=0.0,
                    value=float(active_prof["other_annual_income_inr"]) if active_prof else 0.0,
                    step=10000.0,
                    help="Enter other annual ordinary taxable income after applicable adjustments. Do not include special-rate capital gains or other special-rate income; this estimator does not model those."
                )
                adv_tax = st.number_input(
                    "Advance Tax Paid YTD (₹)",
                    min_value=0.0,
                    value=float(active_prof["advance_tax_paid_inr"]) if active_prof else 0.0,
                    step=5000.0
                )
                tds_credit = st.number_input(
                    "TDS Credits Available (₹)",
                    min_value=0.0,
                    value=float(active_prof["tds_credits_inr"]) if active_prof else 0.0,
                    step=2500.0
                )
                curr_gst_thresh = float(active_prof.get("gst_threshold_inr", 2000000.0)) if active_prof else 2000000.0
                gst_thresh_choice = st.selectbox(
                    "Applicable GST Registration Threshold",
                    options=[2000000.0, 1000000.0],
                    format_func=lambda x: f"₹{x:,.0f} ({'Standard States' if x == 2000000.0 else 'Special Category States: Manipur, Mizoram, Nagaland, Tripura'})",
                    index=0 if curr_gst_thresh == 2000000.0 else 1,
                    help="CBIC GST threshold for service providers is ₹20 Lakhs in most states, but ₹10 Lakhs in Special Category states.",
                )
                curr_acc_method = str(active_prof.get("accounting_method", "cash")).strip().lower() if active_prof else "cash"
                acc_method_choice = st.selectbox(
                    "Tax Accounting Method (Income-tax Act, 2025 Section 276)",
                    options=["cash", "mercantile"],
                    format_func=lambda x: "Cash (Income recognized upon actual receipt; default for solo operations)" if x == "cash" else "Mercantile / Accrual (Income recognized when invoices issued)",
                    index=0 if curr_acc_method == "cash" else 1,
                    help="Section 276 allows cash or mercantile accounting regularly employed by the business.",
                )
                if st.form_submit_button("Update Tax Profile"):
                    upsert_tax_profile(
                        other_annual_income_inr=other_inc,
                        other_pan_aggregate_turnover_inr=other_pan_turnover,
                        advance_tax_paid_inr=adv_tax,
                        tds_credits_inr=tds_credit,
                        gst_threshold_inr=gst_thresh_choice,
                        accounting_method=acc_method_choice,
                    )
                    st.success("Tax profile updated!")
                    st.rerun()

    with bank_col:
        st.markdown("#### 🏦 Import Bank Statement CSV & Reconcile")
        bank_csv = st.file_uploader("Upload Bank Statement (CSV only)", type=["csv"], key="bank_stmt_upload")
        if bank_csv:
            if st.button("Import Bank Statement"):
                try:
                    b_res = import_bank_statement_csv(bank_csv.getvalue(), filename=bank_csv.name)
                    st.success(f"Imported {b_res['rows_imported']} rows from {bank_csv.name}!")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Import error: {exc}")

        st.markdown("##### Unreconciled Bank Entries")
        unreconciled = list_unreconciled_statement_rows(limit=20)
        if unreconciled:
            pending_invoice_rows = [i for i in list_invoices() if i["status"] in {"issued", "payment_pending"}]
            expense_rows = list_expenses(limit=500)
            for u in unreconciled:
                st.write(
                    f"**#{u['id']} · {u['tx_date']}** | {u['tx_type'].upper()}: "
                    f"₹{u['amount_inr']:,.2f} | *{u['description'][:80]}* "
                    f"(Ref: `{u['bank_ref'] or 'N/A'}`)"
                )
                default_entities = (
                    ["invoice", "other"] if u["tx_type"] == "credit"
                    else ["expense", "owner_draw", "tax_payment", "gst_payment", "other"]
                )
                with st.form(f"reconcile_bank_row_{u['id']}"):
                    chosen_entity = st.selectbox(
                        "Match bank entry to",
                        options=default_entities,
                        key=f"reconcile_type_{u['id']}",
                    )
                    chosen_entity_id = ""
                    tds_input = 0.0
                    if chosen_entity == "invoice":
                        invoice_map = {
                            f"{i['invoice_number']} · {i['client_name']} · ₹{i['amount_total']:,.2f}": i["invoice_number"]
                            for i in pending_invoice_rows
                        }
                        if invoice_map:
                            selected_invoice_label = st.selectbox(
                                "Invoice",
                                options=list(invoice_map),
                                key=f"reconcile_invoice_{u['id']}",
                            )
                            chosen_entity_id = invoice_map[selected_invoice_label]
                            tds_options = [
                                (k, v["label"], v["rate"])
                                for k, v in SECTION_393_TDS_SCHEDULE.items()
                            ] + [("custom", "Custom TDS Amount (Manual Entry)", None)]

                            tds_cat = st.selectbox(
                                "Section 393 Statutory TDS Category (Income-tax Act, 2025 Table 6)",
                                options=tds_options,
                                format_func=lambda x: x[1],
                                key=f"reconcile_tds_cat_{u['id']}",
                                help="Income-tax Act, 2025 Section 393(1) Table 6 schedules differentiate rates based on payment type and the payee's legal status.",
                            )
                            inv_match = next((i for i in pending_invoice_rows if i["invoice_number"] == chosen_entity_id), None)
                            inv_subtotal = float(inv_match["amount_subtotal"]) if inv_match else 0.0
                            calc_tds = round(inv_subtotal * tds_cat[2], 2) if tds_cat[2] is not None else 0.0

                            tds_input = st.number_input(
                                f"TDS Withheld by Client ₹ (Section 393 - {tds_cat[1]})",
                                min_value=0.0,
                                value=calc_tds,
                                step=50.0,
                                key=f"reconcile_tds_{u['id']}",
                                help="Enter tax deducted by corporate client under Section 393. Bank credit + TDS must equal invoice total.",
                            )
                        else:
                            st.warning("No unpaid invoices are available. Choose 'other' or create the correct invoice first.")
                            chosen_entity_id = ""
                    elif chosen_entity == "expense":
                        expense_map = {
                            f"EXP-{e['id']:05d} · {e['vendor']} · ₹{e['amount_inr']:,.2f}": f"EXP-{e['id']:05d}"
                            for e in expense_rows
                        }
                        if expense_map:
                            selected_expense_label = st.selectbox(
                                "Expense",
                                options=list(expense_map),
                                key=f"reconcile_expense_{u['id']}",
                            )
                            chosen_entity_id = expense_map[selected_expense_label]
                        else:
                            st.warning("No recorded expenses are available to match.")
                    else:
                        chosen_entity_id = st.text_input(
                            "Reference / explanation",
                            value=u["bank_ref"] or f"Statement row {u['id']}",
                            key=f"reconcile_other_{u['id']}",
                        )
                    owner_ok = st.checkbox(
                        "I independently checked this exact entry against my bank statement",
                        value=False,
                        key=f"reconcile_confirm_{u['id']}",
                    )
                    do_reconcile = st.form_submit_button("Confirm reconciliation")
                    if do_reconcile:
                        try:
                            if not chosen_entity_id:
                                raise ValueError("Select or enter the matching invoice, expense, or explanation.")
                            reconcile_statement_row(
                                row_id=int(u["id"]),
                                entity_type=chosen_entity,
                                entity_id=chosen_entity_id,
                                owner_confirmed=owner_ok,
                                tds_withheld_inr=tds_input,
                            )
                            st.success(f"Bank row #{u['id']} reconciled.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Reconciliation blocked: {exc}")
        else:
            st.caption("Zero unreconciled bank rows. All imported transactions matched.")

    # Section 4: Service Unit Economics & Minimum Quote Calculator
    st.divider()
    st.markdown("### 🧮 Service Unit Economics & Minimum Profitable Quote Calculator")
    calc_c1, calc_c2 = st.columns([1, 1])

    with calc_c1:
        st.markdown("#### Minimum Quote Safeguard")
        st.caption("Never accept jobs that fall below our minimum 50% contribution margin threshold. Quote calculator provides a pre-tax contribution estimate, not a take-home profit quote; excludes general overhead, client acquisition, rework/refunds, income tax, and GST.")
        q_hours = st.number_input("Project Estimated Hours", min_value=0.5, max_value=40.0, value=3.0, step=0.5)
        q_direct = st.number_input("Direct Costs (Data / APIs) ₹", min_value=0.0, value=200.0, step=50.0)
        q_hourly = st.number_input("Desired Minimum Hourly Return (₹/hr)", min_value=1000.0, value=2000.0, step=250.0)

        quote_res = NetProfitOptimizer.calculate_minimum_profitable_quote(
            estimated_hours=q_hours,
            direct_costs_inr=q_direct,
            desired_hourly_return_inr=q_hourly,
        )
        st.write(f"**Recommended Minimum Quote:** ₹{quote_res['minimum_quote_inr']:,.2f}")
        st.write(f"**Net Contribution:** ₹{quote_res['net_contribution_inr']:,.2f} ({quote_res['contribution_margin_pct']}% margin)")
        st.write(f"**Effective Hourly Return:** ₹{quote_res['effective_hourly_return_inr']:,.2f}/hr")
        if quote_res["is_acceptable"]:
            st.success("✅ Profitable: Exceeds 50% contribution margin requirement.")
        else:
            st.error("❌ Unprofitable: Expected contribution margin below 50%. Reject or re-quote.")

    with calc_c2:
        st.markdown("#### Standard Service Catalog Unit Economics")
        st.dataframe(pd.DataFrame(np_plan.service_economics), use_container_width=True, hide_index=True)

    # Section 5: Priority Opportunity Queue Ranked by Risk-Adjusted Return
    st.divider()
    st.markdown("### ⚡ Priority Opportunity Queue (Risk-Adjusted Hourly Yield)")
    all_opps = list_opportunities()
    if all_opps:
        ranked_opps = RevenueOptimizer.rank_opportunities_by_hourly_yield(all_opps)
        display_data = []
        for o in ranked_opps[:15]:
            display_data.append({
                "ID": o["id"],
                "Title": o["title"][:40],
                "Budget (₹)": o["amount_inr"],
                "Hours": o["estimated_hours"],
                "Risk-adjusted gross yield (₹/hr)": round(o.get("risk_adjusted_gross_yield_inr_per_hour", 0.0), 0),
                "Skill Match": f"{int(o['skill_match'] * 100)}%",
                "Confidence": f"{int(o['payment_confidence'] * 100)}%",
                "Risk": o["risk_level"],
                "Status": o["status"],
            })
        st.dataframe(pd.DataFrame(display_data), use_container_width=True, hide_index=True)
    else:
        st.info("No opportunities in pipeline to rank.")


# ----------------- Tab 6: UPI Payment Centre (M7) -----------------
with tabs[6]:
    st.subheader("⚡ UPI Payment Centre & Dynamic QR Engine (Milestone 7)")
    st.caption("Direct UPI collection and dynamic QR generation without payment gateway fees. Instant scannable payment links, bank credit matching, and automated ledger settlement.")

    upi_summary = get_upi_payment_centre_summary()
    active_upi = upi_summary["upi_settings"]

    # Stage Info Banner
    if active_upi.get("account_type") == "personal":
        st.info(
            "**Active Mode: Stage A — Direct Personal UPI (Zero Gateway Fees)**\n\n"
            f"• Linked VPA: `{active_upi['payee_vpa']}` · Payee Name: **{active_upi['payee_name']}** · Bank: **{active_upi.get('bank_name', 'Linked Bank Account')}**\n"
            "• Standard Initiation Modes: Scannable QR uses **Mode 01 (QR code)**; Mobile links use **Mode 04 (Intent)**.\n"
            "• Payments flow directly into your linked bank account without intermediary gateway fees.\n"
            "• Settlement is verified strictly against receiving bank statement credits confirmed by the owner."
        )
    else:
        st.warning(
            f"**Stage B: Merchant UPI Integration ({active_upi.get('account_type', '').title()}) — NOT CONNECTED**\n\n"
            f"• Target VPA: `{active_upi['payee_vpa']}` · Configured MCC: `{active_upi.get('merchant_code', 'N/A')}`\n"
            "• **Connection Status: NOT CONNECTED TO LIVE BANK.**\n"
            "• The merchant webhook adapter and HMAC verification architecture are implemented in code, "
            "but live bank connectivity is NOT active. Bank merchant onboarding, sole-proprietor KYC, and bank-assigned credentials are required before live transactions can flow."
        )

    # 4 Proposed Headline Metrics
    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
    kpi1.metric("1. Invoices Generated", upi_summary["invoices_generated_count"], "Total issued invoices")
    kpi2.metric("2. Pending Payment", f"₹{upi_summary['pending_invoices_amount_inr']:,.2f}", f"{upi_summary['pending_invoices_count']} invoices awaiting credit")
    kpi3.metric("3. Confirmed Paid", f"₹{upi_summary['confirmed_reconciled_amount_inr']:,.2f}", f"{upi_summary['confirmed_reconciled_count']} bank-reconciled")
    kpi4.metric("4. Net Take-Home Profit", f"₹{upi_summary['net_profit_after_tax_inr']:,.2f}", "After actual expenses & tax provision")

    st.divider()

    # Workflow Stepper Diagram
    with st.expander("ℹ️ How the SOLOEARN UPI Payment Workflow Operates"):
        st.markdown(
            "```\n"
            "1. Generate Invoice   ──> 2. Create Dynamic UPI QR   ──> 3. Customer Scans QR\n"
            "   (INV-2026-XXXX)           (upi://pay?pa=...&am=...)        (GPay / PhonePe / Paytm)\n"
            "                                                                     │\n"
            "7. Net Profit Updated <── 6. Reconcile Automatically <── 5. Bank Account Credited\n"
            "   (Tax & Take-Home)        (Match Statement Row)            (Direct UPI Settlement)\n"
            "```\n"
            "**Key Financial Principle:** SOLOEARN never marks an invoice paid on customer screenshots, UTR messages, or QR scans alone. "
            "It only marks payment verified when confirmed against the receiving bank account's statement."
        )

    # Preflight Verification & Kotak Onboarding Assistant
    pref_col1, pref_col2 = st.columns(2)
    with pref_col1:
        with st.expander("🧪 Run M7.0.1 Local Preflight Test (Sandbox Verification)"):
            st.markdown(
                "Verify the complete Stage A payment collection lifecycle in an isolated sandbox before issuing real customer invoices: "
                "invoice creation, Mode 01 QR, Mode 04 Intent, Kotak statement parsing, duplicate protection, and ledger reconciliation."
            )
            if st.button("▶️ Execute Local Preflight Test", key="run_local_preflight_btn", type="primary"):
                with st.spinner("Running 7-step local preflight verification..."):
                    pref_res = run_m7_local_preflight()
                    if pref_res["success"]:
                        st.success(f"✅ Preflight Succeeded: {pref_res['steps_completed']}/{pref_res['total_steps']} steps verified!")
                    else:
                        st.error(f"❌ Preflight Incomplete: {pref_res['steps_completed']}/{pref_res['total_steps']} steps passed.")
                    for step_item in pref_res["results"]:
                        icon = "✅" if step_item["status"] == "PASSED" else "❌"
                        st.markdown(f"**Step {step_item['step']}: {icon} {step_item['name']}**")
                        st.caption(step_item["details"])

    with pref_col2:
        with st.expander("🏛️ Kotak Mahindra Bank Pilot Setup & Udyam Guide"):
            kotak_info = get_kotak_onboarding_guide()
            st.markdown(
                f"**Recommended Account:** `{kotak_info['recommended_account']}`\n\n"
                f"• **AQB Requirement:** ₹{kotak_info['average_quarterly_balance_inr']:,.2f} average quarterly balance.\n"
                f"• **Shortfall Fees:** ₹500/quarter if balance is ₹2.5k–₹5k; ₹1,000/quarter if below ₹2.5k.\n"
                f"• **Merchant App:** `{kotak_info['kotak_biz_specs']['app_name']}` ({kotak_info['kotak_biz_specs']['installation_charges']}).\n"
                f"• **Free Udyam Registration:** [{kotak_info['udyam_portal_url']}]({kotak_info['udyam_portal_url']})\n\n"
                "**Exact NIC Codes for Udyam:**\n"
                "- `62011`: Writing / automation scripts & computer programs\n"
                "- `62020`: Data analytics consulting & business intelligence\n"
                "- `63112`: Data processing, ETL & Excel data cleansing\n\n"
                "**4 Questions to Ask Kotak Branch:**\n"
                + "\n".join(f"- {q}" for q in kotak_info["four_questions_for_kotak_branch"])
            )

    st.divider()

    # Section 1: Dynamic UPI QR Invoice Generator & Viewer
    st.markdown("### 📱 Dynamic UPI QR Generator & Payment Intent")
    st.caption("Generate an NPCI-standard dynamic UPI QR code pre-filled with the exact invoice amount, payee VPA, and unique invoice reference.")

    invoices = list_invoices()
    active_invs = [i for i in invoices if i.get("status") != "cancelled"]

    if active_invs:
        inv_labels = {
            f"{i['invoice_number']} · {i['client_name']} · ₹{i['amount_total']:,.2f} ({i['status'].upper()})": i
            for i in active_invs
        }
        chosen_label = st.selectbox("Select Invoice to Generate UPI Payment QR", options=list(inv_labels.keys()), key="upi_qr_invoice_select")
        chosen_inv = inv_labels[chosen_label]

        upi_qr_data = generate_invoice_upi_qr(chosen_inv, upi_settings=active_upi, save_to_disk=True)

        qr_c1, qr_c2 = st.columns([1, 2])
        with qr_c1:
            st.image(upi_qr_data["qr_png_bytes"], caption=f"Scan to Pay: {chosen_inv['invoice_number']}", width=220)
            st.download_button(
                "⬇️ Download UPI QR Code (PNG)",
                data=upi_qr_data["qr_png_bytes"],
                file_name=f"{chosen_inv['invoice_number']}_upi_qr.png",
                mime="image/png",
                use_container_width=True,
            )

        with qr_c2:
            st.markdown(
                f"- **Invoice Number:** `{chosen_inv['invoice_number']}`\n"
                f"- **Client Name:** {chosen_inv['client_name']}\n"
                f"- **Exact Amount Due:** **₹{chosen_inv['amount_total']:,.2f}**\n"
                f"- **Payee UPI VPA:** `{upi_qr_data['payee_vpa']}`\n"
                f"- **Payee Name:** {upi_qr_data['payee_name']}\n"
                f"- **Transaction Ref (`tr`):** `{upi_qr_data['transaction_ref']}`\n"
                f"- **Transaction Note (`tn`):** `{upi_qr_data['transaction_note']}`\n"
                f"- **Initiation Mode:** `Mode 01 (QR code)` for Scannable QR · `Mode 04 (Intent)` for Mobile Link\n"
                f"- **Payment Status:** `{chosen_inv['status'].upper()}`\n"
            )
            st.code(upi_qr_data["upi_uri"], language="text")
            st.markdown(
                f"[📱 Open / Pay with UPI App (GPay / PhonePe / Paytm / BHIM)]({upi_qr_data.get('intent_uri', upi_qr_data['upi_uri'])})"
            )

            # View rendered HTML invoice button
            invoice_html_path = DATA_DIR / "invoices" / f"{chosen_inv['invoice_number']}.html"
            if invoice_html_path.exists():
                st.caption(f"HTML Invoice with embedded QR: `{invoice_html_path.name}`")
                st.download_button(
                    "📄 Download Print-Ready HTML Invoice",
                    data=invoice_html_path.read_text(encoding="utf-8"),
                    file_name=f"{chosen_inv['invoice_number']}.html",
                    mime="text/html",
                )
    else:
        st.info("No invoices found. Generate an invoice from Tab 4 (Revenue Operations) to view dynamic UPI QR codes.")

    st.divider()

    # Section 2: Statement Reconciliation & Automated UPI Matcher
    st.markdown("### 🏦 Bank Statement Matching & Automated UPI Settlement")
    st.caption("Match bank credits directly to pending invoices. Prevents fraud by requiring actual receiving-account statement credits.")

    unrec_rows = list_unreconciled_statement_rows()
    pending_invs = [i for i in active_invs if i.get("status") in {"issued", "payment_pending"}]

    if unrec_rows and pending_invs:
        st.write(f"Scanning **{len(unrec_rows)}** unreconciled bank statement rows against **{len(pending_invs)}** pending invoices...")
        matches = scan_and_match_statement_for_upi(unrec_rows, pending_invs)

        if matches:
            st.success(f"Found **{len(matches)}** potential UPI payment match(es) in imported statement:")
            for idx, m in enumerate(matches):
                with st.container():
                    st.markdown(
                        f"**Match #{idx + 1}: Invoice `{m['matched_invoice_number']}` ({m['matched_client_name']})** "
                        f"matches Statement Entry on `{m['statement_date']}` for **₹{m['statement_amount_inr']:,.2f}**"
                    )
                    st.caption(f"Narration: {m['statement_description']} | Bank Ref: {m['statement_bank_ref']} | Match Type: {m['match_type']}")
                    
                    match_col1, match_col2 = st.columns([2, 1])
                    with match_col1:
                        confirm_box = st.checkbox(
                            f"I confirm that ₹{m['statement_amount_inr']:,.2f} has cleared in my bank account for {m['matched_invoice_number']}",
                            key=f"upi_match_confirm_{m['statement_row_id']}_{idx}",
                        )
                    with match_col2:
                        if st.button("✅ Reconcile & Mark Paid", key=f"upi_reconcile_btn_{m['statement_row_id']}_{idx}", type="primary"):
                            if not confirm_box:
                                st.error("Please tick the confirmation checkbox to verify bank credit.")
                            else:
                                try:
                                    res = reconcile_upi_payment_from_bank_credit(
                                        statement_row_id=m["statement_row_id"],
                                        invoice_number=m["matched_invoice_number"],
                                        owner_confirmed=True,
                                    )
                                    st.success(f"Invoice {m['matched_invoice_number']} successfully reconciled and marked PAID!")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"Reconciliation error: {exc}")
                st.divider()
        else:
            st.info("No automatic matches found between statement credits and pending invoices. Check narration or use Tab 5 for manual matching.")
    elif not unrec_rows:
        st.info("No unreconciled bank statement rows available. Import your bank statement CSV in Tab 5 to scan for UPI credits.")
    else:
        st.info("No pending invoices awaiting payment.")

    st.divider()

    # Section 3: Financial Safeguards & Security Rules
    st.markdown("### 🛡️ Financial Safeguards & Security Rules")
    s_col1, s_col2 = st.columns(2)
    with s_col1:
        st.markdown(
            "**What SOLOEARN Will NEVER Do:**\n"
            "- ❌ Never mark an invoice paid on customer screenshot or UTR message.\n"
            "- ❌ Never mark paid merely because a QR code was scanned.\n"
            "- ❌ Never store or ask for your UPI PIN, MPIN, or banking passwords.\n"
            "- ❌ Never store banking OTPs or automate bank application logins.\n"
            "- ❌ Never initiate withdrawals or transfers without your authorization."
        )
    with s_col2:
        st.markdown(
            "**How Security & Integrity Are Maintained:**\n"
            "- ✅ Customer authorizes payment exclusively on their personal UPI app.\n"
            "- ✅ Payment proof requires actual receiving-account credit in bank statement.\n"
            "- ✅ Immutable append-only ledger prevents duplicate reference reuse.\n"
            "- ✅ Post-tax net profit and tax reserves update only upon cleared funds."
        )

    st.divider()

    # Section 4: UPI Settings Configuration
    st.markdown("### ⚙️ UPI Payment Configuration")
    st.caption("Configure your receiving UPI Virtual Payment Address (VPA), payee name, and mode.")

    with st.form("upi_settings_form"):
        cfg_vpa = st.text_input("Receiving UPI ID / VPA", value=active_upi.get("payee_vpa", "soloearn@upi"), help="Your personal or merchant UPI address (e.g. yourname@okhdfcbank or business@upi)")
        cfg_name = st.text_input("Payee Business / Personal Name", value=active_upi.get("payee_name", "SOLOEARN Operations"), help="Name displayed to customers on their UPI payment screen")
        cfg_type = st.selectbox(
            "Account Integration Mode",
            options=["personal", "merchant_bank_api", "gateway"],
            index=["personal", "merchant_bank_api", "gateway"].index(active_upi.get("account_type", "personal")),
            format_func=lambda x: {
                "personal": "Stage A: Personal UPI (Active - Zero Software Cost; Statement Reconciled)",
                "merchant_bank_api": "Stage B: Merchant UPI through Bank (NOT CONNECTED - Integration Framework Ready)",
                "gateway": "Stage C: Third-Party Payment Gateway (NOT CONNECTED)",
            }[x],
        )
        cfg_bank = st.text_input("Linked Bank Name", value=active_upi.get("bank_name", "Bank Linked UPI"))
        cfg_mc = st.text_input("Merchant Category Code (MCC, Optional)", value=active_upi.get("merchant_code", ""), help="Optional 4-digit MCC code (e.g. 8931 for IT/Data processing)")
        cfg_secret = st.text_input("Webhook HMAC Secret (Stage B/C)", value=active_upi.get("webhook_secret", ""), type="password", help="Secret key for verifying bank or gateway webhook signatures")

        save_upi_btn = st.form_submit_button("Save UPI Settings", type="primary")
        if save_upi_btn:
            try:
                updated_settings = save_upi_settings(
                    payee_vpa=cfg_vpa,
                    payee_name=cfg_name,
                    account_type=cfg_type,
                    merchant_code=cfg_mc,
                    bank_name=cfg_bank,
                    webhook_secret=cfg_secret,
                )
                st.success("UPI settings saved successfully!")
                st.rerun()
            except Exception as e:
                st.error(f"Error saving UPI settings: {e}")

    # Section 5: Stage B Bank Merchant Webhook Simulator
    with st.expander("🔌 Stage B — Bank Merchant Onboarding & Webhook Simulator (NOT CONNECTED — Sandbox Only)"):
        st.warning(
            "**Status: NOT CONNECTED to Live Bank.**\n\n"
            "This sandbox simulator tests the internal HMAC-SHA256 signature verification, 300-second replay attack protection, "
            "and idempotency logic. Live bank connectivity is only established once an acquiring bank approves merchant onboarding."
        )
        sim_col1, sim_col2 = st.columns(2)
        with sim_col1:
            sim_event_id = st.text_input("Webhook Event ID", value="EVT_UPI_2026_001", key="sim_evt_id")
            sim_inv_num = st.text_input("Invoice Number", value="INV-2026-0001", key="sim_inv_num")
            sim_amt = st.number_input("Amount (₹)", value=5000.0, step=500.0, key="sim_amt")
            sim_ref = st.text_input("Bank UTR / Transaction Ref", value="402511223344", key="sim_ref")
        with sim_col2:
            st.markdown("**Simulated Webhook Payload**")
            test_payload = {
                "event_id": sim_event_id,
                "invoice_number": sim_inv_num,
                "amount_inr": sim_amt,
                "transaction_ref": sim_ref,
                "status": "success",
            }
            st.json(test_payload)
            if st.button("Simulate Bank Webhook Callback"):
                try:
                    res = process_merchant_bank_webhook(test_payload)
                    st.success(f"Webhook processed: {res['status']} — {res['message']}")
                except Exception as exc:
                    st.error(f"Webhook processing error: {exc}")


# ----------------- Tab 7: CSV Workbench -----------------
with tabs[7]:
    st.subheader("CSV → business deliverables")
    st.write("Upload a CSV you own or are authorized to process. The worker creates a cleaned CSV, an Excel workbook, data-quality profile, charts and an HTML report.")
    uploaded = st.file_uploader("Choose a CSV file", type=["csv"], help=f"Maximum {MAX_UPLOAD_MB} MB and 250,000 rows in this MVP.")
    linked_opps = [o for o in list_opportunities() if o["status"] in {"approved_for_work", "submission_ready"}]
    link_options = {"No linked opportunity": None}
    link_options.update({f"#{o['id']} — {o['title']}": o["id"] for o in linked_opps})
    linked_label = st.selectbox("Link to an approved opportunity (optional)", list(link_options.keys()))
    run_qa = st.checkbox("Perform Independent QA Audit & Self-Repair verification", value=True)
    if uploaded and st.button("Run local analysis", type="primary"):
        try:
            llm_call = None
            if ollama_ok and OLLAMA_MODEL in installed_models:
                llm_call = lambda system, user: client.chat(system, user)

            if run_qa:
                opp_dict = {"id": link_options[linked_label], "title": Path(uploaded.name).stem, "source_permitted": True, "status": "approved_for_work"}
                with st.spinner("Executing work plan and running independent QA checks…"):
                    job_res = run_autonomous_job(
                        opportunity=opp_dict,
                        file_bytes=uploaded.getvalue(),
                        filename=uploaded.name,
                        llm_call=llm_call,
                    )
                st.success(f"Task #{job_res.task_id} completed. QA Score: {job_res.final_qa_report.score_pct}%.")
                st.download_button(
                    "Download verified deliverables ZIP",
                    data=job_res.artifact_zip.read_bytes(),
                    file_name=f"soloearn_task_{job_res.task_id}_verified.zip",
                    mime="application/zip",
                )
                st.write(f"Local output: `{job_res.artifact_zip.relative_to(PROJECT_ROOT)}`")
            else:
                task_id = create_task(
                    title=f"CSV analysis: {Path(uploaded.name).name}",
                    task_type="csv_business_analysis",
                    workspace="pending",
                    opportunity_id=link_options[linked_label],
                    notes="Local data analysis. External delivery requires owner approval.",
                )
                with st.spinner("Analyzing locally and building deliverables…"):
                    artifact = create_analytics_bundle(uploaded.getvalue(), uploaded.name, task_id, llm_call)
                complete_task(task_id, str(artifact.relative_to(PROJECT_ROOT)), "Analysis completed. Review outputs before client delivery.")
                st.success(f"Task #{task_id} completed. Deliverables are ready for review.")
                st.download_button("Download deliverables ZIP", data=artifact.read_bytes(), file_name=f"soloearn_task_{task_id}.zip", mime="application/zip")
                st.write(f"Local output: `{artifact.relative_to(PROJECT_ROOT)}`")
        except Exception as exc:
            st.error(f"Task could not be completed: {exc}")
            log_event("csv_analysis_error", str(exc))
    st.subheader("Completed tasks")
    tasks = list_tasks()
    if tasks:
        st.dataframe(pd.DataFrame(tasks)[["id", "title", "status", "artifact_path", "created_at"]], use_container_width=True, hide_index=True)


# ----------------- Tab 8: Earnings & Audit -----------------
with tabs[8]:
    st.subheader("Record income")
    st.caption("This form records expected or invoiced values only. Record paid receipts through the invoice workflow after independently checking your bank statement and payment reference.")
    with st.form("revenue_form", clear_on_submit=True):
        desc = st.text_input("Description", placeholder="Example: Client payment for sales dashboard")
        amount_paid = st.number_input("Amount (₹)", min_value=1.0, value=5000.0, step=500.0)
        status = st.selectbox("Status", ["expected", "invoiced", "cancelled"])
        opps = list_opportunities()
        revenue_opp = st.selectbox("Related opportunity (optional)", [None] + [o["id"] for o in opps], format_func=lambda i: "None" if i is None else f"#{i}")
        save_revenue = st.form_submit_button("Save revenue record")
        if save_revenue:
            if not desc.strip():
                st.error("Enter a description.")
            else:
                try:
                    add_revenue(desc.strip(), amount_paid, status, opportunity_id=revenue_opp)
                    st.success("Revenue record saved.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    rev = list_revenue()
    if rev:
        st.dataframe(pd.DataFrame(rev)[["id", "description", "amount_inr", "status", "paid_at", "created_at"]], use_container_width=True, hide_index=True)
    st.subheader("Audit log")
    logs = recent_audit(25)
    if logs:
        st.dataframe(pd.DataFrame(logs), use_container_width=True, hide_index=True)

st.divider()
st.caption("SOLOEARN v0.5.3 · local-first assisted-work system · no earnings are guaranteed · review all work and comply with each platform's rules.")
