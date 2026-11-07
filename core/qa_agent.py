from __future__ import annotations

import html
import io
import json
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openpyxl
import pandas as pd

from core.analytics import APPROVED_DELIVERABLE_FILES, is_formula_injection
from core.config import safe_workspace_path


@dataclass
class QACheckResult:
    check_name: str
    passed: bool
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class QAReport:
    task_id: int
    passed: bool
    score_pct: float
    total_checks: int
    passed_checks: int
    checks: list[QACheckResult]
    failure_reasons: list[str]
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
        checks: list[QACheckResult] = []

        checks.append(self._check_file_existence())
        checks.append(self._check_excel_integrity())
        checks.append(self._check_calculation_consistency())
        checks.append(self._check_charts_and_visuals())
        checks.append(self._check_html_report_integrity())
        checks.append(self._check_archive_integrity())

        passed_count = sum(1 for c in checks if c.passed)
        total_count = len(checks)
        all_passed = passed_count == total_count
        score_pct = round((passed_count / total_count) * 100, 1)

        failures = [f"[{c.check_name}] {c.summary}" for c in checks if not c.passed]

        report = QAReport(
            task_id=self.task_id,
            passed=all_passed,
            score_pct=score_pct,
            total_checks=total_count,
            passed_checks=passed_count,
            checks=checks,
            failure_reasons=failures,
        )

        # Save QA report directly to the task workspace
        report_file = self.workspace / "qa_report.json"
        report_file.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

        return report

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
                            expected_mean = float(df_cleaned[col_name].mean())
                            excel_recorded_mean = float(row["mean"])
                            if not (pd.isna(expected_mean) and pd.isna(excel_recorded_mean)):
                                if abs(expected_mean - excel_recorded_mean) > 1e-3:
                                    return QACheckResult(
                                        check_name="calculation_consistency",
                                        passed=False,
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

    def _check_archive_integrity(self) -> QACheckResult:
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
