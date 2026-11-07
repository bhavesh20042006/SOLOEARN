from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import streamlit as st

from core.analytics import create_analytics_bundle
from core.autonomous_worker import run_autonomous_job
from core.config import (
    MAX_UPLOAD_MB,
    OLLAMA_MODEL,
    PROJECT_ROOT,
    WORKSPACES_DIR,
    ensure_directories,
    safe_workspace_path,
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
from core.scheduler import AutonomousDaemonScheduler
from core.scoring import opportunity_score, score_label

st.set_page_config(page_title="SOLOEARN — Private AI Workbench", page_icon="🧠", layout="wide")
ensure_directories()
init_db()

st.title("🧠 SOLOEARN")
st.caption("Private, local-first income-workflow workbench · single owner · zero paid API dependency")
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
    "Autonomous Discovery (M4)",
    "Opportunity Pipeline",
    "Revenue Operations & Invoicing (M5)",
    "Revenue Target Optimizer (₹1L/mo)",
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


# ----------------- Tab 1: Autonomous Discovery (Milestone 4) -----------------
with tabs[1]:
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


# ----------------- Tab 2: Opportunity Pipeline -----------------
with tabs[2]:
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


# ----------------- Tab 3: Revenue Operations & Invoicing (M5) -----------------
with tabs[3]:
    st.subheader("🧾 Client Invoicing & Owner-Confirmed Receipts (Milestone 5)")
    st.caption("Generate local invoice drafts and record owner-confirmed receipts. SOLOEARN does not connect to a bank or independently verify UTRs.")

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


# ----------------- Tab 4: Revenue Target Optimizer (₹1L/mo) -----------------
with tabs[4]:
    st.subheader("🎯 Gross Paid Revenue Target Optimizer (Illustrative Goal: ₹1,00,000 / Month)")
    st.caption("Tracks owner-confirmed gross receipts and estimates pipeline. It cannot guarantee customers, gross sales, net profit, or personal take-home income.")

    plan = RevenueOptimizer.analyze_monthly_target(MONTHLY_REVENUE_TARGET_INR)

    st.markdown("### Monthly Performance Dashboard")
    pc1, pc2, pc3, pc4 = st.columns(4)
    pc1.metric("Gross Paid Revenue Target", f"₹{plan.monthly_target_inr:,.0f}")
    pc2.metric("MTD Owner-Confirmed Gross Paid", f"₹{plan.mtd_paid_inr:,.0f}", f"{plan.progress_pct}%")
    pc3.metric("Outstanding Invoices (All Periods)", f"₹{plan.invoiced_pending_inr:,.0f}")
    pc4.metric("Uncontracted Active Pipeline", f"₹{plan.pipeline_active_inr:,.0f}")

    st.progress(min(1.0, plan.progress_pct / 100.0), text=f"Progress to ₹1 Lakh: {plan.progress_pct}% (Gap: ₹{plan.target_gap_inr:,.0f})")

    st.info(f"💡 **Recommended Operational Action:** {plan.recommended_action}")

    st.divider()
    st.markdown("### 📊 Illustrative ₹1,05,000 Gross Billing Mix")
    st.write("This is arithmetic using assumed catalog prices, not proven market demand or a delivery-capacity guarantee. The example is 46 direct delivery hours before sales, communication, revisions, administration, fees and taxes.")
    st.dataframe(pd.DataFrame(plan.optimal_monthly_mix), use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("### ⚡ Priority Opportunity Queue (Risk-Adjusted Gross Yield Estimate)")
    st.write("Jobs ranked by estimated risk-adjusted gross yield per delivery hour. This heuristic is not net profit, a trained payment probability, or a guarantee; it excludes acquisition, admin, fees and revisions:")
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


# ----------------- Tab 5: CSV Workbench -----------------
with tabs[5]:
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


# ----------------- Tab 6: Earnings & Audit -----------------
with tabs[6]:
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
