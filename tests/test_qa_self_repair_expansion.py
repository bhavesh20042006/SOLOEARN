import csv
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd

from core.analytics import create_analytics_bundle, is_formula_injection, sanitize_cell_value
from core.autonomy_profitability_system import DeliveryJob, QualityEngine
from core.config import safe_workspace_path
from core.db import create_task, init_db
from core.qa_agent import IndependentQAAgent, QACheckResult, QAReport
from core.revision_engine import RevisionEngine


SAMPLE_CLEAN_CSV = b"""Order ID,Product Name,Quantity,Unit Price,Total Revenue
101,Laptop,2,45000.0,90000.0
102,Mouse,5,800.0,4000.0
103,Keyboard,3,1500.0,4500.0
104,Monitor,1,12000.0,12000.0
105,Headphones,4,2500.0,10000.0
"""


class QAEdgeCasesVerificationTests(unittest.TestCase):
    def setUp(self):
        self.task_id = 9101
        self.workspace = safe_workspace_path(f"test_qa_edge_{self.task_id}")
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _setup_valid_bundle(self):
        create_analytics_bundle(
            file_bytes=SAMPLE_CLEAN_CSV,
            filename="sales.csv",
            task_id=self.task_id,
        )
        # Copy files into test workspace
        src_ws = safe_workspace_path(f"task_{self.task_id:04d}")
        for item in src_ws.iterdir():
            if item.is_file():
                shutil.copy2(item, self.workspace / item.name)
            elif item.is_dir():
                shutil.copytree(item, self.workspace / item.name, dirs_exist_ok=True)

    def test_zero_row_csv_detection(self):
        self._setup_valid_bundle()
        # Overwrite cleaned_data.csv with a zero-row file (headers only)
        empty_csv = self.workspace / "cleaned_data.csv"
        empty_csv.write_text("Order ID,Product Name,Quantity,Unit Price,Total Revenue\n", encoding="utf-8")

        qa = IndependentQAAgent(self.workspace, self.task_id)
        report = qa.run_all_checks()

        self.assertFalse(report.passed)
        self.assertTrue(any("zero_row_safety" in r or "zero-row" in r.lower() for r in report.failure_reasons))
        self.assertFalse(report.edge_case_verifications["zero_row_safe"])

    def test_zero_byte_file_detection(self):
        self._setup_valid_bundle()
        # Truncate cleaned_data.csv to 0 bytes
        (self.workspace / "cleaned_data.csv").write_bytes(b"")

        qa = IndependentQAAgent(self.workspace, self.task_id)
        report = qa.run_all_checks()

        self.assertFalse(report.passed)
        self.assertTrue(any("empty" in r.lower() or "zero-row" in r.lower() for r in report.failure_reasons))

    def test_nan_propagation_mismatch_detected(self):
        self._setup_valid_bundle()
        # Corrupt Excel Numeric Summary so that it records a valid mean while actual CSV mean is NaN
        csv_path = self.workspace / "cleaned_data.csv"
        df = pd.read_csv(csv_path)
        # Make Unit Price all NaNs in CSV
        df["Unit Price"] = np.nan
        df.to_csv(csv_path, index=False)

        # In Excel Numeric Summary, leave the original numeric mean (e.g. 12460.0)
        qa = IndependentQAAgent(self.workspace, self.task_id)
        report = qa.run_all_checks()

        self.assertFalse(report.passed)
        # Should detect NaN propagation discrepancy or statistical anomaly
        self.assertTrue(
            any("nan" in r.lower() or "calculation_consistency" in r or "statistical_stability" in r for r in report.failure_reasons)
        )
        self.assertFalse(report.edge_case_verifications["nan_propagation_safe"])

    def test_division_by_zero_and_infinite_values_detected(self):
        self._setup_valid_bundle()
        # Inject infinite values (e.g. float('inf') from division by zero) into CSV
        csv_path = self.workspace / "cleaned_data.csv"
        df = pd.read_csv(csv_path)
        df.loc[0, "Total Revenue"] = float("inf")
        df.loc[1, "Total Revenue"] = float("-inf")
        df.to_csv(csv_path, index=False)

        qa = IndependentQAAgent(self.workspace, self.task_id)
        report = qa.run_all_checks()

        self.assertFalse(report.passed)
        self.assertTrue(any("infinite" in r.lower() or "statistical_stability" in r for r in report.failure_reasons))
        self.assertFalse(report.edge_case_verifications["division_by_zero_safe"])

    def test_extreme_statistical_magnitude_anomaly_detected(self):
        self._setup_valid_bundle()
        # Inject extreme overflow value > 1e15
        csv_path = self.workspace / "cleaned_data.csv"
        df = pd.read_csv(csv_path)
        df.loc[0, "Total Revenue"] = 1e20
        df.to_csv(csv_path, index=False)

        qa = IndependentQAAgent(self.workspace, self.task_id)
        report = qa.run_all_checks()

        self.assertFalse(report.passed)
        self.assertTrue(any("magnitude" in r.lower() or "overflow" in r.lower() or "statistical_stability" in r for r in report.failure_reasons))
        self.assertFalse(report.edge_case_verifications["extreme_anomalies_safe"])


class HardenedFormulaInjectionTests(unittest.TestCase):
    def setUp(self):
        self.task_id = 9102
        self.workspace = safe_workspace_path(f"test_formula_inj_{self.task_id}")
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _setup_valid_bundle(self):
        create_analytics_bundle(
            file_bytes=SAMPLE_CLEAN_CSV,
            filename="sales.csv",
            task_id=self.task_id,
        )
        src_ws = safe_workspace_path(f"task_{self.task_id:04d}")
        for item in src_ws.iterdir():
            if item.is_file():
                shutil.copy2(item, self.workspace / item.name)
            elif item.is_dir():
                shutil.copytree(item, self.workspace / item.name, dirs_exist_ok=True)

    def test_is_formula_injection_thorough(self):
        # Valid safe numbers must not trigger
        self.assertFalse(is_formula_injection("-50"))
        self.assertFalse(is_formula_injection("+100"))
        self.assertFalse(is_formula_injection("-12.34"))
        self.assertFalse(is_formula_injection("0.0"))
        self.assertFalse(is_formula_injection(42))
        self.assertFalse(is_formula_injection(-99.9))

        # Already escaped formula must not trigger
        self.assertFalse(is_formula_injection("'=SUM(A1:A10)"))
        self.assertFalse(is_formula_injection("'-2+3+cmd"))
        self.assertFalse(is_formula_injection("'@HYPERLINK"))

        # Dangerous payloads must trigger
        self.assertTrue(is_formula_injection("=cmd|' /C calc'!A0"))
        self.assertTrue(is_formula_injection("-2+3+cmd|' /C calc'!A0"))
        self.assertTrue(is_formula_injection("+cmd|' /C calc'!A0"))
        self.assertTrue(is_formula_injection("@HYPERLINK('http://attacker.com')"))
        self.assertTrue(is_formula_injection("|cmd|' /C calc'!A0"))
        self.assertTrue(is_formula_injection("\t=1+1"))
        self.assertTrue(is_formula_injection("\r=cmd"))
        self.assertTrue(is_formula_injection("\x00=1+1"))

    def test_qa_detects_formula_injection_in_csv_cells(self):
        self._setup_valid_bundle()
        # Inject formula payload into cleaned_data.csv data row
        csv_path = self.workspace / "cleaned_data.csv"
        df = pd.read_csv(csv_path)
        df.loc[0, "Product Name"] = "=cmd|' /C calc'!A0"
        df.to_csv(csv_path, index=False)

        qa = IndependentQAAgent(self.workspace, self.task_id)
        report = qa.run_all_checks()

        self.assertFalse(report.passed)
        self.assertTrue(any("formula_injection_safety" in r or "formula" in r.lower() for r in report.failure_reasons))
        self.assertFalse(report.edge_case_verifications["formula_injection_clean"])

    def test_qa_detects_formula_injection_in_csv_headers(self):
        self._setup_valid_bundle()
        # Inject formula payload into column header of cleaned_data.csv
        csv_path = self.workspace / "cleaned_data.csv"
        df = pd.read_csv(csv_path)
        df = df.rename(columns={"Product Name": "=SUM(A1:A10)"})
        df.to_csv(csv_path, index=False)

        qa = IndependentQAAgent(self.workspace, self.task_id)
        report = qa.run_all_checks()

        self.assertFalse(report.passed)
        self.assertTrue(any("formula" in r.lower() for r in report.failure_reasons))

    def test_qa_detects_formula_injection_in_text_report(self):
        self._setup_valid_bundle()
        # Inject formula payload into insights.txt
        insights_path = self.workspace / "insights.txt"
        insights_path.write_text("=cmd|' /C calc'!A0\nNormal observation line.\n", encoding="utf-8")

        qa = IndependentQAAgent(self.workspace, self.task_id)
        report = qa.run_all_checks()

        self.assertFalse(report.passed)
        self.assertTrue(any("formula" in r.lower() for r in report.failure_reasons))


class AutomatedSelfRepairCycleTests(unittest.TestCase):
    def setUp(self):
        self.task_id = 9103
        self.workspace = safe_workspace_path(f"test_self_repair_{self.task_id}")
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _setup_valid_bundle(self):
        create_analytics_bundle(
            file_bytes=SAMPLE_CLEAN_CSV,
            filename="sales.csv",
            task_id=self.task_id,
        )
        src_ws = safe_workspace_path(f"task_{self.task_id:04d}")
        for item in src_ws.iterdir():
            if item.is_file():
                shutil.copy2(item, self.workspace / item.name)
            elif item.is_dir():
                shutil.copytree(item, self.workspace / item.name, dirs_exist_ok=True)

    def test_repair_fixes_formula_injection_discrepancy(self):
        self._setup_valid_bundle()
        # Inject unescaped formula into cleaned_data.csv
        csv_path = self.workspace / "cleaned_data.csv"
        df = pd.read_csv(csv_path)
        df.loc[0, "Product Name"] = "=cmd|' /C calc'!A0"
        df.to_csv(csv_path, index=False)

        qa = IndependentQAAgent(self.workspace, self.task_id)
        failing_report = qa.run_all_checks()
        self.assertFalse(failing_report.passed)

        # Run 3-cycle automated self repair
        rev = RevisionEngine(self.workspace, self.task_id, max_attempts=3)
        passed, final_report = rev.repair_and_retest(failing_report)

        self.assertTrue(passed)
        self.assertTrue(final_report.passed)
        self.assertLessEqual(len(rev.history), 3)

        # Verify that CSV was sanitized with a leading quote
        repaired_df = pd.read_csv(csv_path)
        self.assertTrue(repaired_df.loc[0, "Product Name"].startswith("'="))

    def test_repair_fixes_corrupt_charts_and_missing_excel(self):
        self._setup_valid_bundle()
        # Remove Excel workbook and corrupt charts
        (self.workspace / "business_analysis.xlsx").unlink()
        chart_dir = self.workspace / "charts"
        for p in chart_dir.glob("*.png"):
            p.write_bytes(b"corrupted_png_bytes")

        qa = IndependentQAAgent(self.workspace, self.task_id)
        failing_report = qa.run_all_checks()
        self.assertFalse(failing_report.passed)

        rev = RevisionEngine(self.workspace, self.task_id, max_attempts=3)
        passed, final_report = rev.repair_and_retest(failing_report)

        self.assertTrue(passed)
        self.assertTrue(final_report.passed)
        self.assertTrue((self.workspace / "business_analysis.xlsx").exists())
        self.assertTrue((self.workspace / "deliverables.zip").exists())

    def test_repair_fixes_division_by_zero_infinite_values(self):
        self._setup_valid_bundle()
        # Inject infinite values into numeric float column
        csv_path = self.workspace / "cleaned_data.csv"
        df = pd.read_csv(csv_path)
        df["Total Revenue"] = df["Total Revenue"].astype(float)
        df.loc[0, "Total Revenue"] = float("inf")
        df.to_csv(csv_path, index=False)

        qa = IndependentQAAgent(self.workspace, self.task_id)
        failing_report = qa.run_all_checks()
        self.assertFalse(failing_report.passed)

        rev = RevisionEngine(self.workspace, self.task_id, max_attempts=3)
        passed, final_report = rev.repair_and_retest(failing_report)

        self.assertTrue(passed)
        self.assertTrue(final_report.passed)

        # Verify infinite values were replaced with null/None
        repaired_df = pd.read_csv(csv_path)
        self.assertFalse(np.isinf(repaired_df["Total Revenue"].dropna()).any())

    def test_repair_strictly_bounds_at_max_3_attempts(self):
        # Empty workspace cannot be repaired (unrecoverable missing raw data)
        qa = IndependentQAAgent(self.workspace, self.task_id)
        failing_report = qa.run_all_checks()
        self.assertFalse(failing_report.passed)

        rev = RevisionEngine(self.workspace, self.task_id, max_attempts=3)
        passed, final_report = rev.repair_and_retest(failing_report)

        self.assertFalse(passed)
        self.assertEqual(len(rev.history), 3)
        self.assertTrue((self.workspace / "revision_history.json").exists())


class MachineReadableQAReportTests(unittest.TestCase):
    def setUp(self):
        self.task_id = 9104
        self.workspace = safe_workspace_path(f"test_qa_json_{self.task_id}")
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _setup_valid_bundle(self):
        create_analytics_bundle(
            file_bytes=SAMPLE_CLEAN_CSV,
            filename="sales.csv",
            task_id=self.task_id,
        )
        src_ws = safe_workspace_path(f"task_{self.task_id:04d}")
        for item in src_ws.iterdir():
            if item.is_file():
                shutil.copy2(item, self.workspace / item.name)
            elif item.is_dir():
                shutil.copytree(item, self.workspace / item.name, dirs_exist_ok=True)

    def test_qa_report_json_contains_all_structured_fields(self):
        self._setup_valid_bundle()
        qa = IndependentQAAgent(self.workspace, self.task_id)
        report = qa.run_all_checks()

        self.assertTrue(report.passed)
        json_path = self.workspace / "qa_report.json"
        self.assertTrue(json_path.exists())

        data = json.loads(json_path.read_text(encoding="utf-8"))

        # Verify top-level machine-readable fields
        self.assertIn("audit_id", data)
        self.assertTrue(data["audit_id"].startswith("QA-9104-"))
        self.assertEqual(data["task_id"], 9104)
        self.assertEqual(data["status"], "PASSED")
        self.assertEqual(data["service_tier"], "data_analytics")
        self.assertIsInstance(data["score_pct"], float)
        self.assertEqual(data["total_checks"], len(data["checks"]))
        self.assertEqual(data["passed_checks"], data["total_checks"])
        self.assertEqual(data["failed_checks"], 0)

        # Verify edge case verifications
        edge_cases = data["edge_case_verifications"]
        self.assertTrue(edge_cases["zero_row_safe"])
        self.assertTrue(edge_cases["nan_propagation_safe"])
        self.assertTrue(edge_cases["division_by_zero_safe"])
        self.assertTrue(edge_cases["extreme_anomalies_safe"])
        self.assertTrue(edge_cases["formula_injection_clean"])

        # Verify metrics
        metrics = data["metrics"]
        self.assertIn("execution_time_seconds", metrics)
        self.assertGreater(metrics["execution_time_seconds"], 0.0)

        # Verify checks structure
        for check in data["checks"]:
            self.assertIn("check_name", check)
            self.assertIn("category", check)
            self.assertIn("severity", check)
            self.assertIn("passed", check)
            self.assertIn("summary", check)
            self.assertIn("details", check)

    def test_quality_engine_inspect_and_verify_outputs_structured_result(self):
        self._setup_valid_bundle()
        init_db()
        t_id = create_task(
            title="QA Expansion Test",
            task_type="autonomous_data_analytics",
            workspace=str(self.workspace),
            is_sandbox=True,
        )
        job = DeliveryJob(
            task_id=t_id,
            opportunity_id=None,
            client_name="Test Enterprise",
            service_tier="data_analytics",
            workspace=str(self.workspace),
            status="qa_ready",
        )

        audit_res = QualityEngine.inspect_and_verify(job, max_revisions=3)

        self.assertTrue(audit_res.passed)
        self.assertEqual(audit_res.status, "submission_ready")
        self.assertTrue(audit_res.audit_id.startswith(f"QA-{t_id}-"))
        self.assertTrue(Path(audit_res.qa_report_path).exists())
        self.assertTrue(audit_res.edge_case_status["zero_row_safe"])
        self.assertTrue(audit_res.edge_case_status["formula_injection_clean"])
        self.assertEqual(audit_res.qa_report["status"], "PASSED")


if __name__ == "__main__":
    unittest.main()
