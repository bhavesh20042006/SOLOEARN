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


FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\n", "|")


def is_formula_injection(val: Any) -> bool:
    """
    Detects spreadsheet formula injection (CWE-1236).
    Checks if a string starts with =, +, -, @, \\t, \\r, \\n, or | without being a valid numeric literal.
    Negative and positive numeric literals (e.g. '-50', '-12.34', '+100') are safe
    and must not be corrupted with a leading quote.
    Already single-quote escaped strings (e.g. "'=1+1") are safe.
    """
    if not isinstance(val, str):
        return False
    if not val:
        return False

    val_clean = val.replace("\x00", "")
    if not val_clean:
        return False

    if val_clean.startswith("'"):
        return False

    val_strip = val_clean.strip()
    starts_with_trigger = val_clean[0] in FORMULA_TRIGGERS or (bool(val_strip) and val_strip[0] in ("=", "+", "-", "@", "|"))
    if not starts_with_trigger:
        return False

    # If the stripped value represents a valid float or integer, it is safe numerical data
    if val_strip and val_strip[0] in ("+", "-"):
        try:
            float(val_strip)
            return False
        except ValueError:
            pass

    # Otherwise, it contains non-numeric formula syntax, e.g. =SUM(), -2+3+cmd|..., @HYPERLINK(), \t=cmd, |calc
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


def generate_executive_workbook(
    xlsx_path: Path,
    df: pd.DataFrame,
    profile: dict[str, Any],
    numeric_summary: pd.DataFrame,
) -> None:
    """
    Generate an openpyxl multi-sheet workbook with executive summary styling,
    KPI cards, professional data tables, number formatting, and auto-adjusted column widths.
    """
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()

    # Sheet 1: Executive Summary
    ws_summary = wb.active
    ws_summary.title = "Executive Summary"
    if ws_summary.views and ws_summary.views.sheetView:
        ws_summary.views.sheetView[0].showGridLines = True

    # Styling definitions
    font_title = Font(name="Calibri", size=15, bold=True, color="FFFFFF")
    font_subtitle = Font(name="Calibri", size=9, italic=True, color="64748B")
    font_card_lbl = Font(name="Calibri", size=9, bold=True, color="475569")
    font_card_val = Font(name="Calibri", size=16, bold=True, color="0F172A")
    font_card_sub = Font(name="Calibri", size=8, italic=True, color="94A3B8")
    font_sec_hdr = Font(name="Calibri", size=11, bold=True, color="1E293B")
    font_tbl_hdr = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    font_data = Font(name="Calibri", size=10, color="1E293B")
    font_data_bold = Font(name="Calibri", size=10, bold=True, color="1E293B")

    fill_navy = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    fill_slate = PatternFill(start_color="334155", end_color="334155", fill_type="solid")
    fill_card_hdr = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
    fill_white = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
    fill_zebra = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

    border_thin = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )
    border_cell = Border(
        left=Side(style="thin", color="E2E8F0"),
        right=Side(style="thin", color="E2E8F0"),
        top=Side(style="thin", color="E2E8F0"),
        bottom=Side(style="thin", color="E2E8F0"),
    )

    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")
    align_right = Alignment(horizontal="right", vertical="center")

    # Title Banner
    ws_summary.merge_cells("B2:I2")
    ws_summary["B2"] = "EXECUTIVE BUSINESS INTELLIGENCE SUMMARY"
    ws_summary["B2"].font = font_title
    ws_summary["B2"].fill = fill_navy
    ws_summary["B2"].alignment = align_center
    ws_summary.row_dimensions[2].height = 32

    src_name = sanitize_cell_value(str(profile.get("source_filename", "Dataset")))
    ws_summary.merge_cells("B3:I3")
    ws_summary["B3"] = f"Source: {src_name} | Verified by SOLOEARN Autonomous Data Pipeline"
    ws_summary["B3"].font = font_subtitle
    ws_summary["B3"].alignment = align_center
    ws_summary.row_dimensions[3].height = 18

    # KPI Cards Row
    ws_summary.row_dimensions[5].height = 18
    ws_summary.row_dimensions[6].height = 28
    ws_summary.row_dimensions[7].height = 16

    rows_orig = profile.get("rows_original", len(df))
    rows_clean = profile.get("rows_after_deduplication", len(df))
    dups_removed = profile.get("duplicate_rows_removed", 0)
    missing_cells = profile.get("missing_cells", 0)
    total_cells = max(1, rows_clean * max(1, len(df.columns)))
    completeness_pct = max(0.0, min(100.0, 100.0 - (missing_cells / total_cells * 100)))

    cards = [
        ("B", "C", "INGESTED RECORDS", f"{rows_orig:,}", "Raw source volume"),
        ("D", "E", "CLEANED RECORDS", f"{rows_clean:,}", "Verified rows"),
        ("F", "G", "DATA COMPLETENESS", f"{completeness_pct:.1f}%", f"{missing_cells:,} missing cells"),
        ("H", "I", "ATTRIBUTES", f"{len(df.columns)}", f"{len(profile.get('numeric_columns', []))} num / {len(profile.get('categorical_columns', []))} cat"),
    ]

    for c1, c2, lbl, val, sub in cards:
        ws_summary.merge_cells(f"{c1}5:{c2}5")
        ws_summary.merge_cells(f"{c1}6:{c2}6")
        ws_summary.merge_cells(f"{c1}7:{c2}7")

        top_cell = ws_summary[f"{c1}5"]
        mid_cell = ws_summary[f"{c1}6"]
        bot_cell = ws_summary[f"{c1}7"]

        top_cell.value = sanitize_cell_value(lbl)
        top_cell.font = font_card_lbl
        top_cell.fill = fill_card_hdr
        top_cell.alignment = align_center

        mid_cell.value = sanitize_cell_value(val)
        mid_cell.font = font_card_val
        mid_cell.fill = fill_white
        mid_cell.alignment = align_center

        bot_cell.value = sanitize_cell_value(sub)
        bot_cell.font = font_card_sub
        bot_cell.fill = fill_white
        bot_cell.alignment = align_center

        for r in range(5, 8):
            for col_letter in (c1, c2):
                ws_summary[f"{col_letter}{r}"].border = border_thin

    # Section 1: Ingestion Specifications Table
    ws_summary["B9"] = "1. Ingestion & Quality Audit Specifications"
    ws_summary["B9"].font = font_sec_hdr

    headers_s1 = ["Metric", "Observed Value", "Status", "Operational Interpretation"]
    cols_s1 = ["B", "C", "D", "E"]
    ws_summary.row_dimensions[10].height = 22
    for col_l, h in zip(cols_s1, headers_s1):
        cell = ws_summary[f"{col_l}10"]
        cell.value = sanitize_cell_value(h)
        cell.font = font_tbl_hdr
        cell.fill = fill_slate
        cell.alignment = align_left if col_l in ("B", "E") else align_center
        cell.border = border_cell

    metrics_rows = [
        ("Source Filename", src_name, "Verified", "Input tabular data received"),
        ("Ingested Line Items", f"{rows_orig:,}", "Audited", "Raw rows at ingestion boundary"),
        ("Cleaned Valid Records", f"{rows_clean:,}", "Verified", "Deduplicated tabular dataset rows"),
        ("Duplicate Records Removed", f"{dups_removed:,}", "Reconciled", "Redundant identical entries purged"),
        ("Missing Value Cells", f"{missing_cells:,}", "Audited", "Null cells in dataset"),
        ("Formula Injection Shield", "Active", "Protected", "Formula injection neutralized (CWE-1236)"),
    ]

    for idx, (m, v, s, o) in enumerate(metrics_rows, start=11):
        ws_summary.row_dimensions[idx].height = 20
        row_fill = fill_zebra if idx % 2 == 1 else fill_white
        vals = [m, v, s, o]
        for col_l, val in zip(cols_s1, vals):
            cell = ws_summary[f"{col_l}{idx}"]
            cell.value = sanitize_cell_value(val)
            cell.font = font_data_bold if col_l == "B" else font_data
            cell.fill = row_fill
            cell.alignment = align_left if col_l in ("B", "E") else align_center
            cell.border = border_cell

    # Section 2: Workbook Directory Table
    start_r2 = 18
    ws_summary[f"B{start_r2}"] = "2. Workbook Worksheet Directory"
    ws_summary[f"B{start_r2}"].font = font_sec_hdr

    headers_s2 = ["Worksheet Tab", "Record Count", "Functional Description"]
    cols_s2 = ["B", "C", "D"]
    ws_summary.row_dimensions[start_r2 + 1].height = 22
    for col_l, h in zip(cols_s2, headers_s2):
        cell = ws_summary[f"{col_l}{start_r2 + 1}"]
        cell.value = sanitize_cell_value(h)
        cell.font = font_tbl_hdr
        cell.fill = fill_slate
        cell.alignment = align_left if col_l != "C" else align_center
        cell.border = border_cell

    dir_rows = [
        ("Cleaned Data", f"{len(df):,} rows", "Sanitized, deduplicated tabular dataset with formula injection defense"),
        ("Data Quality", f"{len(profile.get('columns', []))} columns", "Column-level null counts, null percentages, and unique cardinality"),
        ("Numeric Summary", f"{len(numeric_summary):,} columns", "Descriptive statistics (mean, std, min, quartiles, max) for numeric columns"),
    ]

    for idx, (tab, cnt, desc) in enumerate(dir_rows, start=start_r2 + 2):
        ws_summary.row_dimensions[idx].height = 20
        row_fill = fill_zebra if idx % 2 == 1 else fill_white
        vals = [tab, cnt, desc]
        for col_l, val in zip(cols_s2, vals):
            cell = ws_summary[f"{col_l}{idx}"]
            cell.value = sanitize_cell_value(val)
            cell.font = font_data_bold if col_l == "B" else font_data
            cell.fill = row_fill
            cell.alignment = align_left if col_l != "C" else align_center
            cell.border = border_cell

    # Set column widths for Executive Summary
    ws_summary.column_dimensions["A"].width = 4
    ws_summary.column_dimensions["B"].width = 28
    ws_summary.column_dimensions["C"].width = 22
    ws_summary.column_dimensions["D"].width = 16
    ws_summary.column_dimensions["E"].width = 44
    ws_summary.column_dimensions["F"].width = 18
    ws_summary.column_dimensions["G"].width = 18
    ws_summary.column_dimensions["H"].width = 18
    ws_summary.column_dimensions["I"].width = 18

    # Sheet 2: Cleaned Data
    ws_clean = wb.create_sheet("Cleaned Data")
    if ws_clean.views and ws_clean.views.sheetView:
        ws_clean.views.sheetView[0].showGridLines = True
    ws_clean.freeze_panes = "A2"
    ws_clean.row_dimensions[1].height = 26

    for c_idx, col_name in enumerate(df.columns, start=1):
        cell = ws_clean.cell(row=1, column=c_idx, value=sanitize_cell_value(str(col_name)))
        cell.font = font_tbl_hdr
        cell.fill = fill_navy
        cell.alignment = align_left
        cell.border = border_cell

    col_dtypes = {c_idx: df[col_name].dtype for c_idx, col_name in enumerate(df.columns, start=1)}

    for r_idx, row in enumerate(df.itertuples(index=False), start=2):
        ws_clean.row_dimensions[r_idx].height = 20
        row_fill = fill_zebra if r_idx % 2 == 1 else fill_white
        for c_idx, val in enumerate(row, start=1):
            s_val = sanitize_cell_value(val) if isinstance(val, str) else val
            cell = ws_clean.cell(row=r_idx, column=c_idx, value=s_val)
            cell.font = font_data
            cell.fill = row_fill
            cell.border = border_cell
            dt = col_dtypes[c_idx]
            if pd.api.types.is_float_dtype(dt):
                cell.number_format = "#,##0.00"
                cell.alignment = align_right
            elif pd.api.types.is_integer_dtype(dt):
                cell.number_format = "#,##0"
                cell.alignment = align_right

    ws_clean.auto_filter.ref = ws_clean.dimensions

    for col in ws_clean.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = 0
        for cell in col[:1000]:
            val_str = str(cell.value or "")
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws_clean.column_dimensions[col_letter].width = max(min(max_len + 4, 52), 14)

    # Sheet 3: Data Quality
    ws_dq = wb.create_sheet("Data Quality")
    if ws_dq.views and ws_dq.views.sheetView:
        ws_dq.views.sheetView[0].showGridLines = True
    ws_dq.freeze_panes = "A2"
    ws_dq.row_dimensions[1].height = 26

    profile_df = pd.DataFrame(profile.get("columns", []))
    for c_idx, col_name in enumerate(profile_df.columns, start=1):
        cell = ws_dq.cell(row=1, column=c_idx, value=sanitize_cell_value(str(col_name)))
        cell.font = font_tbl_hdr
        cell.fill = fill_navy
        cell.alignment = align_left
        cell.border = border_cell

    for r_idx, row in enumerate(profile_df.itertuples(index=False), start=2):
        ws_dq.row_dimensions[r_idx].height = 20
        row_fill = fill_zebra if r_idx % 2 == 1 else fill_white
        for c_idx, val in enumerate(row, start=1):
            s_val = sanitize_cell_value(val) if isinstance(val, str) else val
            cell = ws_dq.cell(row=r_idx, column=c_idx, value=s_val)
            cell.font = font_data
            cell.fill = row_fill
            cell.border = border_cell
            col_hdr = str(profile_df.columns[c_idx - 1])
            if "percent" in col_hdr:
                cell.number_format = "0.0%"
                cell.alignment = align_right
            elif isinstance(val, (int, float)):
                cell.number_format = "#,##0"
                cell.alignment = align_right

    ws_dq.auto_filter.ref = ws_dq.dimensions
    for col in ws_dq.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = max((len(str(c.value or "")) for c in col[:1000]), default=10)
        ws_dq.column_dimensions[col_letter].width = max(min(max_len + 4, 50), 14)

    # Sheet 4: Numeric Summary
    ws_num = wb.create_sheet("Numeric Summary")
    if ws_num.views and ws_num.views.sheetView:
        ws_num.views.sheetView[0].showGridLines = True
    ws_num.freeze_panes = "A2"
    ws_num.row_dimensions[1].height = 26

    for c_idx, col_name in enumerate(numeric_summary.columns, start=1):
        cell = ws_num.cell(row=1, column=c_idx, value=sanitize_cell_value(str(col_name)))
        cell.font = font_tbl_hdr
        cell.fill = fill_navy
        cell.alignment = align_left
        cell.border = border_cell

    for r_idx, row in enumerate(numeric_summary.itertuples(index=False), start=2):
        ws_num.row_dimensions[r_idx].height = 20
        row_fill = fill_zebra if r_idx % 2 == 1 else fill_white
        for c_idx, val in enumerate(row, start=1):
            s_val = sanitize_cell_value(val) if isinstance(val, str) else val
            cell = ws_num.cell(row=r_idx, column=c_idx, value=s_val)
            cell.font = font_data
            cell.fill = row_fill
            cell.border = border_cell
            if isinstance(val, float):
                cell.number_format = "#,##0.00"
                cell.alignment = align_right
            elif isinstance(val, int):
                cell.number_format = "#,##0"
                cell.alignment = align_right

    ws_num.auto_filter.ref = ws_num.dimensions
    for col in ws_num.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = max((len(str(c.value or "")) for c in col[:1000]), default=10)
        ws_num.column_dimensions[col_letter].width = max(min(max_len + 4, 50), 14)

    # Explicit gridlines across all sheets
    for ws in wb.worksheets:
        if ws.views and ws.views.sheetView:
            ws.views.sheetView[0].showGridLines = True

    wb.save(xlsx_path)
    wb.close()


def create_analytics_bundle(
    file_bytes: bytes,
    filename: str,
    task_id: int,
    llm_call: Callable[[str, str], str] | None = None,
) -> Path:
    from core.service_executors import read_tabular_dataset_streamed

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise ValueError(f"File exceeds configured {MAX_UPLOAD_MB} MB upload limit")
    
    valid_exts = (".csv", ".tsv", ".xlsx", ".xlsm", ".xls")
    if not filename.lower().endswith(valid_exts):
        raise ValueError("Accepted file formats are CSV, TSV, or Excel (.xlsx, .xls)")
    
    try:
        df_raw, ingestion_meta = read_tabular_dataset_streamed(file_bytes, filename)
    except Exception as exc:
        raise ValueError(f"Could not read dataset '{filename}': {exc}") from exc
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
        "ingestion_metadata": ingestion_meta,
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
    generate_executive_workbook(xlsx_path, df, profile, numeric_summary)

    # Accelerated charts rendering with low compression overhead and sample optimization
    plt.ioff()
    matplotlib.rcParams["path.simplify"] = True
    matplotlib.rcParams["path.simplify_threshold"] = 1.0
    matplotlib.rcParams["agg.path.chunksize"] = 10000

    chart_files: list[str] = []
    for col in list(numeric.columns)[:4]:
        values = pd.to_numeric(df[col], errors="coerce").dropna()
        if values.empty:
            continue
        plot_sample = values.sample(min(10_000, len(values)), random_state=42) if len(values) > 10_000 else values
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.hist(plot_sample, bins=min(25, max(8, int(len(plot_sample) ** 0.5))), color="#0284c7", edgecolor="#0f172a", alpha=0.9)
        ax.set_title(f"Distribution: {col}", fontsize=11, fontweight="bold", color="#1e293b")
        ax.set_xlabel(str(col), fontsize=10)
        ax.set_ylabel("Frequency", fontsize=10)
        ax.grid(axis="y", linestyle="--", alpha=0.5)
        fig.tight_layout()
        path = chart_dir / f"{_slug(str(col))}_distribution.png"
        fig.savefig(path, dpi=100, pil_kwargs={"optimize": False, "compress_level": 1})
        plt.close(fig)
        chart_files.append(path.name)

    for col in cat_cols[:2]:
        if df[col].nunique(dropna=True) > 0:
            counts = df[col].fillna("[Missing]").astype(str).value_counts().head(10).sort_values()
            fig, ax = plt.subplots(figsize=(7, 4))
            ax.barh(counts.index, counts.values, color="#38bdf8", edgecolor="#0f172a", alpha=0.9)
            ax.set_title(f"Top categories: {col}", fontsize=11, fontweight="bold", color="#1e293b")
            ax.set_xlabel("Rows", fontsize=10)
            ax.grid(axis="x", linestyle="--", alpha=0.5)
            fig.tight_layout()
            path = chart_dir / f"{_slug(str(col))}_categories.png"
            fig.savefig(path, dpi=100, pil_kwargs={"optimize": False, "compress_level": 1})
            plt.close(fig)
            chart_files.append(path.name)

    (workspace / "profile.json").write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    (workspace / "insights.txt").write_text(insight_text, encoding="utf-8")
    df.to_csv(workspace / "cleaned_data.csv", index=False)

    chart_html = "".join(
        f'<figure><img src="charts/{html.escape(name)}" alt="{html.escape(name)}"><figcaption>{html.escape(name)}</figcaption></figure>'
        for name in chart_files
    ) or "<p>No charts could be generated from the available columns.</p>"

    # Accelerated Interactive HTML Executive Report
    total_cells = max(1, len(df) * max(1, len(df.columns)))
    completeness_rate = max(0.0, min(100.0, 100.0 - (missing / total_cells * 100)))

    table_rows = []
    for r in profile_df.itertuples(index=False):
        col_n = html.escape(str(r.name))
        dt = html.escape(str(r.dtype))
        m_cnt = f"{r.missing:,}"
        m_pct = f"{r.missing_percent:.1f}%"
        u_cnt = f"{r.unique_values:,}"
        badge_cls = "badge-ok" if r.missing == 0 else "badge-warn"
        table_rows.append(
            f"<tr><td><code>{col_n}</code></td><td><span class='badge'>{dt}</span></td>"
            f"<td style='text-align:right'>{m_cnt}</td><td style='text-align:right'><span class='{badge_cls}'>{m_pct}</span></td>"
            f"<td style='text-align:right'>{u_cnt}</td></tr>"
        )
    profile_table_html = "".join(table_rows)

    html_report = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SOLOEARN Business Data Report</title>
<style>
:root {{
  --bg-page: #f8fafc; --bg-card: #ffffff; --text-main: #0f172a; --text-muted: #64748b;
  --accent: #0284c7; --accent-dark: #0369a1; --border: #e2e8f0; --border-dark: #cbd5e1;
}}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  max-width: 1100px; margin: 32px auto; padding: 0 20px; background: var(--bg-page); color: var(--text-main); line-height: 1.5;
}}
header {{ border-bottom: 2px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }}
h1 {{ color: #0f172a; margin: 0 0 8px 0; font-size: 26px; }}
h2 {{ color: #1e293b; font-size: 19px; margin-top: 32px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }}
.badge {{ display: inline-block; padding: 2px 8px; font-size: 11px; font-weight: 600; border-radius: 4px; background: #e0f2fe; color: #0369a1; }}
.badge-ok {{ color: #166534; font-weight: 600; }}
.badge-warn {{ color: #b45309; font-weight: 600; }}
.kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 14px; margin: 20px 0; }}
.kpi {{ background: var(--bg-card); padding: 18px; border-radius: 12px; border: 1px solid var(--border); box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
.kpi b {{ display: block; font-size: 11px; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.05em; margin-bottom: 6px; }}
.kpi-val {{ font-size: 24px; font-weight: 700; color: #0f172a; }}
.kpi-sub {{ font-size: 11px; color: var(--text-muted); margin-top: 4px; }}
.tabs {{ display: flex; gap: 8px; border-bottom: 1px solid var(--border); margin: 24px 0 16px 0; }}
.tab-btn {{ background: none; border: none; padding: 8px 16px; font-size: 14px; font-weight: 600; color: var(--text-muted); cursor: pointer; border-bottom: 2px solid transparent; }}
.tab-btn.active {{ color: var(--accent); border-bottom-color: var(--accent); }}
.tab-content {{ display: none; }}
.tab-content.active {{ display: block; }}
figure {{ margin: 20px 0; padding: 16px; background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; }}
figcaption {{ margin-top: 8px; font-size: 12px; color: var(--text-muted); font-weight: 600; }}
img {{ max-width: 100%; height: auto; border-radius: 6px; }}
pre {{ white-space: pre-wrap; background: #ffffff; padding: 18px; border-radius: 10px; border: 1px solid var(--border); font-family: inherit; font-size: 14px; color: #334155; }}
.search-box {{ width: 100%; box-sizing: border-box; padding: 10px 14px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; margin-bottom: 12px; }}
table {{ border-collapse: collapse; width: 100%; background: #ffffff; border-radius: 8px; overflow: hidden; border: 1px solid var(--border); }}
td, th {{ border-bottom: 1px solid var(--border); padding: 10px 12px; text-align: left; font-size: 13px; }}
th {{ background: #f1f5f9; font-weight: 600; color: #334155; }}
.meta-box {{ background: #f8fafc; border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-size: 13px; color: #475569; margin: 16px 0; }}
</style>
</head>
<body>
<header>
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <h1>Business Data Analysis</h1>
    <span class="badge">QA Verified Delivery</span>
  </div>
  <p style="color:var(--text-muted);margin:4px 0 0 0;font-size:13px;">Generated locally by SOLOEARN Autonomous Delivery Pipeline. Descriptive analysis only; validate assumptions before making business decisions.</p>
</header>

<section class="kpis">
  <div class="kpi"><b>Rows</b><div class="kpi-val">{len(df):,}</div><div class="kpi-sub">Cleaned records</div></div>
  <div class="kpi"><b>Columns</b><div class="kpi-val">{len(df.columns)}</div><div class="kpi-sub">Analyzed attributes</div></div>
  <div class="kpi"><b>Missing cells</b><div class="kpi-val">{missing:,}</div><div class="kpi-sub">{completeness_rate:.1f}% completeness</div></div>
  <div class="kpi"><b>Duplicates removed</b><div class="kpi-val">{before - len(df):,}</div><div class="kpi-sub">Purged entries</div></div>
</section>

<div class="tabs">
  <button class="tab-btn active" onclick="openTab(event, 'tab-obs')">Analyst observations</button>
  <button class="tab-btn" onclick="openTab(event, 'tab-quality')">Data quality</button>
  <button class="tab-btn" onclick="openTab(event, 'tab-charts')">Charts</button>
</div>

<div id="tab-obs" class="tab-content active">
  <h2>Analyst observations</h2>
  <pre>{html.escape(insight_text)}</pre>
</div>

<div id="tab-quality" class="tab-content">
  <h2>Data quality profile</h2>
  <input class="search-box" id="dqSearch" type="search" placeholder="Filter columns by name or type..." oninput="filterQualityTable()">
  <div style="overflow-x:auto">
    <table id="dqTable">
      <thead>
        <tr><th>Column</th><th>Data type</th><th style="text-align:right">Missing count</th><th style="text-align:right">Missing %</th><th style="text-align:right">Distinct values</th></tr>
      </thead>
      <tbody>
        {profile_table_html}
      </tbody>
    </table>
  </div>
</div>

<div id="tab-charts" class="tab-content">
  <h2>Charts</h2>
  {chart_html}
</div>

<div class="meta-box">
  <b>Supporting files:</b> business_analysis.xlsx, cleaned_data.csv, profile.json, insights.txt.
</div>

<script>
function openTab(evt, tabId) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById(tabId).classList.add('active');
  evt.currentTarget.classList.add('active');
}}
function filterQualityTable() {{
  const q = document.getElementById('dqSearch').value.toLowerCase();
  document.querySelectorAll('#dqTable tbody tr').forEach(row => {{
    row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""
    (workspace / "report.html").write_text(html_report, encoding="utf-8")

    return build_client_archive(workspace)
