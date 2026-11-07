from __future__ import annotations

"""
SOLOEARN Financial Foundation & Ledger Engine (M6.0)
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

from core.db import connect, utc_now

DEFAULT_WORKING_CAPITAL_BUFFER_INR = 25000.0

VALID_LEDGER_TX_TYPES = {
    "client_receipt",
    "platform_fee",
    "refund",
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
                reference_id, description, is_reversal, linked_tx_id, created_at
            ) VALUES (?, ?, ?, ?, 'INR', ?, ?, ?, ?, ?)""",
            (
                eff_date,
                tx_type,
                amount_inr,
                paise,
                str(reference_id)[:100],
                str(description)[:255],
                1 if is_reversal else 0,
                linked_tx_id,
                utc_now(),
            ),
        )
        tx_id = int(cur.lastrowid)

    return tx_id


def list_ledger_transactions(limit: int = 100, tx_type: str = "") -> list[dict[str, Any]]:
    """Retrieves recent ledger transactions in reverse chronological order."""
    query = "SELECT * FROM financial_transactions"
    params: list[Any] = []
    if tx_type:
        query += " WHERE tx_type = ?"
        params.append(tx_type)
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
) -> int:
    """
    Registers a business expense, records it in the expenses table,
    and appends an immutable transaction to the financial ledger.
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

    paise = inr_to_paise(amount_inr)
    eff_date = expense_date.strip() if expense_date else utc_now()[:10]

    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO expenses (
                expense_date, vendor, category, amount_inr, amount_paise, currency,
                job_id, is_recurring, business_use_pct, gst_amount_inr,
                tax_deductibility, reviewer_notes, created_at
            ) VALUES (?, ?, ?, ?, ?, 'INR', ?, ?, ?, ?, ?, ?, ?)""",
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
                utc_now(),
            ),
        )
        expense_id = int(cur.lastrowid)

        # Record in ledger
        tx_type = "direct_cost" if category == "direct_project_cost" else "expense"
        conn.execute(
            """INSERT INTO financial_transactions (
                tx_date, tx_type, amount_inr, amount_paise, currency,
                reference_id, description, is_reversal, created_at
            ) VALUES (?, ?, ?, ?, 'INR', ?, ?, 0, ?)""",
            (
                eff_date,
                tx_type,
                amount_inr,
                paise,
                f"EXP-{expense_id:05d}",
                f"{vendor.strip()} ({category})",
                utc_now(),
            ),
        )

        if gst_amount_inr > 0:
            conn.execute(
                """INSERT INTO financial_transactions (
                    tx_date, tx_type, amount_inr, amount_paise, currency,
                    reference_id, description, is_reversal, created_at
                ) VALUES (?, 'gst_paid', ?, ?, 'INR', ?, ?, 0, ?)""",
                (
                    eff_date,
                    gst_amount_inr,
                    inr_to_paise(gst_amount_inr),
                    f"EXP-{expense_id:05d}",
                    f"GST input credit on EXP-{expense_id:05d}",
                    utc_now(),
                ),
            )

    return expense_id


def list_expenses(limit: int = 100, category: str = "", job_id: int | None = None) -> list[dict[str, Any]]:
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

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))

    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_expenses_summary() -> dict[str, Any]:
    """Computes total expenses, categorized breakdown, and deductible sums."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT category, SUM(amount_inr) as total_inr, COUNT(*) as count FROM expenses GROUP BY category"
        ).fetchall()

        by_cat = {r["category"]: round(float(r["total_inr"] or 0.0), 2) for r in rows}
        total_exp = sum(by_cat.values())

        deductible_row = conn.execute(
            """SELECT SUM(amount_inr * (business_use_pct / 100.0)) as total_deductible
               FROM expenses WHERE tax_deductibility IN ('deductible', 'partly_deductible')"""
        ).fetchone()
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
    for idx, col in enumerate(header):
        if "credit" in col or "deposit" in col:
            credit_idx = idx
        elif "debit" in col or "withdrawal" in col:
            debit_idx = idx
        elif "amount" in col:
            amount_idx = idx

    # Detect reference / UTR column
    ref_idx = -1
    for idx, col in enumerate(header):
        if any(k in col for k in ("ref", "utr", "chq", "cheque", "transaction_id", "reference")):
            ref_idx = idx
            break

    parsed_rows: list[dict[str, Any]] = []
    for r in rows[1:]:
        if not r or all(c.strip() == "" for c in r):
            continue

        raw_date = r[date_idx].strip() if len(r) > date_idx else utc_now()[:10]
        raw_desc = r[desc_idx].strip() if len(r) > desc_idx else "Bank entry"
        raw_ref = r[ref_idx].strip() if ref_idx != -1 and len(r) > ref_idx else ""

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
            if "-" in raw_val or "dr" in raw_val.lower():
                tx_type = "debit"
            clean_str = re.sub(r"[^\d.]", "", raw_val)
            amt = float(clean_str) if clean_str else 0.0

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
) -> dict[str, Any]:
    """
    Reconciles a bank statement entry with an invoice or expense.
    Marks the statement row as reconciled and verifies payment with owner attestation.
    """
    if not owner_confirmed:
        raise ValueError("Reconciliation requires explicit owner verification checkbox")

    if entity_type not in {"invoice", "expense", "owner_draw", "tax_payment", "other"}:
        raise ValueError(f"Invalid matched entity type: '{entity_type}'")

    with connect() as conn:
        st_row = conn.execute("SELECT * FROM bank_statement_rows WHERE id = ?", (row_id,)).fetchone()
        if not st_row:
            raise ValueError(f"Bank statement row ID {row_id} not found")

        conn.execute(
            """UPDATE bank_statement_rows
               SET is_reconciled = 1,
                   matched_entity_type = ?,
                   matched_entity_id = ?,
                   confirmed_by_owner = 1,
                   confirmed_at = ?
               WHERE id = ?""",
            (entity_type, str(entity_id), utc_now(), row_id),
        )

        # Update parent import reconciled count
        conn.execute(
            """UPDATE bank_statement_imports
               SET reconciled_count = reconciled_count + 1
               WHERE id = ?""",
            (st_row["import_id"],),
        )

        # If matched to an invoice and the statement row is a credit, mark invoice paid and record in ledger
        if entity_type == "invoice" and st_row["tx_type"] == "credit":
            inv = conn.execute("SELECT * FROM invoices WHERE invoice_number = ?", (entity_id,)).fetchone()
            if inv:
                conn.execute(
                    """UPDATE invoices
                       SET status = 'paid',
                           paid_at = ?,
                           payment_reference = ?,
                           payment_verified_by_owner = 1
                       WHERE invoice_number = ?""",
                    (utc_now(), st_row["bank_ref"] or f"BANK-ROW-{row_id}", entity_id),
                )
                # Append client_receipt to financial_transactions ledger
                paise = inr_to_paise(inv["amount_subtotal"])
                conn.execute(
                    """INSERT INTO financial_transactions (
                        tx_date, tx_type, amount_inr, amount_paise, currency,
                        reference_id, description, is_reversal, created_at
                    ) VALUES (?, 'client_receipt', ?, ?, 'INR', ?, ?, 0, ?)""",
                    (
                        st_row["tx_date"],
                        inv["amount_subtotal"],
                        paise,
                        entity_id,
                        f"Reconciled client receipt for {entity_id}",
                        utc_now(),
                    ),
                )
                if inv["tax_amount"] > 0:
                    conn.execute(
                        """INSERT INTO financial_transactions (
                            tx_date, tx_type, amount_inr, amount_paise, currency,
                            reference_id, description, is_reversal, created_at
                        ) VALUES (?, 'gst_collected', ?, ?, 'INR', ?, ?, 0, ?)""",
                        (
                            st_row["tx_date"],
                            inv["tax_amount"],
                            inr_to_paise(inv["tax_amount"]),
                            entity_id,
                            f"GST collected on {entity_id}",
                            utc_now(),
                        ),
                    )

    return {
        "statement_row_id": row_id,
        "matched_entity": f"{entity_type}:{entity_id}",
        "status": "reconciled",
        "reconciled_at": utc_now(),
    }


# ---------------------------------------------------------------------------
# 4. CASH RUNWAY & DISTRIBUTABLE CASH SEPARATION
# ---------------------------------------------------------------------------

def get_cash_and_reserves_summary(working_capital_buffer_inr: float = DEFAULT_WORKING_CAPITAL_BUFFER_INR) -> dict[str, Any]:
    """
    Computes exact available cash runway and owner distributable profit using:
    Distributable Cash C = B - G - T - U - W
    where:
      B = Total reconciled bank cash (receipts - paid expenses - refunds - draws)
      G = GST liability reserve (GST collected - GST input tax paid)
      T = Income tax reserve (modeled tax provision - advance tax paid)
      U = Unpaid bills / pending refunds
      W = Working capital buffer reserve (default ₹25,000)
    """
    with connect() as conn:
        txs = conn.execute("SELECT tx_type, SUM(amount_inr) as total_inr FROM financial_transactions GROUP BY tx_type").fetchall()
        tx_sums = {r["tx_type"]: float(r["total_inr"] or 0.0) for r in txs}

        collected_receipts = tx_sums.get("client_receipt", 0.0)
        direct_costs = tx_sums.get("direct_cost", 0.0)
        expenses_paid = tx_sums.get("expense", 0.0)
        refunds_paid = tx_sums.get("refund", 0.0)
        platform_fees = tx_sums.get("platform_fee", 0.0)
        owner_draws = tx_sums.get("owner_draw", 0.0)
        gst_collected = tx_sums.get("gst_collected", 0.0)
        gst_paid = tx_sums.get("gst_paid", 0.0)
        tax_payments = tx_sums.get("tax_payment", 0.0)

        # Net cash balance in bank
        total_inflows = collected_receipts + gst_collected
        total_outflows = direct_costs + expenses_paid + refunds_paid + platform_fees + gst_paid + tax_payments + owner_draws
        bank_balance = round(total_inflows - total_outflows, 2)

        # Statutory GST reserve: GST collected - GST paid (net liability)
        gst_liability_reserve = max(0.0, round(gst_collected - gst_paid, 2))

        # Income tax reserve calculation
        # Operating profit = collected_receipts - (direct_costs + expenses_paid + platform_fees + refunds_paid)
        operating_profit = max(0.0, collected_receipts - (direct_costs + expenses_paid + platform_fees + refunds_paid))
        from core.tax_engine import compute_income_tax_india_2026
        # Annualized estimate for reserve:
        tax_res = compute_income_tax_india_2026(operating_profit * 12.0)
        annual_tax = tax_res["total_tax"]
        monthly_tax_provision = round(annual_tax / 12.0, 2)
        income_tax_reserve = max(0.0, round(monthly_tax_provision - tax_payments, 2))

        # Unpaid bills: expenses entered but not marked as reconciled to bank rows
        unpaid_row = conn.execute(
            """SELECT SUM(amount_inr) as total_unpaid FROM expenses
               WHERE id NOT IN (
                   SELECT CAST(matched_entity_id AS INTEGER)
                   FROM bank_statement_rows
                   WHERE is_reconciled = 1 AND matched_entity_type = 'expense'
               )"""
        ).fetchone()
        unpaid_bills = round(float(unpaid_row["total_unpaid"] or 0.0), 2) if unpaid_row else 0.0

        buffer_reserve = max(0.0, float(working_capital_buffer_inr))

        # Available Distributable Cash C = B - G - T - U - W
        distributable_cash = round(
            bank_balance - gst_liability_reserve - income_tax_reserve - unpaid_bills - buffer_reserve, 2
        )

    return {
        "bank_balance_inr": bank_balance,
        "collected_receipts_inr": round(collected_receipts, 2),
        "total_operating_expenses_inr": round(direct_costs + expenses_paid + platform_fees + refunds_paid, 2),
        "operating_profit_inr": round(operating_profit, 2),
        "gst_liability_reserve_inr": gst_liability_reserve,
        "income_tax_reserve_inr": income_tax_reserve,
        "unpaid_bills_inr": unpaid_bills,
        "working_capital_buffer_inr": buffer_reserve,
        "distributable_cash_inr": distributable_cash,
        "is_solvent": distributable_cash >= 0.0,
    }
