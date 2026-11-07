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
    complete_task,
    create_task,
    delete_discovery_source,
    get_discovery_source,
    get_opportunity,
    get_proposal,
    init_db,
    list_discovery_sources,
    list_opportunities,
    list_proposals,
    list_revenue,
    list_tasks,
    log_event,
    metrics,
    recent_audit,
    save_proposal_draft,
    update_opportunity_status,
)
from core.discovery_engine import DiscoveryEngine
from core.llm import OllamaClient, draft_proposal
from core.opportunity_import import (
    commit_imported_opportunities,
    parse_and_validate_opportunities_csv,
)
from core.proposal_generator import authorize_proposal, draft_tailored_proposal, generate_proposal_template
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


# ----------------- Tab 3: CSV Workbench -----------------
with tabs[3]:
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


# ----------------- Tab 4: Earnings & Audit -----------------
with tabs[4]:
    st.subheader("Record income")
    st.caption("Only mark an amount as Paid after you have independently confirmed the money arrived in your account.")
    with st.form("revenue_form", clear_on_submit=True):
        desc = st.text_input("Description", placeholder="Example: Client payment for sales dashboard")
        amount_paid = st.number_input("Amount (₹)", min_value=1.0, value=5000.0, step=500.0)
        status = st.selectbox("Status", ["expected", "invoiced", "paid", "cancelled"])
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
st.caption("SOLOEARN v0.1 · local-first prototype · no earnings are guaranteed · review all work and comply with each platform's rules.")
