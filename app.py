from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import streamlit as st

from core.analytics import create_analytics_bundle
from core.config import MAX_UPLOAD_MB, OLLAMA_MODEL, PROJECT_ROOT, WORKSPACES_DIR, ensure_directories, safe_workspace_path
from core.db import (
    VALID_OPPORTUNITY_TRANSITIONS,
    add_opportunity, add_revenue, complete_task, create_task, init_db,
    list_opportunities, list_revenue, list_tasks, log_event, metrics,
    recent_audit, update_opportunity_status,
)
from core.llm import OllamaClient, draft_proposal
from core.scoring import opportunity_score, score_label
from core.opportunity_import import (
    commit_imported_opportunities,
    parse_and_validate_opportunities_csv,
)
from core.autonomous_worker import run_autonomous_job

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

tabs = st.tabs(["Overview", "Opportunities", "CSV Workbench", "Earnings & audit"])

with tabs[0]:
    st.subheader("What this version can do")
    left, right = st.columns(2)
    with left:
        st.markdown("**Working now**")
        st.markdown("- Store opportunity records locally\n- Rank permitted opportunities by risk-adjusted expected INR/hour\n- Generate honest proposal drafts (local Ollama optional)\n- Turn a CSV into an Excel workbook, charts and HTML report\n- Track expected, invoiced and confirmed-paid income")
    with right:
        st.markdown("**Intentionally not automated**")
        st.markdown("- No website scraping or platform-rule evasion\n- No automatic proposals or client messages\n- No contract acceptance or external submission\n- No bank login, money transfer or purchases\n- No unrestricted computer/file access")
    st.subheader("Recent work")
    tasks = list_tasks()[:8]
    if tasks:
        st.dataframe(pd.DataFrame(tasks)[["id", "title", "task_type", "status", "created_at"]], use_container_width=True, hide_index=True)
    else:
        st.info("No tasks yet. Try the CSV Workbench with a sample sales dataset.")

with tabs[1]:
    st.subheader("Opportunities")

    entry_tab1, entry_tab2 = st.tabs(["Add single opportunity", "Import from CSV"])
    with entry_tab1:
        st.caption("Enter a lead you found through a permitted source, or an authorized feed/integration. Do not add scraped or prohibited listings.")
        with st.form("opportunity_form", clear_on_submit=True):
            title = st.text_input("Opportunity title", placeholder="Example: Monthly sales dashboard for a local retailer")
            source = st.text_input("Source / client", placeholder="Example: Direct inbound enquiry")
            url = st.text_input("Reference URL (optional)")
            description = st.text_area("Requirements / scope", height=100)
            a, b, c = st.columns(3)
            amount = a.number_input("Quoted / advertised budget (₹)", min_value=0.0, value=5000.0, step=500.0)
            hours = b.number_input("Estimated hours", min_value=0.25, value=5.0, step=0.25)
            skill_fit = c.slider("Your skill fit", 0, 100, 60) / 100
            d, e = st.columns(2)
            confidence = d.slider("Payment confidence", 0, 100, 50) / 100
            risk = e.slider("Risk level (0 low – 10 high)", 0, 10, 5)
            permitted = st.checkbox("I verified this source permits my intended method of contact and work")
            submitted = st.form_submit_button("Save and score opportunity", type="primary")
            if submitted:
                if not title.strip() or not source.strip():
                    st.error("Title and source/client are required.")
                else:
                    data = {"title": title.strip(), "source": source.strip(), "url": url.strip(), "description": description.strip(),
                            "amount_inr": amount, "estimated_hours": hours, "skill_match": skill_fit,
                            "payment_confidence": confidence, "risk_level": risk, "source_permitted": permitted}
                    score = opportunity_score(amount, hours, skill_fit, confidence, risk, permitted)
                    item_id = add_opportunity(data, score)
                    st.success(f"Opportunity #{item_id} saved. Risk-adjusted expected value: ₹{score:,.2f}/hour.")
                    st.caption(score_label(score))

    with entry_tab2:
        st.caption("Import structured opportunities from a user-selected CSV file. Columns must include: title, source, url, description, amount_inr, estimated_hours, skill_match_pct, payment_confidence_pct, risk_level, source_permitted.")
        sample_path = PROJECT_ROOT / "sample_opportunities.csv"
        if sample_path.exists():
            st.download_button("Download sample opportunities CSV template", data=sample_path.read_bytes(), file_name="sample_opportunities.csv", mime="text/csv")

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
                    st.error(f"Found {len(preview.invalid_rows)} invalid or malformed row(s). These will NOT be imported.")
                    invalid_table = [
                        {
                            "Row": r.row_num,
                            "Title": r.raw_data.get("title", ""),
                            "Source": r.raw_data.get("source", ""),
                            "Validation Errors": "; ".join(r.errors),
                        }
                        for r in preview.invalid_rows
                    ]
                    st.dataframe(pd.DataFrame(invalid_table), use_container_width=True, hide_index=True)

                if preview.duplicate_rows:
                    st.warning(f"Found {len(preview.duplicate_rows)} duplicate row(s) (either within file or matching database).")
                    dup_table = [
                        {
                            "Row": r.row_num,
                            "Title": r.data["title"],
                            "Source": r.data["source"],
                            "Reason": r.duplicate_reason,
                        }
                        for r in preview.duplicate_rows
                    ]
                    st.dataframe(pd.DataFrame(dup_table), use_container_width=True, hide_index=True)

                if preview.importable_rows:
                    st.markdown("**Preview of valid opportunities to import:**")
                    preview_data = [
                        {
                            "Row": r.row_num,
                            "Title": r.data["title"],
                            "Source": r.data["source"],
                            "Amount (₹)": r.data["amount_inr"],
                            "Est Hours": r.data["estimated_hours"],
                            "Skill %": int(r.data["skill_match"] * 100),
                            "Confidence %": int(r.data["payment_confidence"] * 100),
                            "Risk (0-10)": r.data["risk_level"],
                            "Permitted": r.data["source_permitted"],
                            "Score (₹/hr)": r.score,
                            "Score Label": r.score_label,
                        }
                        for r in preview.importable_rows
                    ]
                    st.dataframe(pd.DataFrame(preview_data), use_container_width=True, hide_index=True)

                    st.markdown("---")
                    st.subheader("Human confirmation required")
                    st.caption("External action gate: review each opportunity row before committing to your local pipeline.")
                    confirm_check = st.checkbox(
                        f"I verify these {len(preview.importable_rows)} opportunities are from permitted sources and approve importing them.",
                        key="confirm_opp_import"
                    )
                    if st.button("Confirm & import into pipeline", type="primary", disabled=not confirm_check):
                        imported_ids = commit_imported_opportunities(preview.importable_rows, skip_duplicates=True)
                        log_event("opportunities_bulk_imported", f"count={len(imported_ids)}; file={uploaded_opps.name}")
                        st.success(f"Successfully imported {len(imported_ids)} opportunities!")
                        st.rerun()
                elif not preview.invalid_rows and not preview.duplicate_rows:
                    st.info("The CSV file did not contain any usable rows.")
            except Exception as exc:
                st.error(f"Failed to process CSV file: {exc}")

    opportunities = list_opportunities()
    st.subheader("Opportunity pipeline")
    if opportunities:
        view = pd.DataFrame(opportunities)
        view["score_label"] = view["score"].map(score_label)
        st.dataframe(view[["id", "title", "source", "amount_inr", "estimated_hours", "score", "score_label", "source_permitted", "status"]], use_container_width=True, hide_index=True)
        choice = st.selectbox("Select opportunity", options=[o["id"] for o in opportunities], format_func=lambda i: f"#{i} — {next(o['title'] for o in opportunities if o['id']==i)}")
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
        if st.button("Draft proposal for review", disabled=not bool(selected["source_permitted"])):
            with st.spinner("Drafting locally if Ollama is available…"):
                draft, used_model = draft_proposal(selected, client)
            st.text_area("Proposal draft — not sent", value=draft, height=250)
            st.caption("Generated with local Ollama." if used_model else "Template fallback used; local Ollama was unavailable.")

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

                with st.expander("📋 View Execution Plan & QA Report Details", expanded=False):
                    st.write("**Plan Steps:**")
                    st.dataframe(pd.DataFrame([asdict(s) for s in jr.plan.steps]), use_container_width=True, hide_index=True)
                    st.write("**QA Verification Checks:**")
                    qa_table = [
                        {
                            "Check": c.check_name,
                            "Status": "PASS" if c.passed else "FAIL",
                            "Summary": c.summary,
                        }
                        for c in jr.final_qa_report.checks
                    ]
                    st.dataframe(pd.DataFrame(qa_table), use_container_width=True, hide_index=True)

                if jr.submission_ready:
                    confirm_submit = st.checkbox("I have reviewed the deliverables and approve marking this job as submitted", key=f"confirm_sub_{choice}")
                    if st.button("Authorize & Mark Submitted by Owner", disabled=not confirm_submit, key=f"btn_sub_{choice}"):
                        update_opportunity_status(choice, "submitted_by_owner")
                        st.success("Opportunity marked as submitted by owner.")
                        st.rerun()
    else:
        st.info("No opportunities have been added yet.")

with tabs[2]:
    st.subheader("CSV → business deliverables")
    st.write("Upload a CSV you own or are authorized to process. The worker creates a cleaned CSV, an Excel workbook, data-quality profile, charts and an HTML report.")
    uploaded = st.file_uploader("Choose a CSV file", type=["csv"], help=f"Maximum {MAX_UPLOAD_MB} MB and 250,000 rows in this MVP.")
    linked_opps = [o for o in list_opportunities() if o["status"] in {"approved_for_work", "submission_ready"}]
    link_options = {"No linked opportunity": None}
    link_options.update({f"#{o['id']} — {o['title']}": o["id"] for o in linked_opps})
    linked_label = st.selectbox("Link to an approved opportunity (optional)", list(link_options.keys()))
    run_qa = st.checkbox("Perform Independent QA Audit & Self-Repair verification (Milestone 3)", value=True)
    if uploaded and st.button("Run local analysis", type="primary"):
        try:
            llm_call = None
            if ollama_ok and OLLAMA_MODEL in installed_models:
                llm_call = lambda system, user: client.chat(system, user)

            if run_qa:
                opp_dict = {"id": link_options[linked_label], "title": Path(uploaded.name).stem}
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
                # Create task record first so its ID can scope the workspace.
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

with tabs[3]:
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
                add_revenue(desc.strip(), amount_paid, status, opportunity_id=revenue_opp)
                st.success("Revenue record saved.")
                st.rerun()
    rev = list_revenue()
    if rev:
        st.dataframe(pd.DataFrame(rev)[["id", "description", "amount_inr", "status", "paid_at", "created_at"]], use_container_width=True, hide_index=True)
    st.subheader("Audit log")
    logs = recent_audit(25)
    if logs:
        st.dataframe(pd.DataFrame(logs), use_container_width=True, hide_index=True)

st.divider()
st.caption("SOLOEARN v0.1 · local-first prototype · no earnings are guaranteed · review all work and comply with each platform's rules.")
