from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.scoring import opportunity_score, score_label
from core.db import add_opportunity, connect

REQUIRED_COLUMNS = [
    "title",
    "source",
    "url",
    "description",
    "amount_inr",
    "estimated_hours",
    "skill_match_pct",
    "payment_confidence_pct",
    "risk_level",
    "source_permitted",
]

ALLOWED_URL_SCHEMES = {"http", "https"}


@dataclass
class ValidatedRow:
    row_num: int
    data: dict[str, Any]
    score: float
    score_label: str
    is_duplicate: bool = False
    duplicate_reason: str = ""


@dataclass
class InvalidRow:
    row_num: int
    raw_data: dict[str, Any]
    errors: list[str]


@dataclass
class ImportPreview:
    total_rows: int
    valid_rows: list[ValidatedRow] = field(default_factory=list)
    invalid_rows: list[InvalidRow] = field(default_factory=list)
    duplicate_rows: list[ValidatedRow] = field(default_factory=list)

    @property
    def importable_rows(self) -> list[ValidatedRow]:
        return [r for r in self.valid_rows if not r.is_duplicate]


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value in (1, 1.0):
            return True
        if value in (0, 0.0):
            return False
        raise ValueError(f"Cannot parse boolean from numeric value: {value}")
    val_str = str(value).strip().lower()
    if val_str in {"true", "1", "yes", "y", "t"}:
        return True
    if val_str in {"false", "0", "no", "n", "f"}:
        return False
    raise ValueError(f"Invalid boolean value: '{value}'. Expected true/false, 1/0, yes/no.")


def validate_url(url_val: Any) -> tuple[str, str | None]:
    """Returns (normalized_url, error_message). Only http and https URLs allowed."""
    if url_val is None:
        return "", None
    val = str(url_val).strip()
    if not val:
        return "", None

    parsed = urlparse(val)
    if not parsed.scheme:
        return "", "URL missing scheme. Only http:// or https:// URLs are allowed."
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        return "", f"Unsafe URL scheme '{parsed.scheme}'. Only http:// and https:// are permitted."
    if not parsed.netloc:
        return "", "URL missing valid domain/host."

    # Prevent basic embedded javascript or data URIs that might bypass parsing
    if any(ch in val for ch in ["\r", "\n", "\t"]):
        return "", "URL contains illegal control characters."

    return val, None


def get_existing_opportunity_signatures() -> tuple[set[tuple[str, str]], set[str]]:
    """Return sets of existing (title.lower(), source.lower()) and normalized URLs from the database."""
    name_source_set: set[tuple[str, str]] = set()
    url_set: set[str] = set()
    try:
        with connect() as conn:
            rows = conn.execute("SELECT title, source, url FROM opportunities").fetchall()
            for r in rows:
                t = (r["title"] or "").strip().lower()
                s = (r["source"] or "").strip().lower()
                u = (r["url"] or "").strip().lower()
                if t and s:
                    name_source_set.add((t, s))
                if u:
                    url_set.add(u)
    except Exception:
        pass
    return name_source_set, url_set


def parse_and_validate_opportunities_csv(
    file_content: bytes | str | Path,
    check_db_duplicates: bool = True,
) -> ImportPreview:
    """
    Parses and validates a CSV file for opportunity import.
    Accepts bytes, str, or Path.
    Returns ImportPreview with valid, invalid, and duplicate rows.
    """
    if isinstance(file_content, Path):
        text = file_content.read_text(encoding="utf-8-sig")
    elif isinstance(file_content, bytes):
        text = file_content.decode("utf-8-sig", errors="replace")
    else:
        text = file_content

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("CSV file is empty")

    # Map headers to index
    cleaned_header = [h.strip().lower() for h in header]
    missing_cols = [col for col in REQUIRED_COLUMNS if col not in cleaned_header]
    if missing_cols:
        raise ValueError(f"Missing required columns in CSV: {', '.join(missing_cols)}")

    col_idx = {col: cleaned_header.index(col) for col in REQUIRED_COLUMNS}

    existing_titles_sources, existing_urls = (
        get_existing_opportunity_signatures() if check_db_duplicates else (set(), set())
    )
    seen_in_batch_titles_sources: set[tuple[str, str]] = set()
    seen_in_batch_urls: set[str] = set()

    preview = ImportPreview(total_rows=0)

    for row_idx, row in enumerate(reader, start=2):
        if not row or all(c.strip() == "" for c in row):
            continue  # skip blank lines

        preview.total_rows += 1
        raw_dict: dict[str, Any] = {}
        row_errors: list[str] = []

        # Extract values
        for col_name, idx in col_idx.items():
            raw_dict[col_name] = row[idx].strip() if idx < len(row) else ""

        # Validate title
        title = raw_dict.get("title", "").strip()
        if not title:
            row_errors.append("Title is required and cannot be empty.")

        # Validate source
        source = raw_dict.get("source", "").strip()
        if not source:
            row_errors.append("Source is required and cannot be empty.")

        # Validate description
        description = raw_dict.get("description", "").strip()

        # Validate URL
        url_raw = raw_dict.get("url", "").strip()
        valid_url, url_err = validate_url(url_raw)
        if url_err:
            row_errors.append(url_err)

        # Validate amount_inr
        amount_raw = raw_dict.get("amount_inr", "")
        amount_inr: float = 0.0
        try:
            amount_inr = float(amount_raw)
            if amount_inr < 0:
                row_errors.append(f"amount_inr cannot be negative (got {amount_raw}).")
        except ValueError:
            row_errors.append(f"amount_inr must be a valid number (got '{amount_raw}').")

        # Validate estimated_hours
        hours_raw = raw_dict.get("estimated_hours", "")
        estimated_hours: float = 1.0
        try:
            estimated_hours = float(hours_raw)
            if estimated_hours <= 0:
                row_errors.append(f"estimated_hours must be greater than zero (got {hours_raw}).")
        except ValueError:
            row_errors.append(f"estimated_hours must be a valid number (got '{hours_raw}').")

        # Validate skill_match_pct (0 - 100)
        skill_raw = raw_dict.get("skill_match_pct", "")
        skill_match: float = 0.5
        try:
            skill_pct = float(skill_raw)
            if not 0 <= skill_pct <= 100:
                row_errors.append(f"skill_match_pct must be between 0 and 100 (got {skill_raw}).")
            else:
                skill_match = skill_pct / 100.0
        except ValueError:
            row_errors.append(f"skill_match_pct must be a valid number between 0 and 100 (got '{skill_raw}').")

        # Validate payment_confidence_pct (0 - 100)
        conf_raw = raw_dict.get("payment_confidence_pct", "")
        payment_confidence: float = 0.5
        try:
            conf_pct = float(conf_raw)
            if not 0 <= conf_pct <= 100:
                row_errors.append(f"payment_confidence_pct must be between 0 and 100 (got {conf_raw}).")
            else:
                payment_confidence = conf_pct / 100.0
        except ValueError:
            row_errors.append(f"payment_confidence_pct must be a valid number between 0 and 100 (got '{conf_raw}').")

        # Validate risk_level (0 - 10)
        risk_raw = raw_dict.get("risk_level", "")
        risk_level: float = 5.0
        try:
            risk_val = float(risk_raw)
            if not 0 <= risk_val <= 10:
                row_errors.append(f"risk_level must be between 0 and 10 (got {risk_raw}).")
            else:
                risk_level = risk_val
        except ValueError:
            row_errors.append(f"risk_level must be a valid number between 0 and 10 (got '{risk_raw}').")

        # Validate source_permitted
        permitted_raw = raw_dict.get("source_permitted", "")
        source_permitted: bool = False
        try:
            source_permitted = parse_bool(permitted_raw)
        except ValueError as exc:
            row_errors.append(str(exc))

        if row_errors:
            preview.invalid_rows.append(InvalidRow(row_num=row_idx, raw_data=raw_dict, errors=row_errors))
            continue

        # Calculate score
        score = opportunity_score(
            amount_inr=amount_inr,
            estimated_hours=estimated_hours,
            skill_match=skill_match,
            payment_confidence=payment_confidence,
            risk_level=risk_level,
            source_permitted=source_permitted,
        )
        label = score_label(score)

        clean_data = {
            "title": title,
            "source": source,
            "url": valid_url,
            "description": description,
            "amount_inr": amount_inr,
            "estimated_hours": estimated_hours,
            "skill_match": skill_match,
            "payment_confidence": payment_confidence,
            "risk_level": risk_level,
            "source_permitted": source_permitted,
        }

        # Duplicate checking
        sig = (title.lower(), source.lower())
        url_sig = valid_url.lower() if valid_url else ""

        is_dup = False
        dup_reason = ""

        if sig in seen_in_batch_titles_sources:
            is_dup = True
            dup_reason = "Duplicate of another row in this file (same title & source)."
        elif url_sig and url_sig in seen_in_batch_urls:
            is_dup = True
            dup_reason = "Duplicate of another row in this file (same URL)."
        elif sig in existing_titles_sources:
            is_dup = True
            dup_reason = "Already exists in database (matching title & source)."
        elif url_sig and url_sig in existing_urls:
            is_dup = True
            dup_reason = "Already exists in database (matching URL)."

        seen_in_batch_titles_sources.add(sig)
        if url_sig:
            seen_in_batch_urls.add(url_sig)

        validated_row = ValidatedRow(
            row_num=row_idx,
            data=clean_data,
            score=score,
            score_label=label,
            is_duplicate=is_dup,
            duplicate_reason=dup_reason,
        )

        preview.valid_rows.append(validated_row)
        if is_dup:
            preview.duplicate_rows.append(validated_row)

    return preview


def commit_imported_opportunities(rows: list[ValidatedRow], skip_duplicates: bool = True) -> list[int]:
    """
    Commits validated rows to the database.
    Returns list of newly created opportunity IDs.
    """
    imported_ids: list[int] = []
    for r in rows:
        if r.is_duplicate and skip_duplicates:
            continue
        item_id = add_opportunity(r.data, r.score)
        imported_ids.append(item_id)
    return imported_ids
