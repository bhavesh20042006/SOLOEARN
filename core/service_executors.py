from __future__ import annotations

import html
import io
import json
import math
import re
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Generator

import pandas as pd

from core.analytics import sanitize_cell_value
from core.config import MAX_UPLOAD_MB


def _safe_child_path(base_dir: Path, filename: str) -> Path:
    """Resolve a relative output path while rejecting traversal and symlink escapes."""
    base = Path(base_dir).resolve()
    raw = str(filename)
    if not raw or "\x00" in raw:
        raise ValueError("Output filename is empty or invalid")
    win_path = PureWindowsPath(raw)
    posix_path = PurePosixPath(raw)
    if Path(raw).is_absolute() or win_path.is_absolute() or posix_path.is_absolute() or win_path.drive or win_path.root or raw.startswith(("/", "\\")):
        raise ValueError("Absolute output paths are not permitted")

    normalised = raw.replace("\\", "/")
    parts = [part for part in normalised.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Path traversal blocked")

    candidate = base.joinpath(*parts)
    # Reject existing symlinks in every path component before and after resolution.
    current = base
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlink output path rejected")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError("Output path escapes its workspace") from exc
    return resolved


def safe_relative_write(base_dir: Path, filename: str, content: str | bytes) -> Path:
    base = Path(base_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    out_path = _safe_child_path(base, filename)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Re-check after creating parent directories to catch symlink races at this layer.
    out_path = _safe_child_path(base, filename)
    if isinstance(content, str):
        out_path.write_text(content, encoding="utf-8")
    else:
        out_path.write_bytes(content)
    return out_path


def _safe_archive_name(name: str) -> str:
    raw = str(name)
    win_path = PureWindowsPath(raw)
    posix_path = PurePosixPath(raw)
    if not raw or Path(raw).is_absolute() or win_path.is_absolute() or posix_path.is_absolute() or win_path.drive or win_path.root or raw.startswith(("/", "\\")):
        raise ValueError(f"Unsafe archive member name: {raw!r}")
    parts = [p for p in raw.replace("\\", "/").split("/") if p not in {"", "."}]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"Unsafe archive member name: {raw!r}")
    return "/".join(parts)


def package_deliverables_zip(workspace: Path, allowed_filenames: list[str]) -> Path:
    """Package only explicitly allowed, workspace-contained, non-symlink deliverables."""
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    zip_path = _safe_child_path(root, "deliverables.zip")
    temp_path = _safe_child_path(root, ".deliverables.tmp.zip")

    with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        seen: set[str] = set()
        for item in allowed_filenames:
            rel_name = _safe_archive_name(item)
            target = _safe_child_path(root, rel_name)
            if target == zip_path or target == temp_path:
                continue
            if target.is_symlink():
                raise ValueError(f"Symlinked deliverable rejected: {rel_name}")
            if target.is_file():
                resolved = target.resolve()
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise ValueError(f"Deliverable escapes workspace: {rel_name}") from exc
                if rel_name not in seen:
                    zf.write(resolved, arcname=rel_name)
                    seen.add(rel_name)
            elif target.is_dir():
                for subfile in sorted(target.rglob("*")):
                    if subfile.is_symlink():
                        raise ValueError(f"Symlinked deliverable rejected: {subfile}")
                    if not subfile.is_file():
                        continue
                    resolved = subfile.resolve()
                    try:
                        resolved.relative_to(root)
                    except ValueError as exc:
                        raise ValueError(f"Deliverable escapes workspace: {subfile}") from exc
                    arcname = resolved.relative_to(root).as_posix()
                    if arcname not in seen:
                        zf.write(resolved, arcname=arcname)
                        seen.add(arcname)
    temp_path.replace(zip_path)
    return zip_path


def detect_encoding(raw_bytes: bytes) -> str:
    """
    Robust multi-encoding detection supporting UTF-8 BOM, UTF-16, UTF-8, CP1252, Latin-1, and ISO-8859-1.
    """
    if not raw_bytes:
        return "utf-8"
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw_bytes.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if raw_bytes.startswith(b"\xfe\xff"):
        return "utf-16-be"
    sample = raw_bytes[:131072]
    try:
        sample.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        import charset_normalizer
        matches = charset_normalizer.from_bytes(sample)
        best = matches.best()
        if best and best.encoding:
            cand = best.encoding.lower()
            try:
                sample.decode(cand)
                return cand
            except (UnicodeDecodeError, LookupError):
                pass
    except Exception:
        pass
    for cand in ("utf-8-sig", "cp1252", "latin-1", "iso-8859-1"):
        try:
            sample.decode(cand)
            return cand
        except (UnicodeDecodeError, LookupError):
            continue
    return "latin-1"


def is_excel_file(filename: str, raw_bytes: bytes = b"") -> bool:
    """Determine whether the input file is an Excel workbook (.xlsx, .xlsm, .xls)."""
    name = str(filename).lower()
    if name.endswith((".xlsx", ".xlsm", ".xltx", ".xltm", ".xls")):
        return True
    if len(raw_bytes) >= 8:
        if raw_bytes.startswith(b"PK\x03\x04"):
            return True
        if raw_bytes.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return True
    return False


def stream_excel_chunks(
    file_bytes: bytes,
    chunksize: int = 10_000,
    max_rows: int = 250_000,
) -> Generator[pd.DataFrame, None, None]:
    """
    Stream Excel rows in memory-efficient chunks using openpyxl read_only iterator.
    Does not build full XML DOM in memory.
    """
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    try:
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        header = None
        for row in rows_iter:
            if row and any(cell is not None and str(cell).strip() != "" for cell in row):
                header = [
                    str(c).strip() if c is not None and str(c).strip() != "" else f"col_{idx}"
                    for idx, c in enumerate(row, 1)
                ]
                break
        if not header:
            return
        
        chunk: list[list[Any]] = []
        total_rows = 0
        for row in rows_iter:
            if not row or not any(cell is not None and str(cell).strip() != "" for cell in row):
                continue
            chunk.append(list(row[:len(header)]))
            total_rows += 1
            if len(chunk) >= chunksize:
                yield pd.DataFrame(chunk, columns=header)
                chunk = []
            if total_rows >= max_rows:
                break
        if chunk:
            yield pd.DataFrame(chunk, columns=header)
    finally:
        wb.close()


def stream_csv_chunks(
    file_bytes: bytes,
    source_name: str,
    chunksize: int = 10_000,
    max_rows: int = 250_000,
    encoding: str | None = None,
) -> Generator[pd.DataFrame, None, None]:
    """
    Stream CSV rows in chunks with multi-encoding fallback and graceful skipping of corrupt rows.
    """
    clean_bytes = file_bytes.replace(b"\x00", b"")
    enc = encoding or detect_encoding(clean_bytes)
    
    try:
        reader = pd.read_csv(
            io.BytesIO(clean_bytes),
            chunksize=chunksize,
            encoding=enc,
            on_bad_lines="skip",
            low_memory=True,
        )
        total_rows = 0
        for chunk in reader:
            total_rows += len(chunk)
            yield chunk
            if total_rows >= max_rows:
                break
    except Exception:
        # Fallback to python engine with on_bad_lines='skip'
        reader = pd.read_csv(
            io.BytesIO(clean_bytes),
            chunksize=chunksize,
            encoding=enc,
            engine="python",
            on_bad_lines="skip",
        )
        total_rows = 0
        for chunk in reader:
            total_rows += len(chunk)
            yield chunk
            if total_rows >= max_rows:
                break


def read_tabular_dataset_streamed(
    file_bytes: bytes,
    source_name: str,
    max_rows: int = 250_000,
    chunksize: int = 10_000,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Read tabular data (CSV or Excel) memory-efficiently in chunks,
    with multi-encoding detection, corrupt row skipping, and numeric downcasting.
    """
    if not isinstance(file_bytes, (bytes, bytearray)) or not file_bytes:
        raise ValueError("A non-empty CSV file is required; no sample data will be substituted.")
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise ValueError(f"Input file exceeds the configured {MAX_UPLOAD_MB} MB upload limit")
    
    is_excel = is_excel_file(source_name, file_bytes)
    chunks: list[pd.DataFrame] = []
    enc = "binary" if is_excel else detect_encoding(file_bytes)
    
    try:
        if is_excel:
            chunk_gen = stream_excel_chunks(file_bytes, chunksize=chunksize, max_rows=max_rows)
        else:
            chunk_gen = stream_csv_chunks(file_bytes, source_name, chunksize=chunksize, max_rows=max_rows, encoding=enc)
        
        for chunk in chunk_gen:
            if not chunk.empty:
                for col in chunk.select_dtypes(include=["float64"]).columns:
                    chunk[col] = pd.to_numeric(chunk[col], downcast="float")
                for col in chunk.select_dtypes(include=["int64"]).columns:
                    chunk[col] = pd.to_numeric(chunk[col], downcast="integer")
                chunks.append(chunk)
    except Exception as exc:
        raise ValueError(f"Could not parse input CSV '{Path(source_name).name}': {exc}") from exc

    if not chunks:
        raise ValueError("The supplied CSV contains no data rows or columns.")
    
    df = pd.concat(chunks, ignore_index=True)
    if df.empty or len(df.columns) == 0:
        raise ValueError("The supplied CSV contains no data rows or columns.")
    if len(df) > max_rows:
        raise ValueError(f"For this laptop MVP, limit each CSV to {max_rows:,} rows")
    
    mem_bytes = int(df.memory_usage(deep=True).sum())
    mem_mb = round(mem_bytes / (1024 * 1024), 4)
    meta = {
        "format": "excel" if is_excel else "csv",
        "encoding": enc,
        "rows": len(df),
        "columns": len(df.columns),
        "memory_mb": mem_mb,
        "memory_bytes": mem_bytes,
    }
    return df, meta


def _read_csv_strict(file_bytes: bytes, source_name: str) -> pd.DataFrame:
    df, _ = read_tabular_dataset_streamed(file_bytes, source_name)
    return df


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    clean_cols: list[str] = []
    for idx, col in enumerate(df.columns, start=1):
        clean = re.sub(r"[^a-z0-9_]+", "_", str(col).strip().lower()).strip("_")
        clean_cols.append(clean or f"column_{idx}")
    if len(set(clean_cols)) != len(clean_cols):
        raise ValueError("Column normalization produced duplicate names; rename ambiguous source columns and retry.")
    df = df.copy()
    df.columns = clean_cols
    return df


def _sanitize_export_frame(df: pd.DataFrame) -> pd.DataFrame:
    safe = df.copy()
    for col in safe.columns:
        if safe[col].dtype == "object" or pd.api.types.is_string_dtype(safe[col].dtype):
            safe[col] = safe[col].map(sanitize_cell_value)
    return safe


def execute_data_engineering_service(workspace: Path, file_bytes: bytes, filename: str) -> list[str]:
    """Build an ETL starter pack from the supplied CSV. Invalid input is rejected, never fabricated."""
    df = _normalise_columns(_read_csv_strict(file_bytes, filename))
    input_rows = int(len(df))
    duplicate_rows = int(df.duplicated().sum())
    df = df.drop_duplicates().copy()
    num_cols = df.select_dtypes(include=["number"]).columns.tolist()
    for col in num_cols:
        mean_val = df[col].mean()
        if pd.notna(mean_val) and math.isfinite(float(mean_val)):
            df[col] = df[col].fillna(mean_val)

    safe_df = _sanitize_export_frame(df)
    safe_relative_write(workspace, "cleaned_dataset.csv", safe_df.to_csv(index=False))

    schema = {
        "dataset_name": Path(filename.replace("\\", "/")).name,
        "input_row_count": input_rows,
        "row_count": int(len(df)),
        "duplicate_rows_removed": duplicate_rows,
        "column_count": int(len(df.columns)),
        "columns": [
            {
                "name": col,
                "dtype": str(df[col].dtype),
                "null_count": int(df[col].isnull().sum()),
                "distinct_count": int(df[col].nunique(dropna=True)),
            }
            for col in df.columns
        ],
    }
    safe_relative_write(workspace, "schema_manifest.json", json.dumps(schema, indent=2, allow_nan=False))

    default_input = json.dumps(Path(filename.replace("\\", "/")).name)
    pipeline_code = f'''"""Reusable deterministic CSV cleaning pipeline generated by SOLOEARN."""
from __future__ import annotations
import argparse
import json
import logging
from pathlib import Path
import re
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
DEFAULT_INPUT = {default_input}

def normalise_headers(columns):
    result = []
    for index, column in enumerate(columns, start=1):
        name = re.sub(r"[^a-z0-9_]+", "_", str(column).strip().lower()).strip("_") or f"column_{{index}}"
        result.append(name)
    if len(set(result)) != len(result):
        raise ValueError("Header normalization created duplicate column names")
    return result

def sanitize_export_value(value):
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in ("=", "+", "-", "@"):
        return value
    try:
        float(stripped)
        return value
    except ValueError:
        return value if value.startswith("'") else "'" + value

def run_pipeline(input_path: str = DEFAULT_INPUT, output_path: str = "cleaned_dataset.csv") -> int:
    source = Path(input_path)
    destination = Path(output_path)
    if not source.is_file():
        logging.error("Input file not found: %s", source)
        return 1
    chunks = []
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            reader = pd.read_csv(source, encoding=enc, chunksize=10000, on_bad_lines="skip", low_memory=True)
            for ch in reader:
                chunks.append(ch)
            break
        except Exception:
            chunks = []
            continue
    if not chunks:
        df = pd.read_csv(source)
    else:
        df = pd.concat(chunks, ignore_index=True)
    if df.empty or len(df.columns) == 0:
        raise ValueError("Input CSV contains no rows or columns")
    initial_rows = len(df)
    df.columns = normalise_headers(df.columns)
    df = df.drop_duplicates().copy()
    for column in df.select_dtypes(include=["number"]).columns:
        mean_value = df[column].mean()
        if pd.notna(mean_value):
            df[column] = df[column].fillna(mean_value)
    for column in df.select_dtypes(include=["object", "string"]).columns:
        df[column] = df[column].map(sanitize_export_value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(destination, index=False)
    logging.info("Processed %s rows; removed %s duplicates", len(df), initial_rows - len(df))
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run SOLOEARN CSV cleaning pipeline")
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default="cleaned_dataset.csv")
    args = parser.parse_args()
    raise SystemExit(run_pipeline(args.input, args.output))
'''
    safe_relative_write(workspace, "pipeline.py", pipeline_code)

    dict_md = [
        "# Data Dictionary & Schema Documentation",
        "",
        f"- Source file: `{Path(filename.replace(chr(92), '/')).name}`",
        f"- Input rows: {input_rows}",
        f"- Processed rows: {len(df)}",
        f"- Duplicate rows removed: {duplicate_rows}",
        f"- Columns: {len(df.columns)}",
        "",
        "| Column | Data type | Null count | Distinct values |",
        "|---|---|---:|---:|",
    ]
    for col in df.columns:
        safe_col = str(col).replace("|", "\\|").replace("\n", " ")
        dict_md.append(f"| `{safe_col}` | `{df[col].dtype}` | {int(df[col].isna().sum())} | {int(df[col].nunique(dropna=True))} |")
    dict_md.extend(["", "## Run", f"`python pipeline.py --input {Path(filename.replace(chr(92), '/')).name!r} --output cleaned_dataset.csv`", ""])
    safe_relative_write(workspace, "data_dictionary.md", "\n".join(dict_md))

    test_code = '''from pathlib import Path
import tempfile
import unittest
import pandas as pd
from pipeline import run_pipeline

class TestDataPipeline(unittest.TestCase):
    def test_pipeline_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "input.csv"
            output = base / "output.csv"
            pd.DataFrame({"Item Name": ["A", "A", "B"], "Amount": [1, 1, 2]}).to_csv(source, index=False)
            self.assertEqual(run_pipeline(str(source), str(output)), 0)
            result = pd.read_csv(output)
            self.assertEqual(len(result), 2)
            self.assertEqual(list(result.columns), ["item_name", "amount"])

if __name__ == "__main__":
    unittest.main()
'''
    safe_relative_write(workspace, "test_pipeline.py", test_code)

    deliverables = ["cleaned_dataset.csv", "schema_manifest.json", "pipeline.py", "data_dictionary.md", "test_pipeline.py"]
    package_deliverables_zip(workspace, deliverables)
    return deliverables


def execute_dashboard_development_service(workspace: Path, file_bytes: bytes, filename: str) -> list[str]:
    """Generate a standalone HTML dashboard with escaped untrusted dataset content."""
    df = _read_csv_strict(file_bytes, filename)
    display_name = Path(filename.replace("\\", "/")).name
    numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
    primary_metric = str(numeric_cols[0]) if numeric_cols else "count"
    if numeric_cols:
        series = df[primary_metric].dropna()
        metric_sum = float(series.sum()) if len(series) else 0.0
        metric_avg = float(series.mean()) if len(series) else 0.0
    else:
        metric_sum = float(len(df))
        metric_avg = float(len(df))

    kpis = {
        "total_records": int(len(df)),
        "primary_metric_name": primary_metric,
        "total_volume": round(metric_sum, 2),
        "average_volume": round(metric_avg, 2),
        "columns_count": int(len(df.columns)),
    }
    safe_relative_write(workspace, "kpi_metrics.json", json.dumps(kpis, indent=2, allow_nan=False))

    esc = lambda value: html.escape(str(value) if value is not None else "", quote=True)
    display_cols = list(df.columns[:8])
    table_headers = "".join(f"<th>{esc(c)}</th>" for c in display_cols)
    sub_df = df[display_cols].head(50)
    table_rows = [
        "<tr>" + "".join(f"<td>{esc(val)}</td>" for val in row) + "</tr>"
        for row in sub_df.itertuples(index=False)
    ]
    table_body = "".join(table_rows)
    safe_metric = esc(primary_metric)
    safe_filename = esc(display_name)
    html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Executive Interactive Dashboard - {safe_filename}</title>
<style>
:root {{ --bg-primary:#0f172a; --bg-card:#1e293b; --text-primary:#f8fafc; --text-secondary:#94a3b8; --accent:#38bdf8; --border:#334155; }}
body {{ font-family:Arial,sans-serif;background:var(--bg-primary);color:var(--text-primary);margin:0;padding:24px; }}
.container {{ max-width:1200px;margin:0 auto; }}
header {{ display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid var(--border);padding-bottom:16px;margin-bottom:24px; }}
.grid {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-bottom:24px; }}
.card {{ background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:20px; }}
.label {{ color:var(--text-secondary);font-size:12px;text-transform:uppercase;letter-spacing:0.05em; }}
.value {{ font-size:27px;font-weight:700;margin-top:8px;color:#f8fafc; }}
.sub {{ font-size:12px;color:var(--text-secondary);margin-top:4px; }}
input {{ width:100%;box-sizing:border-box;padding:12px;border-radius:8px;margin:12px 0;background:#0f172a;color:#fff;border:1px solid var(--border); }}
table {{ width:100%;border-collapse:collapse; }}
th,td {{ text-align:left;padding:9px;border-bottom:1px solid var(--border);overflow-wrap:anywhere; }}
th {{ background:#0f172a;position:sticky;top:0; }}
</style>
</head>
<body><main class="container">
<header><div><h1>Interactive Executive Dashboard</h1><p>Source: {safe_filename} · High-Efficiency Tabular Delivery Pipeline</p></div><span>SOLOEARN</span></header>
<section class="grid">
<div class="card"><div class="label">Total records</div><div class="value">{len(df):,}</div><div class="sub">Verified rows</div></div>
<div class="card"><div class="label">Total {safe_metric}</div><div class="value">{metric_sum:,.2f}</div><div class="sub">Aggregate volume</div></div>
<div class="card"><div class="label">Average {safe_metric}</div><div class="value">{metric_avg:,.2f}</div><div class="sub">Arithmetic mean</div></div>
<div class="card"><div class="label">Columns</div><div class="value">{len(df.columns)}</div><div class="sub">Attributes analyzed</div></div>
</section>
<section class="card"><h2>Data explorer (first 50 rows)</h2><label for="searchInput">Filter rows</label><input id="searchInput" type="search" oninput="filterTable()" placeholder="Search visible rows">
<div style="overflow-x:auto;max-height:600px"><table id="dataTable"><thead><tr>{table_headers}</tr></thead><tbody>{table_body}</tbody></table></div></section>
</main>
<script>
function filterTable() {{
  const filter = document.getElementById('searchInput').value.toLocaleUpperCase();
  document.querySelectorAll('#dataTable tbody tr').forEach(row => {{
    row.hidden = !row.textContent.toLocaleUpperCase().includes(filter);
  }});
}}
</script>
</body></html>'''
    safe_relative_write(workspace, "dashboard.html", html_content)

    guide = f"""# Executive Interactive Dashboard User Guide

The offline dashboard was generated from `{display_name}`.

- It displays record count and the first numeric column's sum and average.
- The table previews up to 50 records and supports client-side text filtering.
- Validate the metric selection and business meaning before sharing with a client.
"""
    safe_relative_write(workspace, "dashboard_user_guide.md", guide)
    deliverables = ["dashboard.html", "kpi_metrics.json", "dashboard_user_guide.md"]
    package_deliverables_zip(workspace, deliverables)
    return deliverables


def execute_business_automation_service(
    workspace: Path,
    file_bytes: bytes,
    filename: str,
    task_title: str,
) -> list[str]:
    """Create and run a concrete CSV reconciliation automation, not a no-op scaffold."""
    source_df = _normalise_columns(_read_csv_strict(file_bytes, filename))
    input_rows = int(len(source_df))
    duplicate_rows = int(source_df.duplicated().sum())
    result_df = source_df.drop_duplicates().copy()
    result_df = _sanitize_export_frame(result_df)
    safe_relative_write(workspace, "reconciled_output.csv", result_df.to_csv(index=False))

    summary = {
        "workflow": "csv_reconciliation",
        "source_filename": Path(filename.replace("\\", "/")).name,
        "input_rows": input_rows,
        "output_rows": int(len(result_df)),
        "duplicates_removed": duplicate_rows,
        "columns": [str(c) for c in source_df.columns],
        "status": "completed",
    }
    safe_relative_write(workspace, "automation_summary.json", json.dumps(summary, indent=2, allow_nan=False))

    automation_code = '''from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
import pandas as pd


def normalise_headers(columns):
    names = []
    for index, col in enumerate(columns, start=1):
        name = re.sub(r"[^a-z0-9_]+", "_", str(col).strip().lower()).strip("_") or f"column_{index}"
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError("Header normalization created duplicate column names")
    return names


def sanitize_export_value(value):
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in ("=", "+", "-", "@"):
        return value
    try:
        float(stripped)
        return value
    except ValueError:
        return value if value.startswith("'") else "'" + value


def run_automation(input_path: str, output_path: str, summary_path: str = "automation_summary.json") -> dict:
    source = Path(input_path)
    if not source.is_file():
        raise FileNotFoundError(f"Input CSV not found: {source}")
    chunks = []
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            reader = pd.read_csv(source, encoding=enc, chunksize=10000, on_bad_lines="skip", low_memory=True)
            for ch in reader:
                chunks.append(ch)
            break
        except Exception:
            chunks = []
            continue
    if not chunks:
        frame = pd.read_csv(source)
    else:
        frame = pd.concat(chunks, ignore_index=True)
    if frame.empty or len(frame.columns) == 0:
        raise ValueError("Input CSV has no data rows or columns")
    input_rows = len(frame)
    frame.columns = normalise_headers(frame.columns)
    duplicates_removed = int(frame.duplicated().sum())
    frame = frame.drop_duplicates().copy()
    for column in frame.select_dtypes(include=["object", "string"]).columns:
        frame[column] = frame[column].map(sanitize_export_value)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(destination, index=False)
    result = {
        "input_rows": input_rows,
        "output_rows": len(frame),
        "duplicates_removed": duplicates_removed,
        "columns": list(frame.columns),
        "status": "completed",
    }
    Path(summary_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Normalize CSV headers and remove duplicate rows")
    parser.add_argument("--input", required=True, help="Path to a CSV file you are authorized to process")
    parser.add_argument("--output", default="reconciled_output.csv")
    parser.add_argument("--summary", default="automation_summary.json")
    args = parser.parse_args()
    run_automation(args.input, args.output, args.summary)
'''
    safe_relative_write(workspace, "automation.py", automation_code)

    config_content = json.dumps(
        {
            "workflow_name": "CSV Reconciliation",
            "task_title": str(task_title)[:160],
            "version": "1.1.0",
            "operations": ["normalize_headers", "remove_exact_duplicate_rows", "write_reconciled_csv", "write_summary_json"],
            "input_filename_example": "client_data.csv",
            "default_output": "reconciled_output.csv",
        },
        indent=2,
        ensure_ascii=False,
    )
    safe_relative_write(workspace, "config.json", config_content)
    readme = """# CSV Reconciliation Automation

This workflow performs only the documented, deterministic operations:
1. Reads a supplied CSV file.
2. Normalizes column names to lowercase snake_case.
3. Removes exact duplicate rows.
4. Writes a reconciled CSV and a JSON summary.

Run it with `python automation.py --input your_authorized_file.csv --output reconciled_output.csv`.
It does not connect to external services, send messages, or modify source files.
"""
    safe_relative_write(workspace, "README.md", readme)
    test_code = '''from pathlib import Path
import tempfile
import unittest
import pandas as pd
from automation import run_automation

class TestCSVReconciliation(unittest.TestCase):
    def test_deduplicates_and_reports_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "input.csv"
            output = base / "output.csv"
            summary = base / "summary.json"
            pd.DataFrame({"Order ID": [1, 1, 2], "Amount": [10, 10, 20]}).to_csv(source, index=False)
            result = run_automation(str(source), str(output), str(summary))
            self.assertEqual(result["input_rows"], 3)
            self.assertEqual(result["output_rows"], 2)
            self.assertEqual(result["duplicates_removed"], 1)
            self.assertEqual(list(pd.read_csv(output).columns), ["order_id", "amount"])

if __name__ == "__main__":
    unittest.main()
'''
    safe_relative_write(workspace, "test_automation.py", test_code)

    # Execute the trusted in-process transformation above against the supplied file.
    deliverables = [
        "reconciled_output.csv",
        "automation_summary.json",
        "automation.py",
        "config.json",
        "README.md",
        "test_automation.py",
    ]
    package_deliverables_zip(workspace, deliverables)
    return deliverables
