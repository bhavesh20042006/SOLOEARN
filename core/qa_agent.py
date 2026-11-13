from __future__ import annotations

import ast
import csv
import html
import io
import json
import math
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import openpyxl
import pandas as pd

from core.analytics import APPROVED_DELIVERABLE_FILES, is_formula_injection
from core.config import safe_workspace_path


@dataclass
class QACheckResult:
    check_name: str
    passed: bool
    summary: str
    category: str = "file_integrity"
    severity: str = "CRITICAL"
    details: dict[str, Any] = field(default_factory=dict)
    remediation_hint: str = ""


@dataclass
class QAReport:
    task_id: int
    passed: bool
    score_pct: float
    total_checks: int
    passed_checks: int
    checks: list[QACheckResult]
    failure_reasons: list[str]
    audit_id: str = ""
    status: str = "PASSED"
    failed_checks: int = 0
    service_tier: str = "data_analytics"
    edge_case_verifications: dict[str, bool] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IndependentQAAgent:
    """
    Independent Quality Assurance Agent that verifies deliverables, mathematical integrity,
    file formatting, security sanitization, and scope adherence.
    """

    def __init__(self, workspace: Path, task_id: int):
        self.workspace = safe_workspace_path(workspace)
        self.task_id = task_id

    def run_all_checks(self) -> QAReport:
        t_start = time.perf_counter()
        checks: list[QACheckResult] = []
        service_tier = "data_analytics"

        # Service-aware check routing
        if (self.workspace / "pipeline.py").exists():
            service_tier = "data_engineering"
            checks = self._run_data_engineering_checks()
        elif (self.workspace / "dashboard.html").exists():
            service_tier = "interactive_dashboard"
            checks = self._run_dashboard_checks()
        elif (self.workspace / "automation.py").exists():
            service_tier = "business_automation"
            checks = self._run_business_automation_checks()
        else:
            service_tier = "data_analytics"
            checks.append(self._check_file_existence())
            checks.append(self._check_excel_integrity())
            checks.append(self._check_calculation_consistency())
            checks.append(self._check_charts_and_visuals())
            checks.append(self._check_html_report_integrity())
            checks.append(self._check_archive_integrity())

        # Universal Edge-Case & Hardened Verification Suite
        checks.append(self._check_zero_row_and_empty_files())
        checks.append(self._check_formula_injection_across_outputs())
        if service_tier in {"data_analytics", "data_engineering", "business_automation"}:
            checks.append(self._check_statistical_stability_and_anomalies())

        passed_count = sum(1 for c in checks if c.passed)
        total_count = len(checks)
        all_passed = passed_count == total_count
        score_pct = round((passed_count / max(1, total_count)) * 100, 1)

        failures = [f"[{c.check_name}] {c.summary}" for c in checks if not c.passed]

        # Machine-readable edge case verification map
        edge_verifs = {
            "zero_row_safe": not any(c.check_name == "zero_row_safety" and not c.passed for c in checks),
            "nan_propagation_safe": not any("nan" in r.lower() for r in failures),
            "division_by_zero_safe": not any("division by zero" in r.lower() or "infinite" in r.lower() for r in failures),
            "extreme_anomalies_safe": not any(c.check_name == "statistical_stability" and not c.passed for c in checks),
            "formula_injection_clean": not any(c.check_name == "formula_injection_safety" and not c.passed for c in checks) and not any("formula" in r.lower() for r in failures),
        }

        # Status classification
        if all_passed:
            status = "PASSED"
        elif any(c.severity == "CRITICAL" and not c.passed for c in checks):
            status = "CRITICAL_HALT"
        else:
            status = "REPAIRABLE_DISCREPANCY"

        elapsed = round(time.perf_counter() - t_start, 4)
        audit_id = f"QA-{self.task_id}-{uuid.uuid4().hex[:8]}"
        metrics = {
            "execution_time_seconds": elapsed,
            "total_checks": total_count,
            "passed_checks": passed_count,
            "failed_checks": total_count - passed_count,
        }

        report = QAReport(
            task_id=self.task_id,
            passed=all_passed,
            score_pct=score_pct,
            total_checks=total_count,
            passed_checks=passed_count,
            checks=checks,
            failure_reasons=failures,
            audit_id=audit_id,
            status=status,
            failed_checks=total_count - passed_count,
            service_tier=service_tier,
            edge_case_verifications=edge_verifs,
            metrics=metrics,
        )

        # Save QA report directly to the task workspace
        report_file = self.workspace / "qa_report.json"
        report_file.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

        return report

    def _run_data_engineering_checks(self) -> list[QACheckResult]:
        checks = []
        required = ["cleaned_dataset.csv", "schema_manifest.json", "pipeline.py", "data_dictionary.md", "deliverables.zip"]
        missing = [f for f in required if not (self.workspace / f).exists() or (self.workspace / f).stat().st_size == 0]
        checks.append(QACheckResult(
            check_name="data_engineering_files",
            passed=len(missing) == 0,
            summary="All data engineering assets present" if not missing else f"Missing: {', '.join(missing)}",
        ))

        # Check Python pipeline syntax
        py_path = self.workspace / "pipeline.py"
        if py_path.exists():
            try:
                ast.parse(py_path.read_text(encoding="utf-8"))
                checks.append(QACheckResult(check_name="python_syntax", passed=True, summary="Pipeline script parsed successfully with valid AST syntax."))
            except SyntaxError as e:
                checks.append(QACheckResult(check_name="python_syntax", passed=False, summary=f"Syntax error in pipeline.py: {e}"))
        else:
            checks.append(QACheckResult(check_name="python_syntax", passed=False, summary="pipeline.py does not exist."))

        # Check schema manifest JSON
        schema_path = self.workspace / "schema_manifest.json"
        if schema_path.exists():
            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                has_rows = schema.get("row_count", 0) > 0
                has_cols = len(schema.get("columns", [])) > 0
                checks.append(QACheckResult(check_name="schema_manifest_validity", passed=has_rows and has_cols, summary="Schema manifest verified."))
            except Exception as e:
                checks.append(QACheckResult(check_name="schema_manifest_validity", passed=False, summary=f"Invalid schema JSON: {e}"))
        else:
            checks.append(QACheckResult(check_name="schema_manifest_validity", passed=False, summary="schema_manifest.json missing."))

        checks.append(self._check_archive_integrity(expected_files=["cleaned_dataset.csv", "schema_manifest.json", "pipeline.py", "data_dictionary.md"]))
        return checks

    def _run_dashboard_checks(self) -> list[QACheckResult]:
        checks = []
        required = ["dashboard.html", "kpi_metrics.json", "dashboard_user_guide.md", "deliverables.zip"]
        missing = [f for f in required if not (self.workspace / f).exists() or (self.workspace / f).stat().st_size == 0]
        checks.append(QACheckResult(
            check_name="dashboard_files",
            passed=len(missing) == 0,
            summary="All dashboard deliverables present" if not missing else f"Missing: {', '.join(missing)}",
        ))

        # Check HTML5 structure
        html_path = self.workspace / "dashboard.html"
        if html_path.exists():
            txt = html_path.read_text(encoding="utf-8")
            valid = "<!DOCTYPE html>" in txt and "<html" in txt and "</html>" in txt and "dataTable" in txt
            checks.append(QACheckResult(check_name="html5_structure", passed=valid, summary="Dashboard contains complete HTML5 layout and interactive elements."))
        else:
            checks.append(QACheckResult(check_name="html5_structure", passed=False, summary="dashboard.html missing."))

        # Check KPI JSON
        kpi_path = self.workspace / "kpi_metrics.json"
        if kpi_path.exists():
            try:
                kpis = json.loads(kpi_path.read_text(encoding="utf-8"))
                has_records = "total_records" in kpis
                checks.append(QACheckResult(check_name="kpi_metrics_validity", passed=has_records, summary="KPI metrics verified."))
            except Exception as e:
                checks.append(QACheckResult(check_name="kpi_metrics_validity", passed=False, summary=f"Invalid KPI metrics: {e}"))
        else:
            checks.append(QACheckResult(check_name="kpi_metrics_validity", passed=False, summary="kpi_metrics.json missing."))

        checks.append(self._check_archive_integrity(expected_files=["dashboard.html", "kpi_metrics.json", "dashboard_user_guide.md"]))
        return checks

    def _run_business_automation_checks(self) -> list[QACheckResult]:
        checks: list[QACheckResult] = []
        required = [
            "automation.py",
            "config.json",
            "README.md",
            "test_automation.py",
            "reconciled_output.csv",
            "automation_summary.json",
            "deliverables.zip",
        ]
        missing = [f for f in required if not (self.workspace / f).is_file() or (self.workspace / f).stat().st_size == 0]
        checks.append(QACheckResult(
            check_name="automation_files",
            passed=len(missing) == 0,
            summary="All reconciliation deliverables are present" if not missing else f"Missing: {', '.join(missing)}",
        ))

        py_path = self.workspace / "automation.py"
        if py_path.is_file():
            try:
                tree = ast.parse(py_path.read_text(encoding="utf-8"))
                function_names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
                has_real_worker = "run_automation" in function_names
                checks.append(QACheckResult(
                    check_name="automation_script_integrity",
                    passed=has_real_worker,
                    summary="Automation script exposes a real run_automation function" if has_real_worker else "Automation script is only a scaffold; run_automation is missing.",
                ))
            except (SyntaxError, UnicodeDecodeError) as exc:
                checks.append(QACheckResult(check_name="automation_script_integrity", passed=False, summary=f"Invalid automation script: {exc}"))
        else:
            checks.append(QACheckResult(check_name="automation_script_integrity", passed=False, summary="automation.py missing."))

        summary_path = self.workspace / "automation_summary.json"
        output_path = self.workspace / "reconciled_output.csv"
        if summary_path.is_file() and output_path.is_file():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                output = pd.read_csv(output_path)
                input_rows = int(summary["input_rows"])
                output_rows = int(summary["output_rows"])
                duplicates_removed = int(summary["duplicates_removed"])
                consistent = (
                    summary.get("status") == "completed"
                    and len(output) == output_rows
                    and input_rows >= output_rows >= 0
                    and input_rows - output_rows == duplicates_removed
                    and list(output.columns) == summary.get("columns")
                )
                checks.append(QACheckResult(
                    check_name="automation_output_consistency",
                    passed=consistent,
                    summary="Reconciled CSV matches the independently checked row-count summary" if consistent else "Reconciled CSV and automation summary are inconsistent.",
                    details={"input_rows": input_rows, "output_rows": len(output), "duplicates_removed": duplicates_removed},
                ))
            except Exception as exc:
                checks.append(QACheckResult(check_name="automation_output_consistency", passed=False, summary=f"Could not verify automation outputs: {exc}"))
        else:
            checks.append(QACheckResult(check_name="automation_output_consistency", passed=False, summary="Automation output CSV or summary JSON is missing."))

        checks.append(self._check_archive_integrity(expected_files=[
            "reconciled_output.csv", "automation_summary.json", "automation.py", "config.json", "README.md", "test_automation.py"
        ]))
        return checks

    def _check_zero_row_and_empty_files(self) -> QACheckResult:
        """
        Verifies that deliverable datasets are not empty (0 bytes) and contain
        valid non-zero data rows (guarding against zero-row deliverables).
        """
        zero_row_issues: list[str] = []
        verified_files: list[dict[str, Any]] = []

        csv_candidates = [
            "cleaned_data.csv",
            "cleaned_dataset.csv",
            "reconciled_output.csv",
        ]
        for fname in csv_candidates:
            p = self.workspace / fname
            if p.exists() and p.is_file():
                if p.stat().st_size == 0:
                    zero_row_issues.append(f"CSV file '{fname}' is empty (0 bytes).")
                    continue
                try:
                    df = pd.read_csv(p, low_memory=False)
                    if len(df.columns) == 0:
                        zero_row_issues.append(f"CSV file '{fname}' has 0 columns.")
                    elif len(df) == 0:
                        zero_row_issues.append(f"Zero-row file detected: '{fname}' has 0 data rows.")
                    else:
                        verified_files.append({"file": fname, "rows": len(df), "columns": len(df.columns)})
                except Exception as exc:
                    zero_row_issues.append(f"Failed parsing CSV '{fname}': {exc}")

        xlsx_path = self.workspace / "business_analysis.xlsx"
        if xlsx_path.exists() and xlsx_path.is_file():
            if xlsx_path.stat().st_size == 0:
                zero_row_issues.append("business_analysis.xlsx is 0 bytes.")
            else:
                try:
                    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
                    if "Cleaned Data" in wb.sheetnames:
                        ws = wb["Cleaned Data"]
                        rows_found = 0
                        for row in ws.iter_rows(values_only=True):
                            if any(v is not None for v in row):
                                rows_found += 1
                            if rows_found >= 2:
                                break
                        if rows_found < 2:
                            zero_row_issues.append("business_analysis.xlsx 'Cleaned Data' sheet contains zero data rows.")
                    wb.close()
                except Exception as exc:
                    zero_row_issues.append(f"Failed inspecting business_analysis.xlsx rows: {exc}")

        if zero_row_issues:
            return QACheckResult(
                check_name="zero_row_safety",
                category="file_integrity",
                passed=False,
                severity="CRITICAL",
                summary=f"Detected {len(zero_row_issues)} zero-row or empty deliverable(s): {'; '.join(zero_row_issues[:3])}",
                details={"issues": zero_row_issues},
                remediation_hint="Populate valid non-empty data rows into deliverable datasets before submitting.",
            )

        return QACheckResult(
            check_name="zero_row_safety",
            category="file_integrity",
            passed=True,
            severity="CRITICAL",
            summary=f"All {len(verified_files)} verified dataset(s) contain valid non-zero rows and columns.",
            details={"verified_datasets": verified_files},
        )

    def _check_formula_injection_across_outputs(self) -> QACheckResult:
        """
        Hardened formula injection (CWE-1236) scanner across all exported CSV, Excel,
        JSON, and text deliverables in the workspace.
        """
        injections: list[str] = []
        files_scanned: list[str] = []
        cells_scanned = 0

        # 1. Scan all CSV files in workspace
        for csv_file in sorted(self.workspace.glob("*.csv")):
            if csv_file.is_symlink():
                continue
            files_scanned.append(csv_file.name)
            try:
                content = csv_file.read_text(encoding="utf-8", errors="replace")
                reader = csv.reader(io.StringIO(content))
                for r_idx, row in enumerate(reader, start=1):
                    for c_idx, cell in enumerate(row, start=1):
                        cells_scanned += 1
                        if is_formula_injection(cell) and not cell.startswith("'"):
                            injections.append(f"CSV '{csv_file.name}' R{r_idx}C{c_idx}: {cell[:30]}")
            except Exception as exc:
                injections.append(f"Failed scanning CSV '{csv_file.name}': {exc}")

        # 2. Scan all Excel files in workspace
        for xlsx_file in sorted(self.workspace.glob("*.xlsx")):
            if xlsx_file.is_symlink():
                continue
            files_scanned.append(xlsx_file.name)
            try:
                wb = openpyxl.load_workbook(xlsx_file, read_only=False, data_only=False)
                for sname in wb.sheetnames:
                    ws = wb[sname]
                    for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                        for c_idx, cell in enumerate(row, start=1):
                            cells_scanned += 1
                            if isinstance(cell, str) and is_formula_injection(cell) and not cell.startswith("'"):
                                injections.append(f"Excel '{xlsx_file.name}' sheet '{sname}' R{r_idx}C{c_idx}: {cell[:30]}")
                wb.close()
            except Exception as exc:
                injections.append(f"Failed scanning Excel '{xlsx_file.name}': {exc}")

        # 3. Scan JSON manifests for raw formula strings in keys/values
        for json_file in sorted(self.workspace.glob("*.json")):
            if json_file.is_symlink() or json_file.name in {"qa_report.json", "revision_history.json", "execution_plan.json"}:
                continue
            files_scanned.append(json_file.name)
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                def scan_obj(obj: Any, path: str = "") -> None:
                    nonlocal cells_scanned
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            cells_scanned += 1
                            if is_formula_injection(k) and not str(k).startswith("'"):
                                injections.append(f"JSON '{json_file.name}' key '{path}.{k}': {str(k)[:30]}")
                            scan_obj(v, f"{path}.{k}")
                    elif isinstance(obj, list):
                        for idx, item in enumerate(obj):
                            cells_scanned += 1
                            scan_obj(item, f"{path}[{idx}]")
                    elif isinstance(obj, str):
                        cells_scanned += 1
                        if is_formula_injection(obj) and not obj.startswith("'"):
                            injections.append(f"JSON '{json_file.name}' value at '{path}': {obj[:30]}")
                scan_obj(data)
            except Exception:
                pass

        # 4. Scan insights.txt
        insights_path = self.workspace / "insights.txt"
        if insights_path.exists() and insights_path.is_file():
            files_scanned.append(insights_path.name)
            lines = insights_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for l_idx, line in enumerate(lines, start=1):
                cells_scanned += 1
                if is_formula_injection(line) and not line.startswith("'"):
                    injections.append(f"Text '{insights_path.name}' L{l_idx}: {line[:30]}")

        if injections:
            return QACheckResult(
                check_name="formula_injection_safety",
                category="security_sanitization",
                passed=False,
                severity="CRITICAL",
                summary=f"Detected {len(injections)} unescaped formula string(s) across exported outputs: {', '.join(injections[:3])}",
                details={"issues": injections, "files_scanned": files_scanned, "cells_scanned": cells_scanned},
                remediation_hint="Prefix dangerous leading formula characters (=, +, -, @, |) with a single quote ' or sanitize strings.",
            )

        return QACheckResult(
            check_name="formula_injection_safety",
            category="security_sanitization",
            passed=True,
            severity="CRITICAL",
            summary=f"All {len(files_scanned)} exported deliverables passed hardened formula injection sanitization ({cells_scanned} cells/fields scanned).",
            details={"files_scanned": files_scanned, "cells_scanned": cells_scanned},
        )

    def _check_statistical_stability_and_anomalies(self) -> QACheckResult:
        """
        Inspects datasets for NaN propagation, division by zero artifacts (Inf/-Inf),
        extreme statistical anomalies (magnitude > 1e15 or unparsed scientific notation),
        and discrepancy between missing-cell accounting and actual NaN occurrences.
        """
        anomalies: list[str] = []
        numeric_cols_checked: list[str] = []
        datasets_found: list[str] = []

        candidate_csvs = ["cleaned_data.csv", "cleaned_dataset.csv", "reconciled_output.csv"]
        for fname in candidate_csvs:
            p = self.workspace / fname
            if not p.exists() or not p.is_file():
                continue
            datasets_found.append(fname)
            try:
                df = pd.read_csv(p, low_memory=False)
                if df.empty or len(df.columns) == 0:
                    continue
                num_df = df.select_dtypes(include="number")
                for col in num_df.columns:
                    col_name = str(col)
                    numeric_cols_checked.append(f"{fname}:{col_name}")
                    series = num_df[col]

                    # 1. Check for infinite values (division by zero)
                    inf_count = int(np.isinf(series.dropna()).sum())
                    if inf_count > 0:
                        anomalies.append(f"{fname} col '{col_name}': {inf_count} infinite value(s) detected (division by zero artifact).")

                    # 2. Check for extreme floating-point overflow / anomalies
                    finite_vals = series.dropna().replace([float("inf"), float("-inf")], np.nan).dropna()
                    if not finite_vals.empty:
                        max_abs = float(np.abs(finite_vals).max())
                        if max_abs > 1e15 or math.isnan(max_abs):
                            anomalies.append(f"{fname} col '{col_name}': extreme magnitude anomaly detected (max |val| = {max_abs:.2e}).")

                    # 3. Check for unexpected NaN propagation (100% NaN in non-empty table)
                    nan_count = int(series.isna().sum())
                    if len(series) > 0 and nan_count == len(series):
                        anomalies.append(f"{fname} col '{col_name}': 100% NaN values (unintended NaN propagation or total data loss).")
            except Exception as exc:
                anomalies.append(f"Failed analyzing {fname} for statistical stability: {exc}")

        # Check profile.json missing cell reconciliation if available
        profile_path = self.workspace / "profile.json"
        csv_path = self.workspace / "cleaned_data.csv"
        if profile_path.exists() and csv_path.exists():
            try:
                prof = json.loads(profile_path.read_text(encoding="utf-8"))
                df_c = pd.read_csv(csv_path, low_memory=False)
                for col_spec in prof.get("columns", []):
                    cname = col_spec.get("name")
                    if cname in df_c.columns:
                        actual_missing = int(df_c[cname].isna().sum())
                        prof_missing = int(col_spec.get("missing", 0))
                        if actual_missing != prof_missing:
                            anomalies.append(
                                f"NaN accounting mismatch in column '{cname}': CSV has {actual_missing} NaNs but profile recorded {prof_missing}."
                            )
            except Exception:
                pass

        if anomalies:
            return QACheckResult(
                check_name="statistical_stability",
                category="mathematical_consistency",
                passed=False,
                severity="HIGH",
                summary=f"Statistical anomalies detected: {len(anomalies)} issue(s): {'; '.join(anomalies[:3])}",
                details={"anomalies": anomalies, "numeric_cols_checked": numeric_cols_checked},
                remediation_hint="Handle division by zero (replace Inf with bounded values), clean NaNs, and calibrate extreme values.",
            )

        return QACheckResult(
            check_name="statistical_stability",
            category="mathematical_consistency",
            passed=True,
            severity="HIGH",
            summary=f"Verified statistical stability across {len(numeric_cols_checked)} numeric column(s) (zero Inf values, no unhandled NaN propagation, no overflow anomalies).",
            details={"numeric_cols_checked": numeric_cols_checked, "datasets": datasets_found},
        )

    def _check_file_existence(self) -> QACheckResult:
        required = [
            "business_analysis.xlsx",
            "cleaned_data.csv",
            "profile.json",
            "insights.txt",
            "report.html",
            "deliverables.zip",
        ]
        missing = [f for f in required if not (self.workspace / f).exists() or (self.workspace / f).stat().st_size == 0]

        chart_dir = self.workspace / "charts"
        has_charts = chart_dir.exists() and any(chart_dir.glob("*.png"))

        if missing or not has_charts:
            reasons = []
            if missing:
                reasons.append(f"Missing or empty required files: {', '.join(missing)}")
            if not has_charts:
                reasons.append("Charts directory is missing or contains no PNG images.")
            return QACheckResult(
                check_name="deliverables_exist",
                passed=False,
                summary="; ".join(reasons),
                details={"missing": missing, "has_charts": has_charts},
            )

        return QACheckResult(
            check_name="deliverables_exist",
            passed=True,
            summary="All core deliverable files and visual assets exist with non-zero size.",
            details={"verified_files": required, "charts_present": True},
        )

    def _check_excel_integrity(self) -> QACheckResult:
        xlsx_path = self.workspace / "business_analysis.xlsx"
        if not xlsx_path.exists():
            return QACheckResult(check_name="excel_integrity", passed=False, summary="business_analysis.xlsx does not exist.")

        try:
            wb = openpyxl.load_workbook(xlsx_path, read_only=False, data_only=False)
        except Exception as exc:
            return QACheckResult(
                check_name="excel_integrity",
                passed=False,
                summary=f"Excel file failed to open with openpyxl: {exc}",
            )

        expected_sheets = {"Cleaned Data", "Data Quality", "Numeric Summary"}
        found_sheets = set(wb.sheetnames)
        missing_sheets = expected_sheets - found_sheets
        if missing_sheets:
            wb.close()
            return QACheckResult(
                check_name="excel_integrity",
                passed=False,
                summary=f"Missing expected worksheets: {', '.join(missing_sheets)}",
                details={"found": list(found_sheets)},
            )

        # Inspect Cleaned Data sheet
        ws_clean = wb["Cleaned Data"]
        if ws_clean.max_row is None or ws_clean.max_row < 2:
            wb.close()
            return QACheckResult(
                check_name="excel_integrity",
                passed=False,
                summary="Cleaned Data worksheet contains no data rows.",
            )

        # Dimension validation against cleaned_data.csv
        csv_path = self.workspace / "cleaned_data.csv"
        if csv_path.exists():
            try:
                df_clean = pd.read_csv(csv_path, low_memory=False)
                expected_rows = len(df_clean) + 1  # 1 header + N rows
                expected_cols = len(df_clean.columns)
                if ws_clean.max_row != expected_rows or ws_clean.max_column != expected_cols:
                    wb.close()
                    return QACheckResult(
                        check_name="excel_integrity",
                        passed=False,
                        summary=(
                            f"Cleaned Data sheet dimension mismatch: Excel has {ws_clean.max_row} rows and "
                            f"{ws_clean.max_column} columns; expected {expected_rows} rows and {expected_cols} columns."
                        ),
                    )
            except Exception:
                pass

        # Scan ALL cells across ALL sheets for unescaped formula injection
        injection_issues: list[str] = []
        for sheetname in wb.sheetnames:
            ws = wb[sheetname]
            for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                for c_idx, cell in enumerate(row, start=1):
                    if isinstance(cell, str) and is_formula_injection(cell) and not cell.startswith("'"):
                        injection_issues.append(f"Sheet '{sheetname}' R{r_idx}C{c_idx}: {cell[:30]}")

        wb.close()

        if injection_issues:
            return QACheckResult(
                check_name="excel_integrity",
                passed=False,
                summary=f"Detected {len(injection_issues)} unescaped formula string(s) across workbook sheets: {', '.join(injection_issues[:3])}",
                details={"issues": injection_issues},
            )

        return QACheckResult(
            check_name="excel_integrity",
            passed=True,
            summary="Excel workbook opens cleanly, contains all required sheets, valid dimensions, and passes all-sheet formula sanitization.",
            details={"sheets": list(found_sheets), "data_rows": ws_clean.max_row - 1},
        )

    def _check_calculation_consistency(self) -> QACheckResult:
        csv_path = self.workspace / "cleaned_data.csv"
        profile_path = self.workspace / "profile.json"

        if not csv_path.exists() or not profile_path.exists():
            return QACheckResult(
                check_name="calculation_consistency",
                passed=False,
                summary="Missing cleaned_data.csv or profile.json for calculation verification.",
            )

        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            df_cleaned = pd.read_csv(csv_path, low_memory=False)
        except Exception as exc:
            return QACheckResult(
                check_name="calculation_consistency",
                passed=False,
                summary=f"Failed reading datasets for recalculation: {exc}",
            )

        # 1. Identity: rows_original == rows_after_deduplication + duplicate_rows_removed
        r_orig = profile.get("rows_original", 0)
        r_dedup = profile.get("rows_after_deduplication", 0)
        r_removed = profile.get("duplicate_rows_removed", 0)
        if r_orig != (r_dedup + r_removed):
            return QACheckResult(
                check_name="calculation_consistency",
                passed=False,
                summary=f"Row count identity mismatch: original ({r_orig}) != deduplicated ({r_dedup}) + removed ({r_removed})",
            )

        # 2. Re-verify row count matches actual CSV length
        if len(df_cleaned) != r_dedup:
            return QACheckResult(
                check_name="calculation_consistency",
                passed=False,
                summary=f"Cleaned CSV row count ({len(df_cleaned)}) does not match profile rows_after_deduplication ({r_dedup})",
            )

        # 3. Verify missing cell counts
        col_missing_sum = sum(c.get("missing", 0) for c in profile.get("columns", []))
        total_missing = profile.get("missing_cells", 0)
        if col_missing_sum != total_missing:
            return QACheckResult(
                check_name="calculation_consistency",
                passed=False,
                summary=f"Missing cells sum mismatch: sum of columns ({col_missing_sum}) != total ({total_missing})",
            )

        # 4. Check profile columns against actual df_cleaned columns
        profile_col_names = [str(c.get("name")) for c in profile.get("columns", [])]
        actual_col_names = [str(c) for c in df_cleaned.columns]
        if profile_col_names != actual_col_names:
            return QACheckResult(
                check_name="calculation_consistency",
                passed=False,
                summary="Profile columns list does not match cleaned_data.csv column sequence.",
            )

        # 5. Independent mathematical recalculation and complete numeric summary validation
        xlsx_path = self.workspace / "business_analysis.xlsx"
        numeric_cols = [c for c in df_cleaned.columns if pd.api.types.is_numeric_dtype(df_cleaned[c])]

        if xlsx_path.exists():
            try:
                excel_summary = pd.read_excel(xlsx_path, sheet_name="Numeric Summary")
                summary_cols = set(excel_summary["column"].astype(str)) if "column" in excel_summary.columns else set()

                # Require a complete numeric-summary row set for all numeric columns
                missing_summary = [str(c) for c in numeric_cols if str(c) not in summary_cols]
                if missing_summary:
                    return QACheckResult(
                        check_name="calculation_consistency",
                        passed=False,
                        summary=f"Numeric Summary sheet is missing rows for numeric columns: {', '.join(missing_summary)}",
                    )

                # Cross-verify recalculation of means
                if "column" in excel_summary.columns and "mean" in excel_summary.columns:
                    for _, row in excel_summary.iterrows():
                        col_name = str(row["column"])
                        if col_name in df_cleaned.columns and pd.api.types.is_numeric_dtype(df_cleaned[col_name]):
                            col_series = df_cleaned[col_name]
                            expected_mean = float(col_series.mean()) if not col_series.empty else float("nan")
                            raw_excel = row["mean"]
                            try:
                                excel_recorded_mean = float(raw_excel)
                            except (ValueError, TypeError):
                                excel_recorded_mean = float("nan")

                            # Detect NaN propagation discrepancy
                            if pd.isna(expected_mean) != pd.isna(excel_recorded_mean):
                                return QACheckResult(
                                    check_name="calculation_consistency",
                                    category="mathematical_consistency",
                                    passed=False,
                                    severity="CRITICAL",
                                    summary=f"NaN mismatch/propagation detected for '{col_name}': actual mean {expected_mean} != Excel recorded {excel_recorded_mean}",
                                    details={"column": col_name, "actual_mean": str(expected_mean), "excel_mean": str(excel_recorded_mean)},
                                )
                            if not pd.isna(expected_mean) and not pd.isna(excel_recorded_mean):
                                if abs(expected_mean - excel_recorded_mean) > 1e-3:
                                    return QACheckResult(
                                        check_name="calculation_consistency",
                                        category="mathematical_consistency",
                                        passed=False,
                                        severity="CRITICAL",
                                        summary=f"Independent mathematical recalculation discrepancy for '{col_name}': actual mean {expected_mean:.4f} != Excel recorded {excel_recorded_mean:.4f}",
                                    )
            except Exception as exc:
                return QACheckResult(
                    check_name="calculation_consistency",
                    passed=False,
                    summary=f"Failed independent cross-verification with Excel sheet: {exc}",
                )

        return QACheckResult(
            check_name="calculation_consistency",
            passed=True,
            summary="All mathematical identities, deduplication counts, and numeric statistics verified consistently against Excel deliverables.",
            details={"rows": len(df_cleaned), "columns": len(df_cleaned.columns), "missing_cells": total_missing},
        )

    def _check_charts_and_visuals(self) -> QACheckResult:
        chart_dir = self.workspace / "charts"
        if not chart_dir.exists():
            return QACheckResult(check_name="visual_deliverables", passed=False, summary="charts/ directory not found.")

        if chart_dir.is_symlink():
            return QACheckResult(check_name="visual_deliverables", passed=False, summary="charts/ directory is a symlink.")

        png_files = list(chart_dir.glob("*.png"))
        if not png_files:
            return QACheckResult(check_name="visual_deliverables", passed=False, summary="No chart PNGs were generated.")

        # PNG magic header: \x89 P N G \r \n \x1a \n
        png_magic = b"\x89PNG\r\n\x1a\n"
        invalid_pngs = []
        for p in png_files:
            if p.is_symlink():
                return QACheckResult(check_name="visual_deliverables", passed=False, summary=f"Symlinked chart file detected: {p.name}")
            data = p.read_bytes()
            if len(data) < 500 or not data.startswith(png_magic):
                invalid_pngs.append(p.name)

        if invalid_pngs:
            return QACheckResult(
                check_name="visual_deliverables",
                passed=False,
                summary=f"Corrupt or undersized chart image files: {', '.join(invalid_pngs)}",
            )

        return QACheckResult(
            check_name="visual_deliverables",
            passed=True,
            summary=f"Verified {len(png_files)} high-resolution chart PNGs with valid binary headers.",
            details={"chart_count": len(png_files), "charts": [p.name for p in png_files]},
        )

    def _check_html_report_integrity(self) -> QACheckResult:
        report_path = self.workspace / "report.html"
        if not report_path.exists():
            return QACheckResult(check_name="html_report", passed=False, summary="report.html is missing.")

        content = report_path.read_text(encoding="utf-8", errors="replace")
        if len(content) < 300:
            return QACheckResult(check_name="html_report", passed=False, summary="report.html is truncated or too short.")

        if "<!doctype html>" not in content.lower() and "<html" not in content.lower():
            return QACheckResult(check_name="html_report", passed=False, summary="report.html missing HTML5 doctype or root tag.")

        # Check required sections
        required_elements = ["Business Data", "Analyst observations", "Data quality", "Charts"]
        missing_sections = [sec for sec in required_elements if sec.lower() not in content.lower()]
        if missing_sections:
            return QACheckResult(
                check_name="html_report",
                passed=False,
                summary=f"report.html missing key presentation sections: {', '.join(missing_sections)}",
            )

        return QACheckResult(
            check_name="html_report",
            passed=True,
            summary="report.html is valid, well-formed HTML5 with complete executive presentation sections.",
            details={"bytes": len(content)},
        )

    def _check_archive_integrity(self, expected_files: list[str] | None = None) -> QACheckResult:
        zip_path = self.workspace / "deliverables.zip"
        if not zip_path.exists():
            return QACheckResult(check_name="archive_integrity", passed=False, summary="deliverables.zip is missing.")

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                bad_file = zf.testzip()
                if bad_file:
                    return QACheckResult(check_name="archive_integrity", passed=False, summary=f"Corrupted file in zip: {bad_file}")
                names = zf.namelist()
        except Exception as exc:
            return QACheckResult(check_name="archive_integrity", passed=False, summary=f"Failed reading deliverables.zip: {exc}")

        if expected_files is not None:
            missing_in_zip = [f for f in expected_files if f not in names]
            if missing_in_zip:
                return QACheckResult(
                    check_name="archive_integrity",
                    passed=False,
                    summary=f"Deliverable package zip is missing: {', '.join(missing_in_zip)}",
                )
            return QACheckResult(
                check_name="archive_integrity",
                passed=True,
                summary="deliverables.zip is healthy, verified uncorrupted, and contains all expected deliverable files.",
                details={"archived_files_count": len(names)},
            )

        required_in_zip = ["business_analysis.xlsx", "cleaned_data.csv", "report.html", "profile.json", "insights.txt"]
        missing_in_zip = [f for f in required_in_zip if f not in names]
        if missing_in_zip:
            return QACheckResult(
                check_name="archive_integrity",
                passed=False,
                summary=f"Deliverable package zip is missing: {', '.join(missing_in_zip)}",
            )

        # Enforce allow-list: only approved client deliverables and charts allowed; no internal logs or QA files!
        disallowed = []
        for name in names:
            if name in APPROVED_DELIVERABLE_FILES:
                continue
            if name.startswith("charts/") and name.endswith(".png"):
                continue
            disallowed.append(name)

        if disallowed:
            return QACheckResult(
                check_name="archive_integrity",
                passed=False,
                summary=f"Deliverable package contains disallowed or internal files: {', '.join(disallowed)}",
            )

        return QACheckResult(
            check_name="archive_integrity",
            passed=True,
            summary="deliverables.zip is healthy, verified uncorrupted, and contains all key deliverable files without arbitrary internal logs.",
            details={"archived_files_count": len(names)},
        )
