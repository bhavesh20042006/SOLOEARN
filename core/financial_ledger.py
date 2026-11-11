from __future__ import annotations

"""
SOLOEARN Financial Foundation & Ledger Engine (M6.1.2)
Implements:
1. Append-only financial transaction ledger with integer paise / exact arithmetic.
2. Comprehensive business expense registration and categorization.
3. Bank statement CSV import and owner-verified reconciliation.
4. Exact cash runway separation:
   Distributable Cash C = B - G - T - U - W
   (Bank Cash - GST Liability - Tax Reserve - Unpaid Bills/Refunds - Working Capital Buffer).
"""

import csv
import io
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.db import connect, utc_now, get_bank_opening_balance, get_active_tax_profile
from core.tax_engine import compute_business_incremental_tax, validate_tax_profile

DEFAULT_WORKING_CAPITAL_BUFFER_INR = 25000.0

VALID_LEDGER_TX_TYPES = {
    "client_receipt",
    "platform_fee",
    "refund",  # Client refund excluding GST; use gst_refund for the tax component.
    "gst_refund",
    "other_cash_in",
    "other_cash_out",
    "direct_cost",
    "expense",
    "gst_collected",
    "gst_paid",
    "tds_credit",
    "tax_provision",
    "tax_payment",
    "owner_draw",
}

VALID_EXPENSE_CATEGORIES = {
    "direct_project_cost",
    "software_tool",
    "marketing_acquisition",
    "accounting_legal",
    "hosting_infra",
    "office_overhead",
    "other",
}

VALID_TAX_DEDUCTIBILITY = {
    "unknown",
    "deductible",
    "partly_deductible",
    "non_deductible",
}


def inr_to_paise(amount_inr: float) -> int:
    """Converts a floating-point or numeric INR amount into exact integer paise."""
    if math.isnan(amount_inr) or math.isinf(amount_inr):
        raise ValueError("Invalid non-finite currency amount")
    return int(round(amount_inr * 100))


def paise_to_inr(paise: int) -> float:
    """Converts exact integer paise back into INR float rounded to 2 decimals."""
    return round(paise / 100.0, 2)


# ---------------------------------------------------------------------------
# 1. APPEND-ONLY FINANCIAL TRANSACTION LEDGER
# ---------------------------------------------------------------------------

def record_ledger_transaction(
    tx_type: str,
    amount_inr: float,
    description: str,
    reference_id: str = "",
    tx_date: str = "",
    is_reversal: bool = False,
    linked_tx_id: int | None = None,
    is_sandbox: bool = False,
) -> int:
    """
    Appends an immutable financial transaction into the ledger.
    Never alters or mutates past rows. Reversals are separate entries linking to the original.
    """
    if tx_type not in VALID_LEDGER_TX_TYPES:
        raise ValueError(f"Invalid transaction type: '{tx_type}'. Must be one of {VALID_LEDGER_TX_TYPES}")

    if math.isnan(amount_inr) or math.isinf(amount_inr) or amount_inr < 0:
        raise ValueError("Transaction amount must be a positive finite number")

    paise = inr_to_paise(amount_inr)
    eff_date = tx_date.strip() if tx_date else utc_now()[:10]

    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO financial_transactions (
                tx_date, tx_type, amount_inr, amount_paise, currency,
                reference_id, description, is_reversal, linked_tx_id, is_sandbox, created_at
            ) VALUES (?, ?, ?, ?, 'INR', ?, ?, ?, ?, ?, ?)""",
            (
                eff_date,
                tx_type,
                amount_inr,
                paise,
                str(reference_id)[:100],
                str(description)[:255],
                1 if is_reversal else 0,
                linked_tx_id,
                1 if is_sandbox else 0,
                utc_now(),
            ),
        )
        tx_id = int(cur.lastrowid)

    return tx_id


def record_invoice_refund(
    invoice_number: str,
    gross_refund_inr: float,
    refund_reference: str,
    owner_confirmed: bool = False,
    tx_date: str = "",
) -> dict[str, Any]:
    """
    Records a client refund as separate ex-GST service refund and GST-refund entries.
    Cumulative allocation is proportional to the invoice subtotal/tax and cannot exceed
    the original gross invoice total. This is a ledger record, not a bank transfer.
    """
    if owner_confirmed is not True:
        raise ValueError("Recording a refund requires explicit owner confirmation")
    if not isinstance(invoice_number, str) or not invoice_number.strip():
        raise ValueError("Invoice number is required")
    if not isinstance(refund_reference, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", refund_reference.strip()):
        raise ValueError("Refund reference must be 1–80 safe alphanumeric/dot/underscore/hyphen characters")
    if isinstance(gross_refund_inr, bool):
        raise ValueError("Refund amount must be numeric, not boolean")
    try:
        gross_refund = round(float(gross_refund_inr), 2)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Refund amount must be a valid number") from exc
    if not math.isfinite(gross_refund) or gross_refund <= 0:
        raise ValueError("Refund amount must be a positive finite number")

    eff_date = tx_date.strip() if tx_date else utc_now()[:10]
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        inv = conn.execute(
            "SELECT * FROM invoices WHERE invoice_number=?",
            (invoice_number.strip(),),
        ).fetchone()
        if not inv:
            raise ValueError(f"Invoice {invoice_number!r} was not found")
        if inv["status"] != "paid":
            raise ValueError("Only a paid invoice can be refunded")

        subtotal = round(float(inv["amount_subtotal"]), 2)
        gst_amount = round(float(inv["tax_amount"]), 2)
        gross_total = round(float(inv["amount_total"]), 2)
        if gross_total <= 0:
            raise ValueError("Invoice total must be positive")

        prior = conn.execute(
            """SELECT tx_type, COALESCE(SUM(amount_inr), 0) AS total
               FROM financial_transactions
               WHERE reference_id LIKE ? AND tx_type IN ('refund','gst_refund')
               GROUP BY tx_type""",
            (f"REFUND:{invoice_number.strip()}:%",),
        ).fetchall()
        prior_by_type = {r["tx_type"]: round(float(r["total"] or 0.0), 2) for r in prior}
        prior_ex = prior_by_type.get("refund", 0.0)
        prior_gst = prior_by_type.get("gst_refund", 0.0)
        prior_gross = round(prior_ex + prior_gst, 2)

        ref_id = f"REFUND:{invoice_number.strip()}:{refund_reference.strip()}"
        dupe = conn.execute(
            "SELECT id FROM financial_transactions WHERE reference_id=? AND tx_type IN ('refund','gst_refund') LIMIT 1",
            (ref_id,),
        ).fetchone()
        if dupe:
            raise ValueError("This refund reference has already been recorded")

        if prior_gross + gross_refund > gross_total + 0.009:
            raise ValueError("Cumulative refunds cannot exceed the original invoice total")

        new_cumulative_gross = round(prior_gross + gross_refund, 2)
        new_cumulative_ex = min(subtotal, round(new_cumulative_gross * subtotal / gross_total, 2))
        refund_ex = round(new_cumulative_ex - prior_ex, 2)
        refund_gst = round(gross_refund - refund_ex, 2)
        if refund_ex < 0 or refund_gst < 0 or refund_gst > gst_amount - prior_gst + 0.009:
            raise ValueError("Refund tax allocation failed validation")

        now = utc_now()
        is_sb = int(inv["is_sandbox"] or 0) if "is_sandbox" in inv.keys() else 0
        if refund_ex > 0:
            conn.execute(
                """INSERT INTO financial_transactions (
                    tx_date, tx_type, amount_inr, amount_paise, currency,
                    reference_id, description, is_reversal, is_sandbox, created_at
                ) VALUES (?, 'refund', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                (
                    eff_date, refund_ex, inr_to_paise(refund_ex), ref_id,
                    f"Ex-GST client refund for {invoice_number.strip()}", is_sb, now,
                ),
            )
        if refund_gst > 0:
            conn.execute(
                """INSERT INTO financial_transactions (
                    tx_date, tx_type, amount_inr, amount_paise, currency,
                    reference_id, description, is_reversal, is_sandbox, created_at
                ) VALUES (?, 'gst_refund', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                (
                    eff_date, refund_gst, inr_to_paise(refund_gst), ref_id,
                    f"GST component of client refund for {invoice_number.strip()}", is_sb, now,
                ),
            )

    return {
        "invoice_number": invoice_number.strip(),
        "refund_reference": refund_reference.strip(),
        "gross_refund_inr": gross_refund,
        "service_refund_ex_gst_inr": refund_ex,
        "gst_refund_inr": refund_gst,
        "status": "recorded_owner_confirmed",
        "note": "This records a confirmed refund; it does not initiate or verify a bank transfer.",
    }


def list_ledger_transactions(limit: int = 100, tx_type: str = "", include_sandbox: bool = True) -> list[dict[str, Any]]:
    """Retrieves recent ledger transactions in reverse chronological order."""
    query = "SELECT * FROM financial_transactions"
    params: list[Any] = []
    conditions: list[str] = []
    if tx_type:
        conditions.append("tx_type = ?")
        params.append(tx_type)
    if not include_sandbox:
        conditions.append("(is_sandbox = 0 OR is_sandbox IS NULL)")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))

    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 2. BUSINESS EXPENSES MANAGEMENT
# ---------------------------------------------------------------------------

def register_expense(
    vendor: str,
    category: str,
    amount_inr: float,
    expense_date: str = "",
    job_id: int | None = None,
    is_recurring: bool = False,
    business_use_pct: float = 100.0,
    gst_amount_inr: float = 0.0,
    tax_deductibility: str = "unknown",
    reviewer_notes: str = "",
    ca_review_confirmed: bool = False,
    ca_review_notes: str = "",
    reviewed_by: str = "",
    is_sandbox: bool = False,
) -> int:
    """
    Registers an incurred business expense in the expenses table.
    Note: Vendor GST is recorded as informational only (no automatic ITC claimed).
    Tax deductibility requires explicit ca_review_confirmed=True with CA guidance/records.
    Incurring an expense does not reduce bank cash until a bank debit is reconciled.
    """
    if not vendor or not vendor.strip():
        raise ValueError("Vendor name is required")

    if category not in VALID_EXPENSE_CATEGORIES:
        raise ValueError(f"Invalid expense category: '{category}'. Valid: {VALID_EXPENSE_CATEGORIES}")

    if math.isnan(amount_inr) or math.isinf(amount_inr) or amount_inr <= 0:
        raise ValueError("Expense amount must be a positive finite number")

    if not (0.0 <= business_use_pct <= 100.0):
        raise ValueError("Business use percentage must be between 0 and 100")

    if tax_deductibility not in VALID_TAX_DEDUCTIBILITY:
        raise ValueError(f"Invalid tax deductibility: '{tax_deductibility}'")

    if tax_deductibility in ("deductible", "partly_deductible") and not ca_review_confirmed:
        raise ValueError("Tax deductibility requires explicit ca_review_confirmed=True with CA guidance/records")

    paise = inr_to_paise(amount_inr)
    eff_date = expense_date.strip() if expense_date else utc_now()[:10]
    now = utc_now()

    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO expenses (
                expense_date, vendor, category, amount_inr, amount_paise, currency,
                job_id, is_recurring, business_use_pct, gst_amount_inr,
                tax_deductibility, reviewer_notes, ca_review_confirmed,
                ca_review_notes, reviewed_by, reviewed_at, is_sandbox, created_at
            ) VALUES (?, ?, ?, ?, ?, 'INR', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                eff_date,
                vendor.strip()[:120],
                category,
                amount_inr,
                paise,
                job_id,
                1 if is_recurring else 0,
                business_use_pct,
                max(0.0, gst_amount_inr),
                tax_deductibility,
                reviewer_notes.strip()[:500],
                1 if ca_review_confirmed else 0,
                ca_review_notes.strip()[:500],
                reviewed_by.strip()[:100],
                now if ca_review_confirmed else None,
                1 if is_sandbox else 0,
                now,
            ),
        )
        expense_id = int(cur.lastrowid)

    return expense_id


def confirm_expense_tax_deductibility(
    expense_id: int,
    tax_deductibility: str,
    ca_review_confirmed: bool = False,
    ca_review_notes: str = "",
    reviewed_by: str = "",
) -> dict[str, Any]:
    """
    Updates the tax deductibility of an expense following CA review.
    Deductible/partly_deductible requires explicit ca_review_confirmed=True.
    """
    if tax_deductibility not in VALID_TAX_DEDUCTIBILITY:
        raise ValueError(f"Invalid tax deductibility: '{tax_deductibility}'")

    if tax_deductibility in ("deductible", "partly_deductible") and not ca_review_confirmed:
        raise ValueError("Tax deductibility requires explicit ca_review_confirmed=True with CA guidance/records")

    with connect() as conn:
        exp = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if not exp:
            raise ValueError(f"Expense #{expense_id} not found")

        now = utc_now()
        conn.execute(
            """UPDATE expenses
               SET tax_deductibility = ?,
                   ca_review_confirmed = ?,
                   ca_review_notes = ?,
                   reviewed_by = ?,
                   reviewed_at = ?
               WHERE id = ?""",
            (
                tax_deductibility,
                1 if ca_review_confirmed else 0,
                ca_review_notes.strip()[:500],
                reviewed_by.strip()[:100],
                now if ca_review_confirmed else None,
                expense_id,
            ),
        )

    return {
        "expense_id": expense_id,
        "tax_deductibility": tax_deductibility,
        "ca_review_confirmed": ca_review_confirmed,
        "status": "updated",
    }


def list_expenses(
    limit: int = 100,
    category: str = "",
    job_id: int | None = None,
    include_sandbox: bool = True,
) -> list[dict[str, Any]]:
    """Lists recorded expenses with optional category or job filters."""
    query = "SELECT * FROM expenses"
    params: list[Any] = []
    conditions: list[str] = []

    if category:
        conditions.append("category = ?")
        params.append(category)
    if job_id is not None:
        conditions.append("job_id = ?")
        params.append(job_id)
    if not include_sandbox:
        conditions.append("(is_sandbox = 0 OR is_sandbox IS NULL)")

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))

    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_expenses_summary(month: str = "", include_sandbox: bool = False) -> dict[str, Any]:
    """Computes total expenses, categorized breakdown, and deductible sums."""
    eff_month = month.strip() if month else ""
    sandbox_clause = "AND (is_sandbox = 0 OR is_sandbox IS NULL)" if not include_sandbox else ""
    with connect() as conn:
        if eff_month and eff_month != "all":
            rows = conn.execute(
                f"""SELECT category, SUM(amount_inr) as total_inr, COUNT(*) as count 
                   FROM expenses 
                   WHERE expense_date LIKE ? {sandbox_clause}
                   GROUP BY category""",
                (f"{eff_month}%",),
            ).fetchall()
            deductible_row = conn.execute(
                f"""SELECT SUM(amount_inr * (business_use_pct / 100.0)) as total_deductible
                   FROM expenses 
                   WHERE expense_date LIKE ? 
                     AND tax_deductibility IN ('deductible', 'partly_deductible')
                     AND ca_review_confirmed = 1 {sandbox_clause}""",
                (f"{eff_month}%",),
            ).fetchone()
        else:
            where_clause = "WHERE (is_sandbox = 0 OR is_sandbox IS NULL)" if not include_sandbox else ""
            rows = conn.execute(
                f"SELECT category, SUM(amount_inr) as total_inr, COUNT(*) as count FROM expenses {where_clause} GROUP BY category"
            ).fetchall()
            deductible_where = "WHERE tax_deductibility IN ('deductible', 'partly_deductible') AND ca_review_confirmed = 1"
            if not include_sandbox:
                deductible_where += " AND (is_sandbox = 0 OR is_sandbox IS NULL)"
            deductible_row = conn.execute(
                f"""SELECT SUM(amount_inr * (business_use_pct / 100.0)) as total_deductible
                   FROM expenses {deductible_where}"""
            ).fetchone()

        by_cat = {r["category"]: round(float(r["total_inr"] or 0.0), 2) for r in rows}
        total_exp = sum(by_cat.values())
        deductible_inr = round(float(deductible_row["total_deductible"] or 0.0), 2) if deductible_row else 0.0

    return {
        "total_expenses_inr": round(total_exp, 2),
        "deductible_expenses_inr": deductible_inr,
        "by_category": by_cat,
    }


# ---------------------------------------------------------------------------
# 3. BANK STATEMENT CSV IMPORT & RECONCILIATION
# ---------------------------------------------------------------------------

def import_bank_statement_csv(file_content: str | bytes, filename: str, notes: str = "") -> dict[str, Any]:
    """
    Imports a standard bank statement CSV into bank_statement_imports and bank_statement_rows.
    Normalizes column names: Date, Description/Narration, Amount/Credit/Debit, Ref/UTR.
    """
    if isinstance(file_content, bytes):
        text = file_content.decode("utf-8", errors="replace")
    else:
        text = str(file_content)

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("Bank statement file is empty")

    # Locate header row dynamically (skips leading account metadata lines e.g. Kotak/HDFC statement exports)
    header_row_idx = 0
    header = []
    for r_idx, r_candidate in enumerate(rows):
        cand = [h.strip().lower().replace(" ", "_") for h in r_candidate]
        has_date = any("date" in c for c in cand)
        has_desc = any(any(k in c for k in ("description", "narration", "particulars", "remarks", "details")) for c in cand)
        has_amt = any(any(k in c for k in ("amount", "credit", "deposit", "withdrawal", "debit", "balance")) for c in cand)
        if (has_date and has_desc) or (has_date and has_amt) or (has_desc and has_amt):
            header_row_idx = r_idx
            header = cand
            break

    if not header:
        header = [h.strip().lower().replace(" ", "_") for h in rows[0]]

    # Detect date column
    date_idx = -1
    for idx, col in enumerate(header):
        if "date" in col or "txn_date" in col:
            date_idx = idx
            break
    if date_idx == -1:
        date_idx = 0  # Fallback to column 0

    # Detect description column
    desc_idx = -1
    for idx, col in enumerate(header):
        if any(k in col for k in ("description", "narration", "particulars", "remarks", "details")):
            desc_idx = idx
            break
    if desc_idx == -1:
        desc_idx = 1 if len(header) > 1 else 0

    # Detect amount / credit / debit columns
    credit_idx = -1
    debit_idx = -1
    amount_idx = -1
    type_idx = -1
    for idx, col in enumerate(header):
        if "credit" in col or "deposit" in col:
            credit_idx = idx
        elif "debit" in col or "withdrawal" in col:
            debit_idx = idx
        elif "amount" in col:
            amount_idx = idx
        elif ("dr" in col and "cr" in col) or any(k in col for k in ("type", "txn_type", "transaction_type")):
            type_idx = idx

    # Detect reference / UTR column
    ref_idx = -1
    for idx, col in enumerate(header):
        if any(k in col for k in ("ref", "utr", "chq", "cheque", "transaction_id", "reference")):
            ref_idx = idx
            break

    parsed_rows: list[dict[str, Any]] = []
    for r in rows[header_row_idx + 1:]:
        if not r or all(c.strip() == "" for c in r):
            continue

        raw_date = r[date_idx].strip() if len(r) > date_idx else utc_now()[:10]
        raw_desc = r[desc_idx].strip() if len(r) > desc_idx else "Bank entry"
        raw_ref = r[ref_idx].strip() if ref_idx != -1 and len(r) > ref_idx else ""

        # Extract 12-digit UPI RRN / UTR from narration if raw_ref column is absent or empty
        if not raw_ref:
            upi_match = re.search(r"UPI[/_-]([A-Za-z0-9]{10,16})", raw_desc, re.IGNORECASE)
            if upi_match:
                raw_ref = upi_match.group(1)
            else:
                rrn_match = re.search(r"\b(\d{12})\b", raw_desc)
                if rrn_match:
                    raw_ref = rrn_match.group(1)

        # Extract amount & type
        tx_type = "credit"
        amt = 0.0

        if credit_idx != -1 and len(r) > credit_idx and r[credit_idx].strip():
            clean_str = re.sub(r"[^\d.]", "", r[credit_idx].strip())
            amt = float(clean_str) if clean_str else 0.0
            tx_type = "credit"
        elif debit_idx != -1 and len(r) > debit_idx and r[debit_idx].strip():
            clean_str = re.sub(r"[^\d.]", "", r[debit_idx].strip())
            amt = float(clean_str) if clean_str else 0.0
            tx_type = "debit"
        elif amount_idx != -1 and len(r) > amount_idx:
            raw_val = r[amount_idx].strip()
            clean_str = re.sub(r"[^\d.]", "", raw_val)
            amt = float(clean_str) if clean_str else 0.0
            if type_idx != -1 and len(r) > type_idx:
                raw_type = r[type_idx].strip().lower()
                if any(d in raw_type for d in ("dr", "debit", "withdrawal")):
                    tx_type = "debit"
                else:
                    tx_type = "credit"
            elif "-" in raw_val or "dr" in raw_val.lower():
                tx_type = "debit"
            else:
                tx_type = "credit"

        if amt > 0:
            parsed_rows.append(
                {
                    "date": raw_date[:20],
                    "desc": raw_desc[:250],
                    "amount": round(amt, 2),
                    "type": tx_type,
                    "ref": raw_ref[:100],
                }
            )

    if not parsed_rows:
        raise ValueError("Could not parse any valid transaction rows from bank statement")

    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO bank_statement_imports (filename, imported_at, row_count, notes)
               VALUES (?, ?, ?, ?)""",
            (filename[:120], utc_now(), len(parsed_rows), notes[:300]),
        )
        import_id = int(cur.lastrowid)

        for pr in parsed_rows:
            conn.execute(
                """INSERT INTO bank_statement_rows (
                    import_id, tx_date, description, amount_inr, amount_paise,
                    tx_type, bank_ref, is_reconciled, confirmed_by_owner
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)""",
                (
                    import_id,
                    pr["date"],
                    pr["desc"],
                    pr["amount"],
                    inr_to_paise(pr["amount"]),
                    pr["type"],
                    pr["ref"],
                ),
            )

    return {
        "import_id": import_id,
        "filename": filename,
        "rows_imported": len(parsed_rows),
        "status": "success",
    }


def list_unreconciled_statement_rows(limit: int = 50) -> list[dict[str, Any]]:
    """Returns statement rows awaiting owner confirmation and entity matching."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM bank_statement_rows
               WHERE is_reconciled = 0
               ORDER BY id DESC LIMIT ?""",
            (max(1, min(limit, 200)),),
        ).fetchall()
        return [dict(r) for r in rows]


def reconcile_statement_row(
    row_id: int,
    entity_type: str,
    entity_id: str,
    owner_confirmed: bool = True,
    tds_withheld_inr: float = 0.0,
) -> dict[str, Any]:
    """
    Reconciles a bank statement entry only after validating direction, entity,
    outstanding invoice/expense amount, and duplicate references.

    Supports exact invoice settlement via bank cash alone or bank cash plus verified
    client TDS withheld (Income-tax Act, 2025 Section 393, formerly Section 194J/194C):
      Bank Credit + TDS Withheld == Invoice Gross Total

    Partial settlements without verified TDS or overpayments are rejected.
    """
    if owner_confirmed is not True:
        raise ValueError("Reconciliation requires explicit owner verification checkbox")

    allowed_entities = {"invoice", "expense", "owner_draw", "tax_payment", "gst_payment", "other"}
    if entity_type not in allowed_entities:
        raise ValueError(f"Invalid matched entity type: '{entity_type}'")

    tds_val = max(0.0, round(float(tds_withheld_inr or 0.0), 2))

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        st_row = conn.execute("SELECT * FROM bank_statement_rows WHERE id = ?", (row_id,)).fetchone()
        if not st_row:
            raise ValueError(f"Bank statement row ID {row_id} not found")
        if int(st_row["is_reconciled"] or 0) == 1:
            raise ValueError(f"Bank statement row #{row_id} has already been reconciled")

        tx_direction = str(st_row["tx_type"]).lower()
        amount = round(float(st_row["amount_inr"]), 2)
        if amount <= 0 or not math.isfinite(amount):
            raise ValueError("Bank statement amount must be positive and finite")

        inv = None
        exp = None
        bank_ref = str(st_row["bank_ref"] or "").strip()[:120]

        # Validate the matched entity before marking the statement row reconciled.
        if entity_type == "invoice":
            if tx_direction != "credit":
                raise ValueError("An invoice can only be reconciled against a bank credit")
            inv = conn.execute(
                "SELECT * FROM invoices WHERE invoice_number = ? OR id = ?",
                (str(entity_id), str(entity_id)),
            ).fetchone()
            if not inv:
                raise ValueError(f"Invoice '{entity_id}' was not found")
            if inv["status"] not in {"issued", "payment_pending"}:
                raise ValueError(f"Invoice {inv['invoice_number']} is not awaiting payment")
            expected = round(float(inv["amount_total"]), 2)
            settled_amount = round(amount + tds_val, 2)
            if abs(settled_amount - expected) >= 0.01:
                raise ValueError(
                    f"Bank credit ₹{amount:,.2f} + TDS withheld ₹{tds_val:,.2f} (Total: ₹{settled_amount:,.2f}) "
                    f"does not exactly settle invoice total ₹{expected:,.2f}. Partial/over-payments are not supported yet; "
                    "leave this row unreconciled."
                )
            if bank_ref:
                duplicate_ref = conn.execute(
                    "SELECT id FROM invoices WHERE payment_reference=? AND status='paid' AND id<>? LIMIT 1",
                    (bank_ref, int(inv["id"])),
                ).fetchone()
                if duplicate_ref:
                    raise ValueError("This bank reference is already assigned to another paid invoice")

        elif entity_type == "expense":
            if tx_direction != "debit":
                raise ValueError("An expense can only be reconciled against a bank debit")
            exp_id_clean = str(entity_id).replace("EXP-", "").lstrip("0") or "0"
            exp = conn.execute(
                "SELECT * FROM expenses WHERE id = ?",
                (int(exp_id_clean) if exp_id_clean.isdigit() else -1,),
            ).fetchone()
            if not exp:
                raise ValueError(f"Expense '{entity_id}' was not found")
            expected = round(float(exp["amount_inr"]), 2)
            if abs(amount - expected) >= 0.01:
                raise ValueError(
                    f"Bank debit ₹{amount:,.2f} does not exactly settle expense "
                    f"₹{expected:,.2f}. Partial expense payments are not supported yet; "
                    "leave this row unreconciled."
                )

        elif entity_type in {"owner_draw", "tax_payment", "gst_payment"}:
            if tx_direction != "debit":
                raise ValueError(f"{entity_type} can only be reconciled against a bank debit")

        # Validation succeeded; now atomically mark the row and increment its import count.
        conn.execute(
            """UPDATE bank_statement_rows
               SET is_reconciled = 1,
                   matched_entity_type = ?,
                   matched_entity_id = ?,
                   confirmed_by_owner = 1,
                   confirmed_at = ?
               WHERE id = ? AND is_reconciled = 0""",
            (entity_type, str(entity_id), utc_now(), row_id),
        )
        if conn.total_changes < 1:
            raise ValueError(f"Bank statement row #{row_id} could not be claimed for reconciliation")

        conn.execute(
            """UPDATE bank_statement_imports
               SET reconciled_count = reconciled_count + 1
               WHERE id = ?""",
            (st_row["import_id"],),
        )

        now = utc_now()
        ref_for_row = bank_ref or f"BANK-ROW-{row_id}"

        if entity_type == "invoice" and inv is not None:
            inv_id = int(inv["id"])
            inv_num = str(inv["invoice_number"])
            conn.execute(
                """UPDATE invoices
                   SET status='paid', paid_at=?, payment_reference=?, payment_verified_by_owner=1
                   WHERE id=? AND status IN ('issued','payment_pending')""",
                (now, ref_for_row, inv_id),
            )
            opp_id = int(inv["opportunity_id"])
            task_id = inv["task_id"]
            opp_row = conn.execute("SELECT status FROM opportunities WHERE id=?", (opp_id,)).fetchone()
            if opp_row and opp_row["status"] in {"submitted_by_owner", "won", "invoiced"}:
                conn.execute("UPDATE opportunities SET status='paid' WHERE id=?", (opp_id,))

            is_sb = int(inv["is_sandbox"] or 0) if "is_sandbox" in inv.keys() else 0
            rev_row = conn.execute(
                "SELECT id FROM revenue WHERE description LIKE ? OR description LIKE ?",
                (f"%{inv_num}%", f"%{ref_for_row}%"),
            ).fetchone()
            if not rev_row:
                conn.execute(
                    """INSERT INTO revenue(opportunity_id, task_id, description, amount_inr, status, paid_at, is_sandbox, created_at)
                       VALUES (?, ?, ?, ?, 'paid', ?, ?, ?)""",
                    (
                        opp_id, task_id,
                        f"Reconciled payment for {inv_num} ({inv['client_name']}) - Ref: {ref_for_row}",
                        float(inv["amount_total"]), now, is_sb, now,
                    ),
                )

            existing_tx = conn.execute(
                "SELECT id FROM financial_transactions WHERE reference_id=? AND tx_type='client_receipt'",
                (inv_num,),
            ).fetchone()
            if not existing_tx:
                subtotal = round(float(inv["amount_subtotal"]), 2)
                tax_amt = round(float(inv["tax_amount"]), 2)
                conn.execute(
                    """INSERT INTO financial_transactions (
                        tx_date, tx_type, amount_inr, amount_paise, currency,
                        reference_id, description, is_reversal, is_sandbox, created_at
                    ) VALUES (?, 'client_receipt', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                    (
                        str(st_row["tx_date"])[:10], subtotal, inr_to_paise(subtotal),
                        inv_num, f"Reconciled client receipt for {inv_num} ({inv['client_name']})", is_sb, now,
                    ),
                )
                if tax_amt > 0:
                    conn.execute(
                        """INSERT INTO financial_transactions (
                            tx_date, tx_type, amount_inr, amount_paise, currency,
                            reference_id, description, is_reversal, is_sandbox, created_at
                        ) VALUES (?, 'gst_collected', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                        (
                            str(st_row["tx_date"])[:10], tax_amt, inr_to_paise(tax_amt),
                            inv_num, f"GST collected on {inv_num}", is_sb, now,
                        ),
                    )
                if tds_val > 0:
                    conn.execute(
                        """INSERT INTO financial_transactions (
                            tx_date, tx_type, amount_inr, amount_paise, currency,
                            reference_id, description, is_reversal, is_sandbox, created_at
                        ) VALUES (?, 'tds_credit', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                        (
                            str(st_row["tx_date"])[:10], tds_val, inr_to_paise(tds_val),
                            inv_num, f"TDS withheld by client on {inv_num} ({inv['client_name']})", is_sb, now,
                        ),
                    )
                    conn.execute(
                        "UPDATE tax_profiles SET tds_credits_inr = tds_credits_inr + ? WHERE is_active = 1",
                        (tds_val,),
                    )

        elif entity_type == "expense" and exp is not None:
            is_sb = int(exp["is_sandbox"] or 0) if "is_sandbox" in exp.keys() else 0
            exp_ref = f"EXP-{int(exp['id']):05d}"
            existing_tx = conn.execute(
                "SELECT id FROM financial_transactions WHERE reference_id=? AND tx_type IN ('expense','direct_cost')",
                (exp_ref,),
            ).fetchone()
            if not existing_tx:
                tx_t = "direct_cost" if exp["category"] == "direct_project_cost" else "expense"
                conn.execute(
                    """INSERT INTO financial_transactions (
                        tx_date, tx_type, amount_inr, amount_paise, currency,
                        reference_id, description, is_reversal, is_sandbox, created_at
                    ) VALUES (?, ?, ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                    (
                        str(st_row["tx_date"])[:10], tx_t, amount, inr_to_paise(amount),
                        exp_ref, f"Paid expense {exp['vendor']} ({exp['category']})", is_sb, now,
                    ),
                )

        else:
            ledger_type = {
                "owner_draw": "owner_draw",
                "tax_payment": "tax_payment",
                "gst_payment": "gst_paid",
            }.get(entity_type)
            if ledger_type:
                existing = conn.execute(
                    "SELECT id FROM financial_transactions WHERE reference_id=? AND tx_type=?",
                    (ref_for_row, ledger_type),
                ).fetchone()
                if not existing:
                    conn.execute(
                        """INSERT INTO financial_transactions (
                            tx_date, tx_type, amount_inr, amount_paise, currency,
                            reference_id, description, is_reversal, created_at
                        ) VALUES (?, ?, ?, ?, 'INR', ?, ?, 0, ?)""",
                        (
                            str(st_row["tx_date"])[:10], ledger_type, amount, inr_to_paise(amount),
                            ref_for_row, f"Reconciled {entity_type}: {st_row['description']}", now,
                        ),
                    )
            else:
                # "other" cash movements affect bank cash but never business profit.
                other_type = "other_cash_in" if tx_direction == "credit" else "other_cash_out"
                existing = conn.execute(
                    "SELECT id FROM financial_transactions WHERE reference_id=? AND tx_type=?",
                    (ref_for_row, other_type),
                ).fetchone()
                if not existing:
                    conn.execute(
                        """INSERT INTO financial_transactions (
                            tx_date, tx_type, amount_inr, amount_paise, currency,
                            reference_id, description, is_reversal, created_at
                        ) VALUES (?, ?, ?, ?, 'INR', ?, ?, 0, ?)""",
                        (
                            str(st_row["tx_date"])[:10], other_type, amount, inr_to_paise(amount),
                            ref_for_row, f"Reconciled other bank movement: {st_row['description']}", now,
                        ),
                    )

    return {
        "statement_row_id": row_id,
        "matched_entity": f"{entity_type}:{entity_id}",
        "status": "reconciled",
        "amount_inr": amount,
        "tds_withheld_inr": tds_val,
        "reconciled_at": utc_now(),
    }


# ---------------------------------------------------------------------------
# 4. CASH RUNWAY & DISTRIBUTABLE CASH SEPARATION
# ---------------------------------------------------------------------------

def get_cash_and_reserves_summary(
    month: str = "",
    working_capital_buffer_inr: float = DEFAULT_WORKING_CAPITAL_BUFFER_INR,
    include_sandbox: bool = False,
) -> dict[str, Any]:
    """
    Computes exact available cash runway and owner distributable profit:
    Distributable Cash C = B - G - T - U - W
    where:
      B = Total reconciled bank cash (None / unknown if opening balance unconfirmed)
      G = GST liability reserve (GST collected - GST paid)
      T = Income tax reserve
      U = Unpaid bills / unreconciled expenses
      W = Working capital buffer reserve (default ₹25,000)
    """
    eff_month = month.strip() if month else utc_now()[:7]
    sandbox_clause = "AND (is_sandbox = 0 OR is_sandbox IS NULL)" if not include_sandbox else ""
    sandbox_where = "WHERE (is_sandbox = 0 OR is_sandbox IS NULL)" if not include_sandbox else ""

    with connect() as conn:
        # Check bank opening balance
        bank_acc = get_bank_opening_balance()
        bank_balance_known = bank_acc is not None
        opening_balance = float(bank_acc["opening_balance_inr"]) if bank_acc else 0.0

        # MTD performance metrics (scoped to eff_month if not 'all')
        if eff_month and eff_month != "all":
            rec_row = conn.execute(
                f"""SELECT COALESCE(SUM(
                       CASE WHEN tx_type = 'client_receipt' THEN amount_inr
                            WHEN tx_type = 'refund' THEN -amount_inr
                            ELSE 0 END
                   ), 0)
                   FROM financial_transactions
                   WHERE tx_type IN ('client_receipt', 'refund') AND tx_date LIKE ? {sandbox_clause}""",
                (f"{eff_month}%",),
            ).fetchone()
            # client_receipt and refund are both recorded excluding GST; refunds
            # therefore reduce realized service receipts, operating profit and taxable profit.
            collected_receipts = float(rec_row[0] if rec_row else 0.0)

            exp_summary = get_expenses_summary(month=eff_month, include_sandbox=include_sandbox)
            total_operating_expenses = exp_summary["total_expenses_inr"]
            deductible_operating_expenses = exp_summary["deductible_expenses_inr"]
        else:
            rec_row = conn.execute(
                f"""SELECT COALESCE(SUM(
                       CASE WHEN tx_type = 'client_receipt' THEN amount_inr
                            WHEN tx_type = 'refund' THEN -amount_inr
                            ELSE 0 END
                   ), 0)
                   FROM financial_transactions
                   WHERE tx_type IN ('client_receipt', 'refund') {sandbox_clause}"""
            ).fetchone()
            collected_receipts = float(rec_row[0] if rec_row else 0.0)

            exp_summary = get_expenses_summary(month="all", include_sandbox=include_sandbox)
            total_operating_expenses = exp_summary["total_expenses_inr"]
            deductible_operating_expenses = exp_summary["deductible_expenses_inr"]

        # Method of accounting under Income-tax Act, 2025 Section 276 (cash vs mercantile)
        tax_prof = get_active_tax_profile()
        tax_settings = validate_tax_profile(tax_prof)
        acc_method = tax_settings.get("accounting_method", "cash")

        if acc_method == "mercantile":
            # Accrual basis: recognize invoiced revenue ex-GST
            if eff_month and eff_month != "all":
                inv_row = conn.execute(
                    f"""SELECT COALESCE(SUM(amount_subtotal), 0) FROM invoices
                       WHERE status <> 'cancelled' AND substr(issued_at, 1, 7) = ? {sandbox_clause}""",
                    (eff_month,),
                ).fetchone()
            else:
                inv_row = conn.execute(
                    f"SELECT COALESCE(SUM(amount_subtotal), 0) FROM invoices WHERE status <> 'cancelled' {sandbox_clause}"
                ).fetchone()
            mercantile_turnover = float(inv_row[0] if inv_row else 0.0)
            operating_profit = round(mercantile_turnover - total_operating_expenses, 2)
            taxable_business_profit = max(0.0, round(mercantile_turnover - deductible_operating_expenses, 2))
        else:
            # Cash basis: recognize revenue upon actual collection of client receipts
            operating_profit = round(collected_receipts - total_operating_expenses, 2)
            taxable_business_profit = max(0.0, round(collected_receipts - deductible_operating_expenses, 2))

        # Lifetime / All ledger transactions for bank cash & reserves
        txs = conn.execute(f"SELECT tx_type, SUM(amount_inr) as total_inr FROM financial_transactions {sandbox_where} GROUP BY tx_type").fetchall()
        tx_sums = {r["tx_type"]: float(r["total_inr"] or 0.0) for r in txs}

        all_collected_receipts = tx_sums.get("client_receipt", 0.0)
        gst_collected = tx_sums.get("gst_collected", 0.0)
        gst_paid = tx_sums.get("gst_paid", 0.0)
        tax_payments = tx_sums.get("tax_payment", 0.0)
        refunds_paid = tx_sums.get("refund", 0.0) + tx_sums.get("gst_refund", 0.0)
        gst_refunds = tx_sums.get("gst_refund", 0.0)
        owner_draws = tx_sums.get("owner_draw", 0.0)
        platform_fees = tx_sums.get("platform_fee", 0.0)
        other_cash_in = tx_sums.get("other_cash_in", 0.0)
        other_cash_out = tx_sums.get("other_cash_out", 0.0)

        # Reconciled paid expenses (expenses that have been matched in bank_statement_rows)
        paid_exp_row = conn.execute(
            f"""SELECT COALESCE(SUM(amount_inr), 0) FROM expenses
               WHERE id IN (
                   SELECT CAST(matched_entity_id AS INTEGER)
                   FROM bank_statement_rows
                   WHERE is_reconciled = 1 AND matched_entity_type = 'expense'
               ) {sandbox_clause}"""
        ).fetchone()
        paid_expenses = float(paid_exp_row[0] if paid_exp_row else 0.0)

        # Unpaid bills (expenses entered that are not yet matched to a bank statement row)
        unpaid_row = conn.execute(
            f"""SELECT COALESCE(SUM(amount_inr), 0) FROM expenses
               WHERE id NOT IN (
                   SELECT CAST(matched_entity_id AS INTEGER)
                   FROM bank_statement_rows
                   WHERE is_reconciled = 1 AND matched_entity_type = 'expense'
               ) {sandbox_clause}"""
        ).fetchone()
        unpaid_bills = round(float(unpaid_row[0] if unpaid_row else 0.0), 2)

        # Bank balance calculation
        if bank_balance_known:
            total_cash_in = all_collected_receipts + gst_collected + other_cash_in
            total_cash_out = paid_expenses + refunds_paid + owner_draws + tax_payments + platform_fees + gst_paid + other_cash_out
            bank_balance = round(opening_balance + total_cash_in - total_cash_out, 2)
        else:
            bank_balance = None

        # Statutory GST reserve: GST collected - GST paid (net liability)
        gst_liability_reserve = max(0.0, round(gst_collected - gst_paid - gst_refunds, 2))

        # Income tax reserve calculation
        tax_prof = get_active_tax_profile()
        tax_settings = validate_tax_profile(tax_prof)
        other_income = float(tax_prof["other_annual_income_inr"]) if tax_prof else 0.0
        advance_tax = float(tax_prof["advance_tax_paid_inr"]) if tax_prof else 0.0
        tds_credits = float(tax_prof["tds_credits_inr"]) if tax_prof else 0.0

        inc_res = compute_business_incremental_tax(
            taxable_business_profit * 12.0,
            other_income,
            regime=tax_settings["regime"],
        )
        annual_tax = inc_res["incremental_business_tax"]
        monthly_tax_provision = round(annual_tax / 12.0, 2)
        total_prepayments = round(advance_tax + tds_credits, 2)
        remaining_annual_tax = max(0.0, round(annual_tax - total_prepayments, 2))
        income_tax_reserve = min(monthly_tax_provision, round(remaining_annual_tax / 12.0, 2))

        buffer_reserve = max(0.0, float(working_capital_buffer_inr))

        if bank_balance_known and bank_balance is not None:
            distributable_cash = round(
                bank_balance - gst_liability_reserve - income_tax_reserve - unpaid_bills - buffer_reserve, 2
            )
            is_solvent = distributable_cash >= 0.0
        else:
            distributable_cash = None
            is_solvent = None

    return {
        "bank_balance_known": bank_balance_known,
        "bank_balance_inr": bank_balance,
        "collected_receipts_inr": round(collected_receipts, 2),
        "total_operating_expenses_inr": round(total_operating_expenses, 2),
        "deductible_operating_expenses_inr": round(deductible_operating_expenses, 2),
        "operating_profit_inr": round(operating_profit, 2),
        "taxable_business_profit_inr": round(taxable_business_profit, 2),
        "gst_liability_reserve_inr": gst_liability_reserve,
        "income_tax_reserve_inr": income_tax_reserve,
        "monthly_tax_provision_inr": monthly_tax_provision,
        "annual_tax_inr": annual_tax,
        "total_prepayments_inr": total_prepayments,
        "remaining_annual_tax_inr": remaining_annual_tax,
        "unpaid_bills_inr": unpaid_bills,
        "working_capital_buffer_inr": buffer_reserve,
        "distributable_cash_inr": distributable_cash,
        "is_solvent": is_solvent,
        "accounting_method": acc_method,
    }
