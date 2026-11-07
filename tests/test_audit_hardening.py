from __future__ import annotations

import math
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import openpyxl
import pandas as pd

from core.analytics import create_analytics_bundle, is_formula_injection, sanitize_cell_value
from core.autonomous_worker import run_autonomous_job
from core.config import WORKSPACES_DIR, ensure_directories, safe_workspace_path
from core.db import (
    add_opportunity,
    add_revenue,
    connect,
    create_task,
    get_opportunity,
    init_db,
    update_opportunity_status,
    update_task_status,
)
from core.execution_engine import AutonomousExecutionEngine


class AuditHardeningTests(unittest.TestCase):
    def setUp(self):
        ensure_directories()
        init_db()
        with connect() as conn:
            conn.execute("DELETE FROM proposals")
            conn.execute("DELETE FROM revenue")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM opportunities")
            conn.execute("DELETE FROM audit_log")

    def test_cannot_record_paid_revenue_for_unsubmitted_opportunity(self):
        opp_id = add_opportunity(
            {
                "title": "Unsubmitted Lead",
                "source": "Direct",
                "amount_inr": 10000,
                "estimated_hours": 5,
                "skill_match": 1.0,
                "payment_confidence": 1.0,
                "risk_level": 1,
                "source_permitted": True,
            },
            score=1000,
        )

        # Opportunity is in 'new' status. Attempting to record revenue as 'paid' must raise ValueError
        with self.assertRaises(ValueError) as ctx:
            add_revenue("Advance Payment", 10000, "paid", opportunity_id=opp_id)
        self.assertIn("Cannot record confirmed 'paid' revenue", str(ctx.exception))

        # But recording expected or invoiced revenue is fine
        rev_id = add_revenue("Quoted pipeline", 10000, "expected", opportunity_id=opp_id)
        self.assertGreater(rev_id, 0)

    def test_database_illegal_status_transitions_blocked(self):
        opp_id = add_opportunity(
            {
                "title": "Strict Flow Gig",
                "source": "Direct",
                "amount_inr": 5000,
                "estimated_hours": 2,
                "skill_match": 1.0,
                "payment_confidence": 1.0,
                "risk_level": 1,
                "source_permitted": True,
            },
            score=1000,
        )

        # Opportunity starts in 'new'. It cannot jump straight to 'submitted_by_owner' or 'won'
        with self.assertRaises(ValueError) as ctx:
            update_opportunity_status(opp_id, "submitted_by_owner")
        self.assertIn("Illegal status transition", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            update_opportunity_status(opp_id, "won")
        self.assertIn("Illegal status transition", str(ctx.exception))

        # Legal path: new -> approved_for_work -> in_progress
        update_opportunity_status(opp_id, "approved_for_work")
        self.assertEqual(get_opportunity(opp_id)["status"], "approved_for_work")

        update_opportunity_status(opp_id, "in_progress")
        self.assertEqual(get_opportunity(opp_id)["status"], "in_progress")

        # Associated task must be marked 'submission_ready' with an artifact_path
        t_id = create_task("Deliverable Task", "analysis", "task_legal", opportunity_id=opp_id)
        update_task_status(t_id, "submission_ready", artifact_path="deliverables.zip")

        update_opportunity_status(opp_id, "submission_ready")
        self.assertEqual(get_opportunity(opp_id)["status"], "submission_ready")

        update_opportunity_status(opp_id, "submitted_by_owner")
        self.assertEqual(get_opportunity(opp_id)["status"], "submitted_by_owner")

        update_opportunity_status(opp_id, "won")
        self.assertEqual(get_opportunity(opp_id)["status"], "won")

    def test_dataset_with_negative_numbers_and_formulas(self):
        # CSV with negative sales, negative returns, and an injection attempt
        csv_bytes = (
            b"item,profit,delta,notes\n"
            b"Product A,-150.50,-10.0,Normal loss\n"
            b"Product B,250.00,+50.0,Profit\n"
            b"Product C,-25.00,-5.0,=cmd|' /C calc'!A0\n"
        )
        task_id = 918273
        artifact = create_analytics_bundle(csv_bytes, "test_negatives.csv", task_id)
        self.assertTrue(artifact.exists())

        workspace = safe_workspace_path(f"task_{task_id:04d}")
        wb = openpyxl.load_workbook(workspace / "business_analysis.xlsx", data_only=True)
        ws = wb["Cleaned Data"]

        # Row 2: Product A, profit should be -150.5 (numeric) or -150.50 without leading quote
        val_profit = str(ws.cell(row=2, column=2).value)
        self.assertFalse(val_profit.startswith("'"), f"Negative number was corrupted: {val_profit}")

        # Row 4: Product C, notes should be escaped with leading quote
        val_notes = str(ws.cell(row=4, column=4).value)
        self.assertTrue(val_notes.startswith("'="), f"Formula injection was not escaped: {val_notes}")
        wb.close()

    def test_duplicate_execution_reliability(self):
        opp_id = add_opportunity(
            {
                "title": "Idempotent Task",
                "source": "Direct",
                "amount_inr": 4000,
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

        sample_csv = b"col1,col2\n10,20\n30,40\n"

        # Run 1
        res1 = run_autonomous_job(opp, sample_csv, "idem.csv")
        self.assertTrue(res1.submission_ready)

        # Allow re-run: transition back to in_progress
        update_opportunity_status(opp_id, "in_progress")
        opp_updated = get_opportunity(opp_id)

        # Run 2 on same opportunity
        res2 = run_autonomous_job(opp_updated, sample_csv, "idem.csv")
        self.assertTrue(res2.submission_ready)
        self.assertTrue(res2.artifact_zip.exists())

    def test_formula_injection_in_csv_header_is_sanitized(self):
        csv_bytes = (
            b"=cmd,col2,@sum_hdr\n"
            b"10,20,30\n"
            b"40,50,60\n"
        )
        task_id = 918274
        artifact = create_analytics_bundle(csv_bytes, "test_hdr.csv", task_id)
        self.assertTrue(artifact.exists())

        workspace = safe_workspace_path(f"task_{task_id:04d}")
        wb = openpyxl.load_workbook(workspace / "business_analysis.xlsx", data_only=True)
        ws = wb["Cleaned Data"]

        hdr1 = str(ws.cell(row=1, column=1).value)
        hdr3 = str(ws.cell(row=1, column=3).value)
        self.assertTrue(hdr1.startswith("'="), f"Header formula injection not sanitized: {hdr1}")
        self.assertTrue(hdr3.startswith("'@"), f"Header formula injection not sanitized: {hdr3}")
        wb.close()

    def test_formula_injection_logic_preserves_negative_numbers(self):
        # Negative and positive valid numeric values MUST NOT be flagged as injection
        self.assertFalse(is_formula_injection("-50"))
        self.assertFalse(is_formula_injection("-12.34"))
        self.assertFalse(is_formula_injection("-0.005"))
        self.assertFalse(is_formula_injection("+100"))
        self.assertFalse(is_formula_injection("+42.5"))
        self.assertFalse(is_formula_injection(-50))
        self.assertFalse(is_formula_injection(-12.34))
        self.assertFalse(is_formula_injection(100))

        # Real formula injection payloads MUST be flagged
        self.assertTrue(is_formula_injection("=SUM(A1:A10)"))
        self.assertTrue(is_formula_injection("=cmd|' /C calc'!A0"))
        self.assertTrue(is_formula_injection("-2+3+cmd|' /C calc'!A0"))
        self.assertTrue(is_formula_injection("+cmd|' /C calc'!A0"))
        self.assertTrue(is_formula_injection("@HYPERLINK('http://attacker.com')"))
        self.assertTrue(is_formula_injection("=1+1"))

    def test_revenue_rejects_nan_and_infinity(self):
        with self.assertRaises(ValueError):
            add_revenue("Invalid NaN", float("nan"), "expected")

        with self.assertRaises(ValueError):
            add_revenue("Invalid Inf", float("inf"), "expected")

        with self.assertRaises(ValueError):
            add_revenue("Invalid -Inf", float("-inf"), "expected")

    def test_sanitize_cell_value_behavior(self):
        # Numbers preserved
        self.assertEqual(sanitize_cell_value("-500"), "-500")
        self.assertEqual(sanitize_cell_value("-12.50"), "-12.50")
        self.assertEqual(sanitize_cell_value("+250"), "+250")
        self.assertEqual(sanitize_cell_value("Regular Text"), "Regular Text")

        # Injections escaped
        self.assertEqual(sanitize_cell_value("=1+1"), "'=1+1")
        self.assertEqual(sanitize_cell_value("-2+3+cmd|..."), "'-2+3+cmd|...")
        self.assertEqual(sanitize_cell_value("@SUM(A1:A5)"), "'@SUM(A1:A5)")

    def test_submission_ready_requires_qa_passed_task(self):
        opp_id = add_opportunity(
            {
                "title": "Gated Gig",
                "source": "Direct",
                "amount_inr": 3000,
                "estimated_hours": 1,
                "skill_match": 1.0,
                "payment_confidence": 1.0,
                "risk_level": 1,
                "source_permitted": True,
            },
            score=1000,
        )
        update_opportunity_status(opp_id, "approved_for_work")
        update_opportunity_status(opp_id, "in_progress")

        # Without an associated task in submission_ready status, transition must fail
        with self.assertRaises(ValueError) as ctx:
            update_opportunity_status(opp_id, "submission_ready")
        self.assertIn("no associated task has completed QA", str(ctx.exception))

        # Adding a task in in_progress status is still not sufficient
        t_id = create_task("Incomplete Task", "analysis", "workspace_inc", opportunity_id=opp_id)
        with self.assertRaises(ValueError) as ctx:
            update_opportunity_status(opp_id, "submission_ready")
        self.assertIn("no associated task has completed QA", str(ctx.exception))

        # Once task is marked submission_ready with artifact path, transition succeeds
        update_task_status(t_id, "submission_ready", artifact_path="deliverables.zip")
        update_opportunity_status(opp_id, "submission_ready")
        self.assertEqual(get_opportunity(opp_id)["status"], "submission_ready")

    def test_unapproved_opportunity_cannot_execute(self):
        opp_id = add_opportunity(
            {
                "title": "Unapproved Job",
                "source": "Direct",
                "amount_inr": 2000,
                "estimated_hours": 1,
                "skill_match": 1.0,
                "payment_confidence": 1.0,
                "risk_level": 1,
                "source_permitted": True,
            },
            score=1000,
        )
        opp = get_opportunity(opp_id)
        sample_csv = b"a,b\n1,2\n"

        # Opportunity is in 'new' status (not approved for work)
        with self.assertRaises(ValueError) as ctx:
            run_autonomous_job(opp, sample_csv, "test.csv")
        self.assertIn("not approved for work", str(ctx.exception))

        # Direct execution engine call must also fail
        with self.assertRaises(ValueError) as ctx:
            AutonomousExecutionEngine().execute_job(opp, sample_csv, "test.csv")
        self.assertIn("not approved for work", str(ctx.exception))

    def test_unpermitted_opportunity_cannot_execute(self):
        opp_id = add_opportunity(
            {
                "title": "Unpermitted Source Job",
                "source": "Unauthorized Marketplace",
                "amount_inr": 5000,
                "estimated_hours": 2,
                "skill_match": 1.0,
                "payment_confidence": 1.0,
                "risk_level": 5,
                "source_permitted": False,
            },
            score=0,
        )
        opp = get_opportunity(opp_id)
        sample_csv = b"a,b\n1,2\n"

        with self.assertRaises(ValueError) as ctx:
            run_autonomous_job(opp, sample_csv, "test.csv")
        self.assertIn("unpermitted source", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            AutonomousExecutionEngine().execute_job(opp, sample_csv, "test.csv")
        self.assertIn("unpermitted source", str(ctx.exception))

    def test_workspace_security_path_traversal_blocked(self):
        # Escape attempts must raise ValueError
        with self.assertRaises(ValueError):
            safe_workspace_path("..", "system_file.txt")

        with self.assertRaises(ValueError):
            safe_workspace_path("..\\..\\Windows\\System32")

        with self.assertRaises(ValueError):
            safe_workspace_path("subfolder", "..", "..", "escape.txt")

    def test_workspace_symlink_escape_blocked(self):
        symlink_path = WORKSPACES_DIR / "test_symlink_dir"
        target_dir = Path(tempfile.gettempdir())
        try:
            symlink_path.symlink_to(target_dir, target_is_directory=True)
            with self.assertRaises(ValueError) as ctx:
                safe_workspace_path("test_symlink_dir", "secret.txt")
            self.assertIn("symlink", str(ctx.exception).lower())
            symlink_path.unlink()
        except OSError:
            orig_is_symlink = Path.is_symlink
            def mock_is_symlink(p):
                if p.name == "test_symlink_dir":
                    return True
                return orig_is_symlink(p)

            with patch.object(Path, "is_symlink", side_effect=mock_is_symlink, autospec=True):
                with self.assertRaises(ValueError) as ctx:
                    safe_workspace_path("test_symlink_dir", "secret.txt")
                self.assertIn("symlink", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
