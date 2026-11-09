"""
Verification script for Step 2 of Milestone 6.2:
Runs a complete sample job using synthetic sales data (sample_sales.csv).
Measures exact execution time and validates all deliverables.
"""
import json
import os
import sys
import time
import zipfile
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import openpyxl
import pandas as pd

from core.db import init_db, add_opportunity, get_opportunity, update_opportunity_status
from core.autonomous_worker import run_autonomous_job

def run_sample_verification():
    init_db()
    
    csv_path = Path("sample_sales.csv")
    if not csv_path.exists():
        print(f"ERROR: {csv_path} not found.")
        return 1
        
    file_bytes = csv_path.read_bytes()
    raw_df = pd.read_csv(csv_path)
    print(f"Loaded {csv_path.name}: {len(raw_df)} rows, {len(raw_df.columns)} columns.")
    
    # Create an opportunity approved for work
    opp_data = {
        "title": "Commercial Pilot: E-Commerce Sales Analysis & Excel Model",
        "description": "Clean, analyze, and build executive Excel workbook and HTML report for Q4 sales data.",
        "amount_inr": 5000.0,
        "source": "client_pilot",
        "url": "https://soloearn.local/pilot/001",
        "estimated_hours": 4.0,
        "skill_match": 1.0,
        "payment_confidence": 0.95,
        "risk_level": 0.05,
        "source_permitted": True,
    }
    opp_id = add_opportunity(opp_data, score=95.0)
    update_opportunity_status(opp_id, "approved_for_work")
    opp = get_opportunity(opp_id)
    
    print(f"Created Opportunity #{opp_id} in status '{opp['status']}' (Budget: INR {opp['amount_inr']}).")
    print("Starting autonomous job execution...")
    
    t0 = time.perf_counter()
    result = run_autonomous_job(
        opportunity=opp,
        file_bytes=file_bytes,
        filename=csv_path.name,
    )
    elapsed = time.perf_counter() - t0
    
    print("\n" + "=" * 60)
    print(f"EXECUTION COMPLETED IN {elapsed:.3f} seconds ({elapsed*1000:.1f} ms)")
    print("=" * 60)
    print(f"Task ID: {result.task_id}")
    print(f"Status: {result.status}")
    print(f"Submission Ready: {result.submission_ready}")
    print(f"Revisions: {result.revision_count}")
    print(f"Final QA Score: {result.final_qa_report.score_pct}% (Passed: {result.final_qa_report.passed})")
    
    ws = Path(result.workspace)
    print(f"\nInspecting Workspace: {ws}")
    
    # 1. Cleaned CSV
    cleaned_csv = ws / "cleaned_data.csv"
    assert cleaned_csv.exists(), "cleaned_data.csv missing"
    df_clean = pd.read_csv(cleaned_csv)
    print(f"  [x] Cleaned CSV: {cleaned_csv.name} ({cleaned_csv.stat().st_size} bytes, {len(df_clean)} rows)")
    
    # 2. Professional Excel workbook
    excel_path = ws / "business_analysis.xlsx"
    assert excel_path.exists(), "business_analysis.xlsx missing"
    wb = openpyxl.load_workbook(excel_path)
    sheet_names = wb.sheetnames
    print(f"  [x] Excel Workbook: {excel_path.name} ({excel_path.stat().st_size} bytes, Sheets: {sheet_names})")
    assert "Cleaned Data" in sheet_names, "Cleaned Data sheet missing"
    assert "Data Quality" in sheet_names, "Data Quality sheet missing"
    assert "Numeric Summary" in sheet_names, "Numeric Summary sheet missing"
    
    # 3. Charts
    chart_dir = ws / "charts"
    charts = list(chart_dir.glob("*.png"))
    print(f"  [x] Business Charts: {len(charts)} chart(s) generated in {chart_dir.name}/")
    for c in charts:
        print(f"      - {c.name} ({c.stat().st_size} bytes)")
    assert len(charts) > 0, "No charts generated"
    
    # 4. Interactive HTML Report
    html_path = ws / "report.html"
    assert html_path.exists(), "report.html missing"
    html_text = html_path.read_text(encoding="utf-8")
    print(f"  [x] Executive HTML Report: {html_path.name} ({html_path.stat().st_size} bytes, HTML title: 'SOLOEARN Business Data Report')")
    assert "SOLOEARN Business Data Report" in html_text
    
    # 5. Deliverables Package ZIP
    zip_path = ws / "deliverables.zip"
    assert zip_path.exists(), "deliverables.zip missing"
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf_names = zf.namelist()
        test_res = zf.testzip()
    print(f"  [x] Client Deliverables ZIP: {zip_path.name} ({zip_path.stat().st_size} bytes, testzip={test_res})")
    print(f"      Package contents ({len(zf_names)} entries):")
    for name in zf_names:
        print(f"        * {name}")
        
    print("\nALL STEP 2 DELIVERABLES VERIFIED SUCCESSFULLY!")
    return 0

if __name__ == "__main__":
    sys.exit(run_sample_verification())
