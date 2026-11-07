from __future__ import annotations

import html
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Callable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from core.config import MAX_UPLOAD_MB, safe_workspace_path


APPROVED_DELIVERABLE_FILES = {
    "business_analysis.xlsx",
    "cleaned_data.csv",
    "profile.json",
    "insights.txt",
    "report.html",
}


def is_formula_injection(val: Any) -> bool:
    """
    Detects spreadsheet formula injection (CWE-1236).
    Checks if a string starts with =, +, -, or @ without being a valid numeric literal.
    Negative and positive numeric literals (e.g. '-50', '-12.34', '+100') are safe
    and must not be corrupted with a leading quote.
    """
    if not isinstance(val, str):
        return False
    val_strip = val.strip()
    if not val_strip or val_strip[0] not in ("=", "+", "-", "@"):
        return False
    # If the value represents a valid float or integer, it is safe numerical data
    try:
        float(val_strip)
        return False
    except ValueError:
        pass
    # Otherwise, it contains non-numeric formula syntax, e.g. =SUM(), -2+3+cmd|..., @HYPERLINK()
    return True


def sanitize_cell_value(val: Any) -> Any:
    """Escapes dangerous formula strings with a leading single quote while preserving real numbers."""
    if is_formula_injection(val):
        val_str = str(val)
        return val_str if val_str.startswith("'") else ("'" + val_str)
    return val


def build_client_archive(workspace: Path) -> Path:
    """
    Packages only approved deliverable files and chart PNGs into deliverables.zip,
    ignoring arbitrary files, internal logs, QA reports, and rejecting symlinks.
    """
    ws = safe_workspace_path(workspace)
    zip_path = ws / "deliverables.zip"
    if zip_path.is_symlink():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fname in sorted(APPROVED_DELIVERABLE_FILES):
            fpath = ws / fname
            if fpath.is_symlink():
                raise ValueError(f"Symlinked deliverable rejected: {fname}")
            if fpath.exists() and fpath.is_file():
                zf.write(fpath, fname)

        chart_dir = ws / "charts"
        if chart_dir.exists() and chart_dir.is_dir():
            if chart_dir.is_symlink():
                raise ValueError("Symlinked chart directory rejected")
            for cfile in sorted(chart_dir.glob("*.png")):
                if cfile.is_symlink():
                    raise ValueError(f"Symlinked chart rejected: {cfile.name}")
                if cfile.is_file():
                    zf.write(cfile, f"charts/{cfile.name}")

    return zip_path


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_").lower()
    return slug[:50] or "dataset"


def _insight_summary(df: pd.DataFrame, profile: dict) -> str:
    numeric = df.select_dtypes(include="number")
    lines = [f"The dataset has {len(df):,} rows and {len(df.columns)} columns after removing duplicate rows."]
    if profile["missing_cells"]:
        lines.append(f"There are {profile['missing_cells']:,} missing cells; review the data-quality sheet before using results.")
    else:
        lines.append("No missing cells were detected in the retained rows.")
    if len(numeric.columns):
        means = numeric.mean(numeric_only=True).sort_values(ascending=False).head(3)
        for col, val in means.items():
            lines.append(f"Highest numeric mean among columns: {col} ({val:,.2f}).")
    else:
        lines.append("No numeric columns were detected, so numeric trend analysis was not produced.")
    lines.append("These are descriptive observations, not causal conclusions or business forecasts.")
    return "\n".join(lines)


def _llm_insights(profile: dict, base_summary: str, llm_call: Callable[[str, str], str] | None) -> str:
    if not llm_call:
        return base_summary
    compact = json.dumps(profile, ensure_ascii=False)[:7000]
    try:
        result = llm_call(
            "You are a cautious data analyst. Only describe patterns supported by the supplied dataset profile. "
            "Never invent business context, causation, predictions, or column meanings. Mention limitations.",
            f"Write 3-5 concise business observations from this aggregate-only profile.\n{compact}\nBaseline observations:\n{base_summary}",
        )
        return result or base_summary
    except Exception:
        return base_summary


def create_analytics_bundle(
    file_bytes: bytes,
    filename: str,
    task_id: int,
    llm_call: Callable[[str, str], str] | None = None,
) -> Path:
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise ValueError(f"File exceeds configured {MAX_UPLOAD_MB} MB upload limit")
    if not filename.lower().endswith(".csv"):
        raise ValueError("Version 1 accepts CSV files only")
    try:
        df_raw = pd.read_csv(io.BytesIO(file_bytes), low_memory=False)
    except Exception as exc:
        raise ValueError(f"Could not read CSV: {exc}") from exc
    if df_raw.empty or len(df_raw.columns) == 0:
        raise ValueError("CSV contains no usable rows or columns")
    if len(df_raw) > 250_000:
        raise ValueError("For this laptop MVP, limit each CSV to 250,000 rows")

    df = df_raw.copy()

    # Sanitize CSV header column names against formula injection
    sanitized_cols = [sanitize_cell_value(str(col)) if isinstance(col, str) else col for col in df.columns]
    df.columns = sanitized_cols

    # Avoid spreadsheet formula injection without corrupting negative/positive numerical values.
    for col in df.select_dtypes(include=["object", "string"]).columns:
        df[col] = df[col].map(sanitize_cell_value)
    before = len(df)
    df = df.drop_duplicates().reset_index(drop=True)
    missing = int(df.isna().sum().sum())
    numeric = df.select_dtypes(include="number")
    cat_cols = list(df.select_dtypes(exclude="number").columns)

    profile = {
        "source_filename": Path(filename).name,
        "rows_original": int(before),
        "rows_after_deduplication": int(len(df)),
        "duplicate_rows_removed": int(before - len(df)),
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
    base_summary = _insight_summary(df, profile)
    insight_text = _llm_insights(profile, base_summary, llm_call)

    workspace = safe_workspace_path(f"task_{task_id:04d}")
    workspace.mkdir(parents=True, exist_ok=True)
    chart_dir = workspace / "charts"
    chart_dir.mkdir(exist_ok=True)

    # Profile and numeric summary.
    profile_df = pd.DataFrame(profile["columns"])
    numeric_summary = (
        numeric.describe().T.reset_index(names="column")
        if len(numeric.columns)
        else pd.DataFrame(columns=["column"])
    )

    xlsx_path = workspace / "business_analysis.xlsx"
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Cleaned Data", index=False)
        profile_df.to_excel(writer, sheet_name="Data Quality", index=False)
        numeric_summary.to_excel(writer, sheet_name="Numeric Summary", index=False)
        for sheet in writer.book.worksheets:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for cells in sheet.columns:
                max_len = min(max((len(str(c.value or "")) for c in cells[:1000]), default=10) + 2, 42)
                sheet.column_dimensions[cells[0].column_letter].width = max(12, max_len)

    # Charts: keep only small, interpretable charts for laptop-friendly runtime.
    chart_files: list[str] = []
    for col in list(numeric.columns)[:4]:
        values = pd.to_numeric(df[col], errors="coerce").dropna()
        if values.empty:
            continue
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.hist(values, bins=min(25, max(8, int(len(values) ** 0.5))), edgecolor="black")
        ax.set_title(f"Distribution: {col}")
        ax.set_xlabel(str(col))
        ax.set_ylabel("Frequency")
        fig.tight_layout()
        path = chart_dir / f"{_slug(str(col))}_distribution.png"
        fig.savefig(path, dpi=120)
        plt.close(fig)
        chart_files.append(path.name)

    for col in cat_cols[:2]:
        if df[col].nunique(dropna=True) > 0:
            counts = df[col].fillna("[Missing]").astype(str).value_counts().head(10).sort_values()
            fig, ax = plt.subplots(figsize=(7, 4))
            counts.plot(kind="barh", ax=ax)
            ax.set_title(f"Top categories: {col}")
            ax.set_xlabel("Rows")
            fig.tight_layout()
            path = chart_dir / f"{_slug(str(col))}_categories.png"
            fig.savefig(path, dpi=120)
            plt.close(fig)
            chart_files.append(path.name)

    (workspace / "profile.json").write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    (workspace / "insights.txt").write_text(insight_text, encoding="utf-8")
    df.to_csv(workspace / "cleaned_data.csv", index=False)

    chart_html = "".join(
        f'<figure><img src="charts/{html.escape(name)}" alt="{html.escape(name)}"><figcaption>{html.escape(name)}</figcaption></figure>'
        for name in chart_files
    ) or "<p>No charts could be generated from the available columns.</p>"

    html_report = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SOLOEARN Business Data Report</title><style>
body{{font-family:Arial,sans-serif;max-width:1000px;margin:36px auto;padding:0 18px;color:#1f2937;line-height:1.5}}
h1,h2{{color:#111827}}.kpis{{display:flex;gap:12px;flex-wrap:wrap}}.kpi{{background:#f3f4f6;padding:14px;border-radius:10px;min-width:150px}}
figure{{margin:18px 0;padding:12px;background:#fafafa;border:1px solid #e5e7eb;border-radius:10px}}
img{{max-width:100%;height:auto}}pre{{white-space:pre-wrap;background:#f9fafb;padding:14px;border-radius:8px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:8px;text-align:left}}
</style></head><body>
<h1>Business Data Analysis</h1><p>Generated locally by SOLOEARN. Descriptive analysis only; validate assumptions before making business decisions.</p>
<div class="kpis"><div class="kpi"><b>Rows</b><br>{len(df):,}</div><div class="kpi"><b>Columns</b><br>{len(df.columns)}</div><div class="kpi"><b>Missing cells</b><br>{missing:,}</div><div class="kpi"><b>Duplicates removed</b><br>{before-len(df):,}</div></div>
<h2>Analyst observations</h2><pre>{html.escape(insight_text)}</pre>
<h2>Data quality profile</h2>{profile_df.to_html(index=False, escape=True)}
<h2>Charts</h2>{chart_html}
<p>Supporting files: business_analysis.xlsx, cleaned_data.csv, profile.json, insights.txt.</p>
</body></html>"""
    (workspace / "report.html").write_text(html_report, encoding="utf-8")

    return build_client_archive(workspace)
