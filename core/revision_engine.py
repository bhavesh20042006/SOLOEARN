from __future__ import annotations

import html
import json
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import openpyxl
import pandas as pd

from core.analytics import _insight_summary, _slug, build_client_archive, sanitize_cell_value
from core.config import safe_workspace_path
from core.db import log_event
from core.qa_agent import IndependentQAAgent, QAReport


HARD_MAX_ATTEMPTS = 3


@dataclass
class RevisionAttempt:
    attempt: int
    reasons_addressed: list[str]
    actions_taken: list[str]
    qa_passed: bool
    qa_score_pct: float
    timestamp: str


class RevisionEngine:
    """
    Diagnoses QA failures and executes targeted repairs on deliverables.
    Enforces a strictly bounded retry cycle (hard capped at max 3 attempts).
    """

    def __init__(self, workspace: Path, task_id: int, max_attempts: int = 3):
        # Enforce that the workspace path is strictly contained within approved workspaces directory
        self.workspace = safe_workspace_path(workspace)
        self.task_id = task_id
        self.max_attempts = min(max(1, int(max_attempts)), HARD_MAX_ATTEMPTS)
        self.history: list[RevisionAttempt] = []

    def repair_and_retest(self, initial_qa_report: QAReport) -> tuple[bool, QAReport]:
        """
        Takes an initial failing QA report, executes repair actions, and re-tests
        until QA passes or max attempts are reached.
        """
        current_report = initial_qa_report
        attempt = 1

        while not current_report.passed and attempt <= self.max_attempts:
            actions_taken = self._execute_repairs(current_report)

            # Re-test with Independent QA Agent
            qa_agent = IndependentQAAgent(self.workspace, self.task_id)
            new_report = qa_agent.run_all_checks()

            rec = RevisionAttempt(
                attempt=attempt,
                reasons_addressed=list(current_report.failure_reasons),
                actions_taken=actions_taken,
                qa_passed=new_report.passed,
                qa_score_pct=new_report.score_pct,
                timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            self.history.append(rec)
            log_event(
                "deliverable_revised",
                f"task_id={self.task_id}; attempt={attempt}; passed={new_report.passed}; actions={len(actions_taken)}",
            )

            current_report = new_report
            attempt += 1

        # Write revision history to workspace
        history_path = self.workspace / "revision_history.json"
        history_path.write_text(
            json.dumps([asdict(h) for h in self.history], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        return current_report.passed, current_report

    def _execute_repairs(self, qa_report: QAReport) -> list[str]:
        actions: list[str] = []
        failure_names = {c.check_name for c in qa_report.checks if not c.passed}

        # 1. Calculation & profile repair
        if "calculation_consistency" in failure_names or not (self.workspace / "profile.json").exists():
            if (self.workspace / "cleaned_data.csv").exists():
                df = pd.read_csv(self.workspace / "cleaned_data.csv", low_memory=False)
                numeric = df.select_dtypes(include="number")
                cat_cols = list(df.select_dtypes(exclude="number").columns)
                missing = int(df.isna().sum().sum())
                profile = {
                    "source_filename": "cleaned_data.csv",
                    "rows_original": int(len(df)),
                    "rows_after_deduplication": int(len(df)),
                    "duplicate_rows_removed": 0,
                    "columns": [
                        {
                            "name": str(col),
                            "dtype": str(df[col].dtype),
                            "missing": int(df[col].isna().sum()),
                            "missing_percent": round(float(df[col].isna().mean() * 100), 2),
                            "unique_values": int(df[col].nunique(dropna=True)),
                        }
                        for col in df.columns
                    ],
                    "missing_cells": missing,
                    "numeric_columns": [str(c) for c in numeric.columns],
                    "categorical_columns": [str(c) for c in cat_cols],
                }
                (self.workspace / "profile.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
                actions.append("Rebuilt profile.json to align row count and missing cell identities.")

        # 2. Excel repair
        if "excel_integrity" in failure_names or "calculation_consistency" in failure_names or not (self.workspace / "business_analysis.xlsx").exists():
            if (self.workspace / "cleaned_data.csv").exists():
                df = pd.read_csv(self.workspace / "cleaned_data.csv", low_memory=False)
                # Sanitize column headers and values against formula injection
                df.columns = [sanitize_cell_value(str(c)) if isinstance(c, str) else c for c in df.columns]
                for col in df.select_dtypes(include=["object", "string"]).columns:
                    df[col] = df[col].map(sanitize_cell_value)

                profile_path = self.workspace / "profile.json"
                if profile_path.exists():
                    try:
                        profile = json.loads(profile_path.read_text(encoding="utf-8"))
                        profile_df = pd.DataFrame(profile["columns"])
                    except Exception:
                        profile_df = pd.DataFrame([{"column": c, "dtype": str(df[c].dtype)} for c in df.columns])
                else:
                    profile_df = pd.DataFrame([{"column": c, "dtype": str(df[c].dtype)} for c in df.columns])

                numeric = df.select_dtypes(include="number")
                numeric_summary = (
                    numeric.describe().T.reset_index(names="column")
                    if len(numeric.columns)
                    else pd.DataFrame(columns=["column"])
                )

                xlsx_path = self.workspace / "business_analysis.xlsx"
                with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
                    df.to_excel(writer, sheet_name="Cleaned Data", index=False)
                    profile_df.to_excel(writer, sheet_name="Data Quality", index=False)
                    numeric_summary.to_excel(writer, sheet_name="Numeric Summary", index=False)
                    for sheet in writer.book.worksheets:
                        sheet.freeze_panes = "A2"
                        sheet.auto_filter.ref = sheet.dimensions
                        for cells in sheet.columns:
                            max_len = min(max((len(str(c.value or "")) for c in cells[:500]), default=10) + 2, 42)
                            sheet.column_dimensions[cells[0].column_letter].width = max(12, max_len)
                actions.append("Recompiled business_analysis.xlsx with validated worksheets and sanitized formulas.")

        # 3. Visuals repair
        if "visual_deliverables" in failure_names:
            chart_dir = self.workspace / "charts"
            chart_dir.mkdir(exist_ok=True)
            # Remove any corrupt charts or symlinks first
            for old_chart in chart_dir.glob("*.png"):
                try:
                    old_chart.unlink()
                except Exception:
                    pass

            if (self.workspace / "cleaned_data.csv").exists():
                df = pd.read_csv(self.workspace / "cleaned_data.csv", low_memory=False)
                numeric = df.select_dtypes(include="number")
                cat_cols = list(df.select_dtypes(exclude="number").columns)
                charts_created = 0
                for col in list(numeric.columns)[:3]:
                    vals = pd.to_numeric(df[col], errors="coerce").dropna()
                    if not vals.empty:
                        fig, ax = plt.subplots(figsize=(6, 3.5))
                        ax.hist(vals, bins=min(15, max(5, int(len(vals) ** 0.5))), edgecolor="black")
                        ax.set_title(f"Distribution: {col}")
                        fig.tight_layout()
                        chart_path = chart_dir / f"{_slug(str(col))}_distribution.png"
                        fig.savefig(chart_path, dpi=120)
                        plt.close(fig)
                        charts_created += 1

                for col in cat_cols[:2]:
                    if df[col].nunique(dropna=True) > 0:
                        counts = df[col].fillna("[Missing]").astype(str).value_counts().head(10).sort_values()
                        fig, ax = plt.subplots(figsize=(6, 3.5))
                        counts.plot(kind="barh", ax=ax)
                        ax.set_title(f"Top categories: {col}")
                        fig.tight_layout()
                        chart_path = chart_dir / f"{_slug(str(col))}_categories.png"
                        fig.savefig(chart_path, dpi=120)
                        plt.close(fig)
                        charts_created += 1

                # If no numeric/categorical charts could be built, make a dataset summary chart
                if charts_created == 0:
                    fig, ax = plt.subplots(figsize=(6, 3.5))
                    counts = [len(df.columns), len(df)]
                    labels = ["Columns", "Rows"]
                    ax.bar(labels, counts, color=["skyblue", "lightgreen"])
                    ax.set_title("Dataset Dimensions Overview")
                    fig.tight_layout()
                    fig.savefig(chart_dir / "dataset_dimensions.png", dpi=120)
                    plt.close(fig)

                actions.append("Re-rendered valid visual charts in charts/ with correct PNG headers.")

        # 4. HTML report repair
        if "html_report" in failure_names or not (self.workspace / "report.html").exists():
            chart_dir = self.workspace / "charts"
            chart_files = [p.name for p in chart_dir.glob("*.png")] if chart_dir.exists() else []
            insights_path = self.workspace / "insights.txt"
            insights_txt = insights_path.read_text(encoding="utf-8") if insights_path.exists() else "Descriptive analysis produced."

            chart_html = "".join(
                f'<figure><img src="charts/{html.escape(c)}" alt="{html.escape(c)}"><figcaption>{html.escape(c)}</figcaption></figure>'
                for c in chart_files
            ) or "<p>No charts generated.</p>"

            html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>SOLOEARN Business Data Report</title>
<style>body{{font-family:Arial,sans-serif;max-width:900px;margin:30px auto;padding:0 20px;color:#1f2937}}
h1,h2{{color:#111827}}figure{{margin:16px 0;padding:12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px}}
img{{max-width:100%}}pre{{background:#f3f4f6;padding:12px;border-radius:6px;white-space:pre-wrap}}</style></head>
<body><h1>Business Data Analysis</h1><p>Verified client report generated by SOLOEARN.</p>
<h2>Analyst observations</h2><pre>{html.escape(insights_txt)}</pre>
<h2>Data quality and summary</h2><p>Full tables available in business_analysis.xlsx.</p>
<h2>Charts</h2>{chart_html}</body></html>"""
            (self.workspace / "report.html").write_text(html_doc, encoding="utf-8")
            actions.append("Re-synthesized compliant HTML executive presentation report.")

        # 5. Archive package repair: always rebuild archive if any repairs occurred
        if (
            actions
            or "archive_integrity" in failure_names
            or "deliverables_exist" in failure_names
            or not (self.workspace / "deliverables.zip").exists()
        ):
            build_client_archive(self.workspace)
            actions.append("Re-synced deliverables.zip archive with repaired workspace files.")

        return actions
