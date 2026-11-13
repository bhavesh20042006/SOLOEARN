from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

import openpyxl
import pandas as pd

import core.config
from core.analytics import create_analytics_bundle, generate_executive_workbook
from core.config import ensure_directories, safe_workspace_path
from core.db import add_opportunity, connect, get_opportunity, init_db, update_opportunity_status
from core.execution_engine import AutonomousExecutionEngine, generate_execution_plan
from core.qa_agent import IndependentQAAgent
from core.service_executors import (
    detect_encoding,
    is_excel_file,
    read_tabular_dataset_streamed,
    stream_csv_chunks,
    stream_excel_chunks,
)


class DataDeliveryPipelineOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orig_db_path = core.config.DB_PATH
        tmp_path = Path(self.tmp_dir.name)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        core.config.DB_PATH = tmp_path / "data" / "test_delivery.sqlite3"
        ensure_directories()
        init_db()

    def tearDown(self):
        core.config.DB_PATH = self.orig_db_path
        self.tmp_dir.cleanup()

    def test_multi_encoding_detection(self):
        # 1. Standard UTF-8
        utf8_bytes = "col1,col2\nHello,World\n".encode("utf-8")
        self.assertEqual(detect_encoding(utf8_bytes), "utf-8")

        # 2. UTF-8 with BOM (utf-8-sig)
        bom_bytes = b"\xef\xbb\xbfname,score\nAlice,95\n"
        self.assertEqual(detect_encoding(bom_bytes), "utf-8-sig")

        # 3. Latin-1 / Windows-1252 with special European characters
        latin1_bytes = "nom,ville,montant\nRené,Montréal,150.50\n".encode("latin-1")
        detected = detect_encoding(latin1_bytes)
        self.assertIn(detected, ("latin-1", "iso-8859-1", "cp1252", "cp1250", "windows-1252"))

    def test_graceful_corrupt_rows_and_null_bytes_handling(self):
        # CSV containing an embedded null byte and a corrupt line with extra fields
        corrupt_csv = (
            b"id,name,value\n"
            b"1,Al\x00ice,100\n"
            b"2,CorruptRow,999,extra_column_1,extra_column_2\n"
            b"3,Bob,300\n"
        )
        df, meta = read_tabular_dataset_streamed(corrupt_csv, "test_corrupt.csv", chunksize=2)
        # Should gracefully skip corrupt row and remove null bytes
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df.columns), ["id", "name", "value"])
        self.assertIn("Alice", df["name"].tolist())
        self.assertIn("Bob", df["name"].tolist())

    def test_chunked_streaming_for_csv(self):
        # Generate multi-row CSV
        rows = [f"item_{i},{i * 10},{i * 1.5}" for i in range(1, 201)]
        csv_data = ("item,quantity,price\n" + "\n".join(rows) + "\n").encode("utf-8")

        chunks = list(stream_csv_chunks(csv_data, "test_stream.csv", chunksize=50))
        self.assertEqual(len(chunks), 4)  # 200 rows in chunks of 50
        total_streamed = sum(len(ch) for ch in chunks)
        self.assertEqual(total_streamed, 200)

        df, meta = read_tabular_dataset_streamed(csv_data, "test_stream.csv", chunksize=50)
        self.assertEqual(len(df), 200)
        self.assertIn(meta["format"], ("csv",))
        self.assertGreater(meta["memory_bytes"], 0)
        self.assertGreaterEqual(meta["memory_mb"], 0.0)

    def test_chunked_streaming_for_excel(self):
        # Create Excel workbook in memory
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "SalesData"
        ws.append(["transaction_id", "client", "amount"])
        for i in range(1, 151):
            ws.append([f"TXN-{i:04d}", f"Client {i % 10}", float(i * 12.5)])
        buf = io.BytesIO()
        wb.save(buf)
        excel_bytes = buf.getvalue()
        wb.close()

        self.assertTrue(is_excel_file("sales.xlsx", excel_bytes))

        # Test stream_excel_chunks
        chunks = list(stream_excel_chunks(excel_bytes, chunksize=50))
        self.assertEqual(len(chunks), 3)  # 150 rows in chunks of 50
        total_rows = sum(len(c) for c in chunks)
        self.assertEqual(total_rows, 150)

        # Test full streamed reader
        df, meta = read_tabular_dataset_streamed(excel_bytes, "sales.xlsx", chunksize=50)
        self.assertEqual(len(df), 150)
        self.assertEqual(meta["format"], "excel")
        self.assertIn("transaction_id", df.columns)

    def test_openpyxl_executive_summary_workbook_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_path = Path(temp_dir)
            xlsx_path = ws_path / "business_analysis.xlsx"

            df = pd.DataFrame({
                "product": ["Widget Alpha", "Widget Beta", "Widget Gamma"],
                "sales_revenue": [15000.50, 22400.00, 18900.25],
                "units": [15, 22, 18],
            })
            profile = {
                "source_filename": "corporate_sales.csv",
                "rows_original": 5,
                "rows_after_deduplication": 3,
                "duplicate_rows_removed": 2,
                "missing_cells": 0,
                "columns": [
                    {"name": "product", "dtype": "object", "missing": 0, "missing_percent": 0.0, "unique_values": 3},
                    {"name": "sales_revenue", "dtype": "float64", "missing": 0, "missing_percent": 0.0, "unique_values": 3},
                    {"name": "units", "dtype": "int64", "missing": 0, "missing_percent": 0.0, "unique_values": 3},
                ],
                "numeric_columns": ["sales_revenue", "units"],
                "categorical_columns": ["product"],
            }
            num_summary = df.select_dtypes(include="number").describe().T.reset_index(names="column")

            generate_executive_workbook(xlsx_path, df, profile, num_summary)

            self.assertTrue(xlsx_path.exists())
            wb = openpyxl.load_workbook(xlsx_path)

            # Check sheet structure
            sheet_names = wb.sheetnames
            self.assertIn("Executive Summary", sheet_names)
            self.assertIn("Cleaned Data", sheet_names)
            self.assertIn("Data Quality", sheet_names)
            self.assertIn("Numeric Summary", sheet_names)

            # Check Executive Summary styling and KPI cards
            ws_exec = wb["Executive Summary"]
            self.assertIn("EXECUTIVE BUSINESS INTELLIGENCE SUMMARY", str(ws_exec["B2"].value))
            self.assertEqual(ws_exec["B2"].fill.start_color.rgb, "001E293B")
            self.assertIn("INGESTED RECORDS", str(ws_exec["B5"].value))
            self.assertIn("CLEANED RECORDS", str(ws_exec["D5"].value))
            self.assertIn("DATA COMPLETENESS", str(ws_exec["F5"].value))

            # Check Cleaned Data dimensions match QA expectations
            ws_clean = wb["Cleaned Data"]
            self.assertEqual(ws_clean.max_row, len(df) + 1)
            self.assertEqual(ws_clean.max_column, len(df.columns))

            # Check auto-adjusted column width applied
            col_b_width = ws_clean.column_dimensions["B"].width
            self.assertGreater(col_b_width, 12)

            wb.close()

    def test_qa_agent_verifies_enhanced_workbook_and_html(self):
        sample_csv = (
            b"dept,headcount,budget,spend\n"
            b"Engineering,45,950000.0,890000.0\n"
            b"Marketing,20,400000.0,385000.0\n"
            b"Sales,35,750000.0,710000.0\n"
            b"Operations,15,300000.0,295000.0\n"
        )
        task_id = 884422
        artifact_zip = create_analytics_bundle(sample_csv, "department_budget.csv", task_id)
        self.assertTrue(artifact_zip.exists())

        workspace = safe_workspace_path(f"task_{task_id:04d}")
        qa = IndependentQAAgent(workspace, task_id)
        report = qa.run_all_checks()

        self.assertTrue(report.passed, f"QA failed with reasons: {report.failure_reasons}")
        self.assertEqual(report.score_pct, 100.0)

        # Inspect generated HTML report for accelerated interactivity features
        html_report = (workspace / "report.html").read_text(encoding="utf-8")
        self.assertIn("<!doctype html>", html_report.lower())
        self.assertIn("Business Data", html_report)
        self.assertIn("Analyst observations", html_report)
        self.assertIn("Data quality", html_report)
        self.assertIn("Charts", html_report)
        self.assertIn("tab-btn", html_report)
        self.assertIn("search-box", html_report)

    def test_excel_delivery_via_autonomous_execution_engine(self):
        # Create an Excel input dataset
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "MonthlyFinancials"
        ws.append(["quarter", "revenue_inr", "expense_inr", "margin"])
        ws.append(["Q1", 120000, 75000, 0.375])
        ws.append(["Q2", 150000, 90000, 0.400])
        ws.append(["Q3", 180000, 105000, 0.416])
        buf = io.BytesIO()
        wb.save(buf)
        excel_bytes = buf.getvalue()
        wb.close()

        opp_id = add_opportunity(
            {
                "title": "Corporate Financial Data Delivery",
                "source": "Direct",
                "description": "Analyze multi-sheet financial dataset in Excel format.",
                "amount_inr": 8000,
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
            file_bytes=excel_bytes,
            filename="quarterly_finances.xlsx",
        )

        self.assertTrue(workspace.exists())
        self.assertTrue((workspace / "business_analysis.xlsx").exists())
        self.assertTrue((workspace / "cleaned_data.csv").exists())
        self.assertTrue((workspace / "report.html").exists())
        self.assertTrue((workspace / "deliverables.zip").exists())

        qa = IndependentQAAgent(workspace, task_id)
        qa_report = qa.run_all_checks()
        self.assertTrue(qa_report.passed)
        self.assertEqual(qa_report.score_pct, 100.0)

    def test_multimegabyte_csv_and_excel_streaming_performance(self):
        # 1. Multi-megabyte CSV (15,000 rows, ~2-3 MB)
        csv_buffer = io.StringIO()
        csv_buffer.write("transaction_id,customer_name,region,revenue,cost,margin,status\n")
        for i in range(1, 15001):
            csv_buffer.write(f"TXN-{i:07d},Enterprise Client {i % 250},Region-{i % 8},{1000 + (i % 500) * 12.5},{600 + (i % 300) * 7.5},0.40,Completed\n")
        csv_bytes = csv_buffer.getvalue().encode("utf-8")
        self.assertGreater(len(csv_bytes), 1_000_000)  # > 1 MB

        # Stream reading with chunksize 5000
        df_csv, meta_csv = read_tabular_dataset_streamed(csv_bytes, "large_transactions.csv", chunksize=5000)
        self.assertEqual(len(df_csv), 15000)
        self.assertEqual(len(df_csv.columns), 7)
        self.assertEqual(meta_csv["rows"], 15000)
        self.assertGreater(meta_csv["memory_bytes"], 0)

        # 2. Multi-megabyte Excel file (5,000 rows, multi-column)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "HighVolumeData"
        ws.append(["id", "account", "category", "debit", "credit", "balance", "notes"])
        for i in range(1, 5001):
            ws.append([i, f"ACC-{i:06d}", f"Cat-{i % 12}", float(i * 10), float(i * 5), float(i * 5), f"Note for transaction {i}"])
        buf = io.BytesIO()
        wb.save(buf)
        excel_bytes = buf.getvalue()
        wb.close()
        self.assertGreater(len(excel_bytes), 100_000)

        df_excel, meta_excel = read_tabular_dataset_streamed(excel_bytes, "large_accounts.xlsx", chunksize=1000)
        self.assertEqual(len(df_excel), 5000)
        self.assertEqual(meta_excel["format"], "excel")
        self.assertEqual(meta_excel["rows"], 5000)


if __name__ == "__main__":
    unittest.main()
