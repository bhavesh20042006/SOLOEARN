from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path

import openpyxl
import pandas as pd

from core.analytics import build_client_archive
from core.autonomous_worker import run_autonomous_job
from core.config import ensure_directories, safe_workspace_path
from core.db import add_opportunity, connect, get_opportunity, get_task, init_db, update_opportunity_status
from core.execution_engine import AutonomousExecutionEngine, generate_execution_plan
from core.qa_agent import IndependentQAAgent
from core.revision_engine import RevisionEngine

SAMPLE_CSV = b"""product,category,revenue,units_sold,return_rate
Laptop,Electronics,120000,20,0.05
Headphones,Electronics,15000,50,0.02
Desk Chair,Furniture,25000,15,0.01
Monitor,Electronics,45000,30,0.03
Notebook,Stationery,500,100,0.00
Pen,Stationery,200,200,0.00
Coffee Table,Furniture,18000,10,0.02
Keyboard,Electronics,6000,40,0.01
"""


class Milestone3ExecutionTests(unittest.TestCase):
    def setUp(self):
        ensure_directories()
        init_db()
        with connect() as conn:
            conn.execute("DELETE FROM proposals")
            conn.execute("DELETE FROM revenue")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM opportunities")
            conn.execute("DELETE FROM audit_log")

    def test_plan_generation(self):
        opp = {
            "id": 101,
            "title": "Quarterly Sales Review",
            "description": "Analyze monthly retail sales and profile product performance.",
            "amount_inr": 8000,
            "estimated_hours": 4,
            "source_permitted": True,
            "status": "approved_for_work",
        }
        plan = generate_execution_plan(opp, task_id=1, filename="sales.csv")
        self.assertEqual(plan.task_id, 1)
        self.assertEqual(plan.opportunity_id, 101)
        self.assertIn("Quarterly Sales Review", plan.title)
        self.assertEqual(len(plan.steps), 7)
        self.assertIn("business_analysis.xlsx", plan.target_deliverables)
        self.assertIn("report.html", plan.target_deliverables)

    def test_autonomous_execution_engine(self):
        opp_id = add_opportunity(
            {
                "title": "Store Performance Audit",
                "source": "Direct",
                "description": "Deduplicate and clean product sales data.",
                "amount_inr": 6000,
                "estimated_hours": 3,
                "skill_match": 1.0,
                "payment_confidence": 1.0,
                "risk_level": 1,
                "source_permitted": True,
            },
            score=1000,
        )
        update_opportunity_status(opp_id, "approved_for_work")
        opp = get_opportunity(opp_id)

        engine = AutonomousExecutionEngine()
        task_id, workspace, plan = engine.execute_job(
            opportunity=opp,
            file_bytes=SAMPLE_CSV,
            filename="sample_test.csv",
        )
        self.assertTrue(workspace.exists())
        self.assertTrue((workspace / "business_analysis.xlsx").exists())
        self.assertTrue((workspace / "cleaned_data.csv").exists())
        self.assertTrue((workspace / "profile.json").exists())
        self.assertTrue((workspace / "report.html").exists())
        self.assertTrue((workspace / "deliverables.zip").exists())
        self.assertTrue((workspace / "execution_plan.json").exists())
        self.assertTrue(all(step.status == "completed" for step in plan.steps))


class Milestone3QAAgentTests(unittest.TestCase):
    def setUp(self):
        ensure_directories()
        init_db()
        opp_id = add_opportunity(
            {
                "title": "QA Test Opportunity",
                "source": "Direct",
                "description": "Testing QA Agent verification.",
                "amount_inr": 5000,
                "estimated_hours": 2,
                "skill_match": 1.0,
                "payment_confidence": 1.0,
                "risk_level": 1,
                "source_permitted": True,
            },
            score=1000,
        )
        update_opportunity_status(opp_id, "approved_for_work")
        opp = get_opportunity(opp_id)

        engine = AutonomousExecutionEngine()
        self.task_id, self.workspace, _ = engine.execute_job(
            opportunity=opp,
            file_bytes=SAMPLE_CSV,
            filename="qa_test.csv",
        )
        self.qa_agent = IndependentQAAgent(self.workspace, self.task_id)

    def test_qa_passes_on_valid_bundle(self):
        report = self.qa_agent.run_all_checks()
        self.assertTrue(report.passed)
        self.assertEqual(report.score_pct, 100.0)
        self.assertEqual(len(report.failure_reasons), 0)
        self.assertTrue((self.workspace / "qa_report.json").exists())

    def test_qa_detects_missing_report_file(self):
        report_file = self.workspace / "report.html"
        report_file.unlink()
        report = self.qa_agent.run_all_checks()
        self.assertFalse(report.passed)
        self.assertTrue(any("report.html" in r for r in report.failure_reasons))

    def test_qa_detects_corrupt_excel_file(self):
        excel_file = self.workspace / "business_analysis.xlsx"
        excel_file.write_bytes(b"not_a_valid_xlsx_file")
        report = self.qa_agent.run_all_checks()
        self.assertFalse(report.passed)
        self.assertTrue(any("excel_integrity" in r for r in report.failure_reasons))

    def test_qa_detects_math_inconsistency(self):
        profile_file = self.workspace / "profile.json"
        data = json.loads(profile_file.read_text(encoding="utf-8"))
        # Introduce intentional math discrepancy
        data["rows_original"] = 99999
        profile_file.write_text(json.dumps(data), encoding="utf-8")

        report = self.qa_agent.run_all_checks()
        self.assertFalse(report.passed)
        self.assertTrue(any("calculation_consistency" in r for r in report.failure_reasons))

    def test_qa_detects_corrupted_chart_image(self):
        chart_dir = self.workspace / "charts"
        for png in chart_dir.glob("*.png"):
            png.write_bytes(b"corrupt_image_bytes")
            break
        report = self.qa_agent.run_all_checks()
        self.assertFalse(report.passed)
        self.assertTrue(any("visual_deliverables" in r for r in report.failure_reasons))

    def test_qa_detects_formula_injection_in_metadata_sheet(self):
        excel_file = self.workspace / "business_analysis.xlsx"
        wb = openpyxl.load_workbook(excel_file)
        ws_dq = wb["Data Quality"]
        # Inject formula into Data Quality sheet
        ws_dq.cell(row=2, column=1, value="=cmd|' /C calc'!A0")
        wb.save(excel_file)
        wb.close()

        report = self.qa_agent.run_all_checks()
        self.assertFalse(report.passed)
        self.assertTrue(any("excel_integrity" in r for r in report.failure_reasons))
        self.assertTrue(any("formula" in r.lower() for r in report.failure_reasons))

    def test_qa_detects_missing_numeric_summary_row(self):
        excel_file = self.workspace / "business_analysis.xlsx"
        wb = openpyxl.load_workbook(excel_file)
        ws_num = wb["Numeric Summary"]
        # Delete row 2 to simulate a missing numeric column summary
        ws_num.delete_rows(2, 1)
        wb.save(excel_file)
        wb.close()

        report = self.qa_agent.run_all_checks()
        self.assertFalse(report.passed)
        self.assertTrue(any("calculation_consistency" in r for r in report.failure_reasons))
        self.assertTrue(any("Numeric Summary" in r for r in report.failure_reasons))

    def test_archive_builder_rejects_symlinked_chart(self):
        chart_dir = self.workspace / "charts"
        pngs = list(chart_dir.glob("*.png"))
        if not pngs:
            dummy = chart_dir / "test_dummy.png"
            dummy.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 500)
            pngs = [dummy]

        symlink_chart = chart_dir / "symlink_test.png"
        try:
            symlink_chart.symlink_to(pngs[0].resolve())
            with self.assertRaises(ValueError) as ctx:
                build_client_archive(self.workspace)
            self.assertIn("symlink", str(ctx.exception).lower())
            symlink_chart.unlink()
        except OSError:
            # Platform restricted symlinks (e.g. non-admin Windows)
            orig_is_symlink = Path.is_symlink
            def mock_is_symlink(p):
                if p.name == "mock_symlink.png":
                    return True
                return orig_is_symlink(p)

            mock_file = chart_dir / "mock_symlink.png"
            mock_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 500)
            try:
                with patch.object(Path, "is_symlink", side_effect=mock_is_symlink, autospec=True):
                    with self.assertRaises(ValueError) as ctx:
                        build_client_archive(self.workspace)
                    self.assertIn("symlink", str(ctx.exception).lower())
            finally:
                if mock_file.exists():
                    mock_file.unlink()

    def test_archive_excludes_arbitrary_workspace_files(self):
        secret_file = self.workspace / "arbitrary_secret.txt"
        secret_file.write_text("confidential-token-12345", encoding="utf-8")
        internal_log = self.workspace / "internal_debug.log"
        internal_log.write_text("debug log output", encoding="utf-8")

        build_client_archive(self.workspace)

        zip_path = self.workspace / "deliverables.zip"
        with zipfile.ZipFile(zip_path, "r") as zf:
            archived = zf.namelist()
            self.assertNotIn("arbitrary_secret.txt", archived)
            self.assertNotIn("internal_debug.log", archived)
            self.assertNotIn("execution_plan.json", archived)
            self.assertNotIn("qa_report.json", archived)
            self.assertIn("business_analysis.xlsx", archived)
            self.assertIn("report.html", archived)


class Milestone3RevisionEngineTests(unittest.TestCase):
    def setUp(self):
        ensure_directories()
        init_db()
        opp_id = add_opportunity(
            {
                "title": "Revision Test Opportunity",
                "source": "Direct",
                "description": "Testing Revision Engine self-healing.",
                "amount_inr": 5000,
                "estimated_hours": 2,
                "skill_match": 1.0,
                "payment_confidence": 1.0,
                "risk_level": 1,
                "source_permitted": True,
            },
            score=1000,
        )
        update_opportunity_status(opp_id, "approved_for_work")
        opp = get_opportunity(opp_id)

        engine = AutonomousExecutionEngine()
        self.task_id, self.workspace, _ = engine.execute_job(
            opportunity=opp,
            file_bytes=SAMPLE_CSV,
            filename="revision_test.csv",
        )

    def test_revision_engine_repairs_missing_excel(self):
        # Break the Excel workbook
        (self.workspace / "business_analysis.xlsx").unlink()

        qa_agent = IndependentQAAgent(self.workspace, self.task_id)
        failing_report = qa_agent.run_all_checks()
        self.assertFalse(failing_report.passed)

        # Trigger Revision Engine
        rev = RevisionEngine(self.workspace, self.task_id, max_attempts=2)
        passed, final_report = rev.repair_and_retest(failing_report)

        self.assertTrue(passed)
        self.assertTrue(final_report.passed)
        self.assertTrue((self.workspace / "business_analysis.xlsx").exists())
        self.assertEqual(len(rev.history), 1)
        self.assertTrue((self.workspace / "revision_history.json").exists())

    def test_revision_engine_repairs_corrupted_charts(self):
        # Corrupt all charts
        chart_dir = self.workspace / "charts"
        for c in chart_dir.glob("*.png"):
            c.write_bytes(b"corrupt")

        qa_agent = IndependentQAAgent(self.workspace, self.task_id)
        failing_report = qa_agent.run_all_checks()
        self.assertFalse(failing_report.passed)

        # Trigger Revision Engine
        rev = RevisionEngine(self.workspace, self.task_id, max_attempts=2)
        passed, final_report = rev.repair_and_retest(failing_report)

        self.assertTrue(passed)
        self.assertTrue(final_report.passed)
        self.assertEqual(len(rev.history), 1)

    def test_revision_engine_bounds_retry_cycle(self):
        # Completely remove source data so repair cannot succeed
        (self.workspace / "cleaned_data.csv").unlink()
        (self.workspace / "profile.json").unlink()

        qa_agent = IndependentQAAgent(self.workspace, self.task_id)
        failing_report = qa_agent.run_all_checks()
        self.assertFalse(failing_report.passed)

        # Ensure max_attempts=2 halts without looping infinitely
        rev = RevisionEngine(self.workspace, self.task_id, max_attempts=2)
        passed, final_report = rev.repair_and_retest(failing_report)

        self.assertFalse(passed)
        self.assertEqual(len(rev.history), 2)

    def test_revision_attempts_are_capped_at_three(self):
        rev = RevisionEngine(self.workspace, self.task_id, max_attempts=10)
        self.assertEqual(rev.max_attempts, 3)

    def test_revision_engine_rejects_workspace_outside_project(self):
        outside_dir = Path(tempfile.gettempdir()) / "outside_workspace"
        outside_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ValueError):
            RevisionEngine(outside_dir, self.task_id)

    def test_revision_escapes_untrusted_html_insights(self):
        insights_file = self.workspace / "insights.txt"
        insights_file.write_text("<script>alert('xss_attack')</script>", encoding="utf-8")
        (self.workspace / "report.html").unlink()

        qa_agent = IndependentQAAgent(self.workspace, self.task_id)
        failing_report = qa_agent.run_all_checks()
        self.assertFalse(failing_report.passed)

        rev = RevisionEngine(self.workspace, self.task_id, max_attempts=2)
        passed, final_report = rev.repair_and_retest(failing_report)

        self.assertTrue(passed)
        report_html = (self.workspace / "report.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>", report_html)
        self.assertIn("&lt;script&gt;", report_html)


class Milestone3AutonomousWorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        ensure_directories()
        init_db()
        with connect() as conn:
            conn.execute("DELETE FROM proposals")
            conn.execute("DELETE FROM revenue")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM opportunities")
            conn.execute("DELETE FROM audit_log")

    def test_autonomous_job_end_to_end_acceptance(self):
        # 1. Opportunity received & approved
        opp_data = {
            "title": "₹5,000 Retail Sales Analysis",
            "source": "Direct Client Referral",
            "url": "https://example.com/project/retail",
            "description": "Client requested sales data analysis, charts, data quality audit and HTML summary report.",
            "amount_inr": 5000.0,
            "estimated_hours": 3.0,
            "skill_match": 0.9,
            "payment_confidence": 0.9,
            "risk_level": 2.0,
            "source_permitted": True,
        }
        opp_id = add_opportunity(opp_data, score=1200.0)
        update_opportunity_status(opp_id, "approved_for_work")

        opp_record = get_opportunity(opp_id)
        self.assertIsNotNone(opp_record)
        self.assertEqual(opp_record["status"], "approved_for_work")

        # 2. Autonomous execution + QA verification pipeline
        result = run_autonomous_job(
            opportunity=opp_record,
            file_bytes=SAMPLE_CSV,
            filename="retail_sales.csv",
        )

        # 3. Verify deliverables & submission readiness
        self.assertTrue(result.submission_ready)
        self.assertEqual(result.status, "submission_ready")
        self.assertEqual(result.final_qa_report.score_pct, 100.0)
        self.assertTrue(result.artifact_zip.exists())

        # 4. Verify database state
        updated_opp = get_opportunity(opp_id)
        self.assertEqual(updated_opp["status"], "submission_ready")

        task = get_task(result.task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "submission_ready")
        self.assertIn("deliverables.zip", task["artifact_path"])

        # 5. Verify workspace contents
        ws = result.workspace
        self.assertTrue((ws / "business_analysis.xlsx").exists())
        self.assertTrue((ws / "cleaned_data.csv").exists())
        self.assertTrue((ws / "profile.json").exists())
        self.assertTrue((ws / "insights.txt").exists())
        self.assertTrue((ws / "report.html").exists())
        self.assertTrue((ws / "execution_plan.json").exists())
        self.assertTrue((ws / "qa_report.json").exists())


if __name__ == "__main__":
    unittest.main()
