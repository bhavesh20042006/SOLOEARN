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
    "customer_advance",           # Customer advance received (unearned liability)
    "customer_advance_applied",   # Advance converted to earned client receipt upon work acceptance
    "customer_advance_refunded",  # Advance refunded to client
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


def record_owner_draw(
    amount_inr: float,
    description: str = "Owner personal withdrawal",
    reference_id: str = "",
    tx_date: str = "",
    is_sandbox: bool = False,
) -> int:
    """
    Records an owner capital withdrawal.
    CRITICAL: Under Indian tax law, proprietor draws are NOT deductible business expenses.
    They reduce available bank cash and distributable cash, but do NOT reduce operating profit
    or taxable business profit.
    """
    return record_ledger_transaction(
        tx_type="owner_draw",
        amount_inr=amount_inr,
        description=description,
        reference_id=reference_id,
        tx_date=tx_date,
        is_sandbox=is_sandbox,
    )


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

        subtotal_paise = inr_to_paise(float(inv["amount_subtotal"]))
        gst_amount_paise = inr_to_paise(float(inv["tax_amount"]))
        gross_total_paise = inr_to_paise(float(inv["amount_total"]))
        if gross_total_paise <= 0:
            raise ValueError("Invoice total must be positive")

        prior = conn.execute(
            """SELECT tx_type, COALESCE(SUM(amount_paise), 0) AS total_paise
               FROM financial_transactions
               WHERE reference_id LIKE ? AND tx_type IN ('refund','gst_refund')
               GROUP BY tx_type""",
            (f"REFUND:{invoice_number.strip()}:%",),
        ).fetchall()
        prior_by_type = {r["tx_type"]: int(r["total_paise"] or 0) for r in prior}
        prior_ex_paise = prior_by_type.get("refund", 0)
        prior_gst_paise = prior_by_type.get("gst_refund", 0)
        prior_gross_paise = prior_ex_paise + prior_gst_paise

        gross_refund_paise = inr_to_paise(gross_refund)

        ref_id = f"REFUND:{invoice_number.strip()}:{refund_reference.strip()}"
        dupe = conn.execute(
            "SELECT id FROM financial_transactions WHERE reference_id=? AND tx_type IN ('refund','gst_refund') LIMIT 1",
            (ref_id,),
        ).fetchone()
        if dupe:
            raise ValueError("This refund reference has already been recorded")

        if prior_gross_paise + gross_refund_paise > gross_total_paise:
            raise ValueError("Cumulative refunds cannot exceed the original invoice total")

        new_cumulative_gross_paise = prior_gross_paise + gross_refund_paise
        new_cumulative_ex_paise = min(subtotal_paise, int(round(new_cumulative_gross_paise * subtotal_paise / gross_total_paise)))
        refund_ex_paise = new_cumulative_ex_paise - prior_ex_paise
        refund_gst_paise = gross_refund_paise - refund_ex_paise
        if refund_ex_paise < 0 or refund_gst_paise < 0 or refund_gst_paise > (gst_amount_paise - prior_gst_paise):
            raise ValueError("Refund tax allocation failed validation")

        refund_ex = paise_to_inr(refund_ex_paise)
        refund_gst = paise_to_inr(refund_gst_paise)
        gross_refund = paise_to_inr(gross_refund_paise)

        now = utc_now()
        is_sb = int(inv["is_sandbox"] or 0) if "is_sandbox" in inv.keys() else 0
        if refund_ex_paise > 0:
            conn.execute(
                """INSERT INTO financial_transactions (
                    tx_date, tx_type, amount_inr, amount_paise, currency,
                    reference_id, description, is_reversal, is_sandbox, created_at
                ) VALUES (?, 'refund', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                (
                    eff_date, refund_ex, refund_ex_paise, ref_id,
                    f"Ex-GST client refund for {invoice_number.strip()}", is_sb, now,
                ),
            )
        if refund_gst_paise > 0:
            conn.execute(
                """INSERT INTO financial_transactions (
                    tx_date, tx_type, amount_inr, amount_paise, currency,
                    reference_id, description, is_reversal, is_sandbox, created_at
                ) VALUES (?, 'gst_refund', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                (
                    eff_date, refund_gst, refund_gst_paise, ref_id,
                    f"GST component of client refund for {invoice_number.strip()}", is_sb, now,
                ),
            )

    return {
        "invoice_number": invoice_number.strip(),
        "refund_reference": refund_reference.strip(),
        "gross_refund_inr": gross_refund,
        "gross_refund_paise": gross_refund_paise,
        "service_refund_ex_gst_inr": refund_ex,
        "refund_subtotal_paise": refund_ex_paise,
        "gst_refund_inr": refund_gst,
        "refund_tax_paise": refund_gst_paise,
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
# 1B. CUSTOMER ADVANCE LIABILITY & UNEARNED REVENUE ACCOUNTING
# ---------------------------------------------------------------------------

def record_customer_advance(
    amount_inr: float,
    client_name: str,
    reference_id: str = "",
    description: str = "",
    tx_date: str = "",
    is_sandbox: bool = False,
) -> int:
    """
    Records an advance payment received from a prospective or active client.
    CRITICAL: Customer advances are unearned liabilities / obligations under Indian accounting.
    They increase reconciled bank cash once cleared, but create an equal unearned liability reserve.
    Customer advances are strictly NOT recognized as earned turnover, operating profit, or distributable profit
    until services are fully delivered, invoiced, and accepted by the client.
    """
    desc = description or f"Customer advance from {client_name}"
    return record_ledger_transaction(
        tx_type="customer_advance",
        amount_inr=amount_inr,
        description=desc,
        reference_id=reference_id,
        tx_date=tx_date,
        is_sandbox=is_sandbox,
    )


def apply_customer_advance(
    advance_tx_id: int,
    invoice_number: str,
    amount_inr: float,
    description: str = "",
    tx_date: str = "",
    is_sandbox: bool = False,
) -> int:
    """
    Applies a previously received customer advance towards a completed, accepted invoice.
    Reduces the unearned customer advance liability and recognizes earned client_receipt revenue.
    Atomically updates invoice amount_paid and status to prevent double-billing.
    """
    if amount_inr <= 0 or not math.isfinite(amount_inr):
        raise ValueError("Applied advance amount must be positive and finite")

    desc = description or f"Applied customer advance towards invoice {invoice_number}"
    applied_tx_id = record_ledger_transaction(
        tx_type="customer_advance_applied",
        amount_inr=amount_inr,
        description=desc,
        reference_id=invoice_number,
        tx_date=tx_date,
        linked_tx_id=advance_tx_id,
        is_sandbox=is_sandbox,
    )
    record_ledger_transaction(
        tx_type="client_receipt",
        amount_inr=amount_inr,
        description=f"Earned revenue from applied advance: {invoice_number}",
        reference_id=invoice_number,
        tx_date=tx_date,
        linked_tx_id=applied_tx_id,
        is_sandbox=is_sandbox,
    )

    # Synchronize invoice record so outstanding balance does not double-count
    with connect() as conn:
        inv = conn.execute(
            "SELECT id, amount_total, amount_paid FROM invoices WHERE invoice_number = ?",
            (invoice_number,),
        ).fetchone()
        if inv:
            current_paid = float(inv["amount_paid"] or 0.0)
            total_due = float(inv["amount_total"] or 0.0)
            new_paid = round(current_paid + amount_inr, 2)
            new_status = "paid" if new_paid >= total_due - 0.01 else "payment_pending"
            conn.execute(
                "UPDATE invoices SET amount_paid = ?, status = ? WHERE invoice_number = ?",
                (new_paid, new_status, invoice_number),
            )
            conn.commit()

    return applied_tx_id


def refund_customer_advance(
    advance_tx_id: int,
    amount_inr: float,
    reason: str,
    reference_id: str = "",
    tx_date: str = "",
    owner_confirmed: bool = False,
    is_sandbox: bool = False,
) -> int:
    """
    Refunds an unearned customer advance back to the client.
    Requires explicit human owner confirmation under Approval Gate 4.
    """
    if not owner_confirmed:
        raise ValueError("Advance refund requires explicit owner_confirmed=True under Gate 4")
    desc = f"Refund of customer advance: {reason}"
    return record_ledger_transaction(
        tx_type="customer_advance_refunded",
        amount_inr=amount_inr,
        description=desc,
        reference_id=reference_id,
        tx_date=tx_date,
        linked_tx_id=advance_tx_id,
        is_sandbox=is_sandbox,
    )


def get_customer_advance_liability(include_sandbox: bool = False) -> float:
    """
    Returns the net outstanding unearned customer advance liability in INR.
    Liability = customer_advance - customer_advance_applied - customer_advance_refunded
    """
    where_clauses = ["tx_type IN ('customer_advance', 'customer_advance_applied', 'customer_advance_refunded')"]
    if not include_sandbox:
        where_clauses.append("(is_sandbox = 0 OR is_sandbox IS NULL)")
    where_sql = "WHERE " + " AND ".join(where_clauses)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT tx_type, SUM(amount_inr) FROM financial_transactions
               {where_sql}
               GROUP BY tx_type"""
        ).fetchall()
        sums = {r[0]: float(r[1] or 0.0) for r in rows}
        adv_in = sums.get("customer_advance", 0.0)
        adv_app = sums.get("customer_advance_applied", 0.0)
        adv_ref = sums.get("customer_advance_refunded", 0.0)
        return max(0.0, round(adv_in - adv_app - adv_ref, 2))


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

def parse_bank_narration(narration: str) -> dict[str, Any]:
    """
    Audits and extracts critical financial settlement metadata from diverse Indian bank statement narrations
    (specifically tailored for Kotak Mahindra Bank Safalta Current / Savings accounts, HDFC, ICICI, SBI).
    Accurately identifies clearing channels and reference numbers across:
    1. UPI (Unified Payments Interface): 12-digit RRN / alphanumeric reference.
       Examples:
         - 'UPI/432199887766/CR/CLIENT_A/KOTAK/INV-2026-0001'
         - 'UPI-432199887766-CLIENT_A-KOTAK-INV-2026-0001'
         - 'UPI/CR/432199887766/CLIENT@OKAXIS/Payment'
         - 'UPI/P2A/432199887766/REMARKS'
         - 'UPI/P2M/432199887766/MERCHANT'
    2. NEFT (National Electronic Funds Transfer): 10-22 character UTR / transaction identifier.
       Examples:
         - 'NEFT CR-KKBKH26123456789-CLIENT_A-INV-2026-0001'
         - 'NEFT/KKBKH26123456789/CLIENT_A/INV-2026-0001'
         - 'NEFT-N123260123456789-CLIENT_A'
         - 'NEFT CR - KKBKH26123456789 - SENDER'
    3. RTGS (Real Time Gross Settlement): 16-22 character RBI standard UTR.
       Examples:
         - 'RTGS CR-KKBKR52026111200123456-CLIENT_A-INV-2026-0001'
         - 'RTGS/KKBKR52026111200123456/CLIENT_A'
         - 'RTGS-KKBKR52026111200123456-CLIENT'
    4. IMPS (Immediate Payment Service): 10-16 alphanumeric or 12-digit numeric RRN.
       Examples:
         - 'IMPS/P2A/432199887766/CLIENT_A/INV-2026-0001'
         - 'IMPS/432199887766/CLIENT_A'
         - 'IMPS-432199887766-CLIENT_A'
         - 'IMPS CR-432199887766-CLIENT_A'
    5. ACH / NACH / FT / INB:
       Examples:
         - 'ACH/CR/...'
         - 'FT - REF12345 - NAME'

    Returns:
      dict with:
        - channel: 'UPI' | 'NEFT' | 'RTGS' | 'IMPS' | 'ACH' | 'INTERNAL_FT' | 'OTHER'
        - reference_id: Extracted UTR / RRN
        - counterparty: Extracted counterparty name or VPA if identified
        - invoice_reference: Extracted invoice number (e.g. 'INV-2026-0001') if embedded
        - direction: 'credit' | 'debit' | 'reversal' | 'unknown'
        - is_reversal: bool
    """
    raw = str(narration or "").strip()
    upper = raw.upper()

    is_reversal = bool(re.search(r"\b(REVERSAL|REV|RETURN|REVERSED)\b", upper))
    if is_reversal:
        direction = "reversal"
    elif re.search(r"\b(?:CR|CREDIT|DEPOSIT|P2A|P2M)\b", upper):
        direction = "credit"
    elif re.search(r"\b(?:DR|DEBIT|WITHDRAWAL|WDL)\b", upper):
        direction = "debit"
    else:
        direction = "unknown"

    channel = "OTHER"
    ref_id = ""

    # 1. UPI check
    m_upi = re.search(r"\bUPI[/_\s-]+(?:(?:CR|DR|P2A|P2M|P2P)[/_\s-]+)?([A-Za-z0-9]{10,16})\b", raw, re.IGNORECASE)
    if not m_upi:
        m_upi = re.search(r"\bUPI[/_\s-]+([A-Za-z0-9]{10,16})(?:[/_\s-]+(?:CR|DR))?\b", raw, re.IGNORECASE)
    if m_upi:
        channel = "UPI"
        ref_id = m_upi.group(1)

    # 2. RTGS check (16-22 chars)
    if not ref_id:
        m_rtgs = re.search(r"\bRTGS(?:[\s]+CR|[\s]+DR)?[/_\s-]+([A-Za-z0-9]{16,22})\b", raw, re.IGNORECASE)
        if not m_rtgs:
            m_rtgs = re.search(r"\b([A-Z]{4}R[A-Za-z0-9]{11,17})\b", raw)
        if m_rtgs:
            channel = "RTGS"
            ref_id = m_rtgs.group(1)

    # 3. NEFT check (10-22 chars)
    if not ref_id:
        m_neft = re.search(r"\bNEFT(?:[\s]+CR|[\s]+DR)?[/_\s-]+([A-Za-z0-9]{10,22})\b", raw, re.IGNORECASE)
        if not m_neft:
            m_neft = re.search(r"\b([A-Z]{4}N[A-Za-z0-9]{10,17})\b", raw)
        if m_neft:
            channel = "NEFT"
            ref_id = m_neft.group(1)

    # 4. IMPS check (10-16 alphanumeric or 12-digit numeric)
    if not ref_id:
        m_imps = re.search(r"\bIMPS(?:[\s]+CR|[\s]+DR)?[/_\s-]+(?:(?:P2A|P2U|P2P|CR|DR)[/_\s-]+)?([A-Za-z0-9]{10,16})\b", raw, re.IGNORECASE)
        if not m_imps:
            m_imps = re.search(r"\bIMPS[/_\s-]+([A-Za-z0-9]{10,16})\b", raw, re.IGNORECASE)
        if m_imps:
            channel = "IMPS"
            ref_id = m_imps.group(1)

    # 5. ACH / NACH
    if not ref_id:
        m_ach = re.search(r"\b(?:ACH|NACH)(?:[\s]+CR|[\s]+DR)?[/_\s-]+([A-Za-z0-9]{8,22})\b", raw, re.IGNORECASE)
        if m_ach:
            channel = "ACH"
            ref_id = m_ach.group(1)

    # 6. Internal Fund Transfer
    if not ref_id:
        m_ft = re.search(r"\b(?:FT|IFT|INB|MB)(?:[\s]+CR|[\s]+DR)?[/_\s-]+([A-Za-z0-9]{8,20})\b", raw, re.IGNORECASE)
        if m_ft:
            channel = "INTERNAL_FT"
            ref_id = m_ft.group(1)

    # 7. Fallback: Standard 12-digit RRN
    if not ref_id:
        m_rrn = re.search(r"\b(\d{12})\b", raw)
        if m_rrn:
            ref_id = m_rrn.group(1)
            channel = "RRN_12"

    # Extract invoice reference token if embedded
    m_inv = re.search(r"\b(INV[-_]?[0-9]{4}[-_]?[0-9]{2,6}|INV[-_]?[A-Za-z0-9]+)\b", raw, re.IGNORECASE)
    inv_ref = m_inv.group(1) if m_inv else ""

    # Extract counterparty if present (tokens separated by slash or dash)
    counterparty = ""
    parts = [p.strip() for p in re.split(r"[/_-]", raw) if p.strip()]
    ignore_tokens = {
        "UPI", "NEFT", "RTGS", "IMPS", "ACH", "NACH", "CR", "DR", "P2A", "P2M", "P2P", "P2U",
        "KOTAK", "HDFC", "ICICI", "SBI", "AXIS", "YESB", "PAYMENT", "TRANSFER", "CASH", "WDL", "ATM"
    }

    def _is_noise_token(tok: str) -> bool:
        t = tok.upper().strip()
        if t in ignore_tokens:
            return True
        words = t.split()
        if words and all(w in ignore_tokens for w in words):
            return True
        return False

    for p in parts:
        p_up = p.upper()
        if not _is_noise_token(p_up) and p != ref_id and p != inv_ref and len(p) >= 3 and not p.isdigit():
            counterparty = p
            break

    return {
        "channel": channel,
        "reference_id": ref_id,
        "utr_rrn": ref_id,
        "counterparty": counterparty,
        "invoice_reference": inv_ref,
        "inferred_invoice_ref": inv_ref,
        "direction": direction,
        "is_reversal": is_reversal,
    }


def import_bank_statement_csv(file_content: str | bytes, filename: str, notes: str = "") -> dict[str, Any]:
    """
    Imports a standard bank statement CSV into bank_statement_imports and bank_statement_rows.
    Normalizes column names: Date, Description/Narration, Amount/Credit/Debit, Ref/UTR.
    Utilizes parse_bank_narration to extract UTR/RRN across UPI, NEFT, RTGS, IMPS if column is blank.
    Enforces exact integer paise arithmetic on imported monetary values.
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

        # Extract UTR / RRN from narration if raw_ref column is absent or placeholder
        is_placeholder_ref = not raw_ref or raw_ref.strip().upper() in {"", "-", "NA", "N/A", "N.A.", "0", "NONE", "NULL"}
        parsed_meta = parse_bank_narration(raw_desc)
        if is_placeholder_ref and parsed_meta["reference_id"]:
            raw_ref = parsed_meta["reference_id"]
        elif not raw_ref:
            # Fallback regex search
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
            amt_p = inr_to_paise(amt)
            parsed_rows.append(
                {
                    "date": raw_date[:20],
                    "desc": raw_desc[:250],
                    "amount": paise_to_inr(amt_p),
                    "amount_paise": amt_p,
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
                    pr["amount_paise"],
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

    allowed_entities = {"invoice", "expense", "owner_draw", "tax_payment", "gst_payment", "other", "customer_advance"}
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

        raw_tx_type = str(st_row["tx_type"]).lower().strip()
        if raw_tx_type in {"credit", "cr"}:
            tx_direction = "credit"
        elif raw_tx_type in {"debit", "dr"}:
            tx_direction = "debit"
        else:
            tx_direction = raw_tx_type
        amount = round(float(st_row["amount_inr"]), 2)
        if amount <= 0 or not math.isfinite(amount):
            raise ValueError("Bank statement amount must be positive and finite")

        inv = None
        exp = None
        bank_ref = str(st_row["bank_ref"] or "").strip()[:120]

        desc = str(st_row["description"] or "").upper()
        if "REVERSAL" in desc and entity_type in {"invoice", "customer_advance"}:
            raise ValueError("Bank statement row is a reversal transaction and cannot be settled as revenue or customer advance")

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
            amount_paise = inr_to_paise(amount)
            tds_paise = inr_to_paise(tds_val)
            settled_paise = amount_paise + tds_paise

            current_paid_paise = inr_to_paise(float(inv["amount_paid"] or 0.0))
            total_inv_paise = inr_to_paise(float(inv["amount_total"]))
            expected_remaining_paise = total_inv_paise - current_paid_paise

            if settled_paise != expected_remaining_paise:
                raise ValueError(
                    f"Bank credit ₹{amount:,.2f} + TDS withheld ₹{tds_val:,.2f} (Total: ₹{paise_to_inr(settled_paise):,.2f}) "
                    f"does not exactly settle invoice total (remaining balance ₹{paise_to_inr(expected_remaining_paise):,.2f}, "
                    f"total ₹{float(inv['amount_total']):,.2f}, already paid ₹{paise_to_inr(current_paid_paise):,.2f}). "
                    "Partial/over-payments are not supported yet; leave this row unreconciled."
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
            expected_paise = inr_to_paise(float(exp["amount_inr"]))
            debit_paise = inr_to_paise(amount)
            if debit_paise != expected_paise:
                raise ValueError(
                    f"Bank debit ₹{amount:,.2f} does not exactly settle expense "
                    f"₹{paise_to_inr(expected_paise):,.2f}. Partial expense payments are not supported yet; "
                    "leave this row unreconciled."
                )

        elif entity_type in {"owner_draw", "tax_payment", "gst_payment"}:
            if tx_direction != "debit":
                raise ValueError(f"{entity_type} can only be reconciled against a bank debit")

        elif entity_type == "customer_advance":
            if tx_direction != "credit":
                raise ValueError("A customer advance can only be reconciled against a bank credit")

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
                   SET status='paid', paid_at=?, payment_reference=?, payment_verified_by_owner=1, amount_paid=?
                   WHERE id=? AND status IN ('issued','payment_pending')""",
                (now, ref_for_row, float(inv["amount_total"]), inv_id),
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

            # Direct revenue and tax settled in this specific statement row (including client TDS)
            inv_sub_paise = inr_to_paise(float(inv["amount_subtotal"] or 0.0))
            inv_tax_paise = inr_to_paise(float(inv["tax_amount"] or 0.0))
            if total_inv_paise > 0:
                if settled_paise == total_inv_paise:
                    credit_sub_paise = inv_sub_paise
                    credit_tax_paise = inv_tax_paise
                else:
                    credit_tax_paise = int(round(settled_paise * inv_tax_paise / total_inv_paise))
                    credit_sub_paise = settled_paise - credit_tax_paise
            else:
                credit_sub_paise = settled_paise
                credit_tax_paise = 0
            credit_subtotal = paise_to_inr(credit_sub_paise)
            credit_tax = paise_to_inr(credit_tax_paise)

            existing_tx = conn.execute(
                "SELECT id FROM financial_transactions WHERE reference_id=? AND description LIKE ? AND tx_type='client_receipt'",
                (inv_num, f"%Statement row #{row_id}%"),
            ).fetchone()
            if not existing_tx:
                conn.execute(
                    """INSERT INTO financial_transactions (
                        tx_date, tx_type, amount_inr, amount_paise, currency,
                        reference_id, description, is_reversal, is_sandbox, created_at
                    ) VALUES (?, 'client_receipt', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                    (
                        str(st_row["tx_date"])[:10], credit_subtotal, credit_sub_paise,
                        inv_num, f"Reconciled client receipt for {inv_num} ({inv['client_name']}) - Statement row #{row_id}", is_sb, now,
                    ),
                )
                if credit_tax_paise > 0:
                    conn.execute(
                        """INSERT INTO financial_transactions (
                            tx_date, tx_type, amount_inr, amount_paise, currency,
                            reference_id, description, is_reversal, is_sandbox, created_at
                        ) VALUES (?, 'gst_collected', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                        (
                            str(st_row["tx_date"])[:10], credit_tax, credit_tax_paise,
                            inv_num, f"GST collected on {inv_num} - Statement row #{row_id}", is_sb, now,
                        ),
                    )
                if tds_val > 0:
                    tds_paise = inr_to_paise(tds_val)
                    conn.execute(
                        """INSERT INTO financial_transactions (
                            tx_date, tx_type, amount_inr, amount_paise, currency,
                            reference_id, description, is_reversal, is_sandbox, created_at
                        ) VALUES (?, 'tds_credit', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                        (
                            str(st_row["tx_date"])[:10], tds_val, tds_paise,
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

        elif entity_type == "customer_advance":
            existing = conn.execute(
                "SELECT id FROM financial_transactions WHERE reference_id=? AND tx_type='customer_advance'",
                (ref_for_row,),
            ).fetchone()
            if not existing:
                conn.execute(
                    """INSERT INTO financial_transactions (
                        tx_date, tx_type, amount_inr, amount_paise, currency,
                        reference_id, description, is_reversal, is_sandbox, created_at
                    ) VALUES (?, 'customer_advance', ?, ?, 'INR', ?, ?, 0, 0, ?)""",
                    (
                        str(st_row["tx_date"])[:10], amount, inr_to_paise(amount),
                        ref_for_row, f"Reconciled customer advance: {st_row['description']}", now,
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
        txs = conn.execute(
            f"""SELECT tx_type, 
                       COALESCE(SUM(amount_paise), CAST(ROUND(SUM(amount_inr) * 100) AS INTEGER)) as total_paise
                FROM financial_transactions {sandbox_where} 
                GROUP BY tx_type"""
        ).fetchall()
        tx_sums_paise = {r["tx_type"]: int(r["total_paise"] or 0) for r in txs}
        tx_sums = {r["tx_type"]: paise_to_inr(tx_sums_paise.get(r["tx_type"], 0)) for r in txs}

        all_collected_receipts_paise = tx_sums_paise.get("client_receipt", 0)
        customer_advance_in_paise = tx_sums_paise.get("customer_advance", 0)
        customer_advance_applied_paise = tx_sums_paise.get("customer_advance_applied", 0)
        customer_advance_refunded_paise = tx_sums_paise.get("customer_advance_refunded", 0)
        customer_advance_liability_paise = max(0, customer_advance_in_paise - customer_advance_applied_paise - customer_advance_refunded_paise)
        customer_advance_liability = paise_to_inr(customer_advance_liability_paise)

        gst_collected_paise = tx_sums_paise.get("gst_collected", 0)
        gst_paid_paise = tx_sums_paise.get("gst_paid", 0)
        gst_refunds_paise = tx_sums_paise.get("gst_refund", 0)
        tax_payments_paise = tx_sums_paise.get("tax_payment", 0)
        refunds_paid_paise = tx_sums_paise.get("refund", 0) + gst_refunds_paise + customer_advance_refunded_paise
        owner_draws_paise = tx_sums_paise.get("owner_draw", 0)
        platform_fees_paise = tx_sums_paise.get("platform_fee", 0)
        other_cash_in_paise = tx_sums_paise.get("other_cash_in", 0)
        other_cash_out_paise = tx_sums_paise.get("other_cash_out", 0)

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
        paid_expenses_paise = inr_to_paise(paid_expenses)

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
        unpaid_bills_paise = inr_to_paise(unpaid_bills)

        # Bank balance calculation (prevent double-counting applied advances and withholdings)
        if bank_balance_known:
            opening_balance_paise = inr_to_paise(opening_balance)
            tds_credits_ledger_paise = tx_sums_paise.get("tds_credit", 0)
            bank_cash_receipts_paise = max(0, all_collected_receipts_paise - tds_credits_ledger_paise)
            unapplied_advance_paise = max(0, customer_advance_in_paise - customer_advance_applied_paise)
            total_cash_in_paise = bank_cash_receipts_paise + unapplied_advance_paise + gst_collected_paise + other_cash_in_paise
            total_cash_out_paise = (
                paid_expenses_paise + refunds_paid_paise + owner_draws_paise +
                tax_payments_paise + platform_fees_paise + gst_paid_paise + other_cash_out_paise
            )
            bank_balance_paise = opening_balance_paise + total_cash_in_paise - total_cash_out_paise

            bank_balance = paise_to_inr(bank_balance_paise)
            tds_credits_ledger = paise_to_inr(tds_credits_ledger_paise)
            bank_cash_receipts = paise_to_inr(bank_cash_receipts_paise)
            unapplied_advance = paise_to_inr(unapplied_advance_paise)
            total_cash_in = paise_to_inr(total_cash_in_paise)
            total_cash_out = paise_to_inr(total_cash_out_paise)
        else:
            bank_balance = None
            bank_balance_paise = None
            tds_credits_ledger = paise_to_inr(tx_sums_paise.get("tds_credit", 0))
            bank_cash_receipts = None
            unapplied_advance = None
            total_cash_in = None
            total_cash_out = None

        # Statutory GST reserve: GST collected - GST paid (net liability)
        gst_liability_reserve_paise = max(0, gst_collected_paise - gst_paid_paise - gst_refunds_paise)
        gst_liability_reserve = paise_to_inr(gst_liability_reserve_paise)

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
        income_tax_reserve_paise = inr_to_paise(income_tax_reserve)

        buffer_reserve = max(0.0, float(working_capital_buffer_inr))
        buffer_reserve_paise = inr_to_paise(buffer_reserve)

        if bank_balance_known and bank_balance_paise is not None:
            distributable_cash_paise = (
                bank_balance_paise
                - gst_liability_reserve_paise
                - income_tax_reserve_paise
                - unpaid_bills_paise
                - buffer_reserve_paise
                - customer_advance_liability_paise
            )
            distributable_cash = paise_to_inr(distributable_cash_paise)
            is_solvent = distributable_cash_paise >= 0
        else:
            distributable_cash = None
            is_solvent = None

    return {
        "bank_balance_known": bank_balance_known,
        "bank_balance_inr": bank_balance,
        "customer_advance_liability_inr": customer_advance_liability,
        "unapplied_advance_inr": unapplied_advance,
        "collected_receipts_inr": round(collected_receipts, 2),
        "bank_cash_receipts_inr": bank_cash_receipts,
        "tds_receivable_inr": round(tds_credits_ledger, 2),
        "total_cash_in_inr": total_cash_in,
        "total_cash_out_inr": total_cash_out,
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


# ---------------------------------------------------------------------------
# 5. DOUBLE-ENTRY TRIAL BALANCE & LEDGER INTEGRITY AUDIT
# ---------------------------------------------------------------------------

def generate_double_entry_trial_balance(include_sandbox: bool = False) -> dict[str, Any]:
    """
    Constructs a verified double-entry trial balance from the append-only ledger:
    Enforces the fundamental accounting equation:
        Assets = Liabilities + Equity + (Revenue - Expenses)
        Total Debits == Total Credits (0 paise imbalance)

    Chart of Accounts:
      - 1010_bank_cash: Reconciled bank balance (Asset, Dr)
      - 1020_tds_receivable: Section 194J TDS withheld by clients (Asset, Dr)
      - 2010_gst_payable: Output GST collected minus GST paid (Liability, Cr)
      - 2020_customer_advances: Client advance deposits held (Liability, Cr)
      - 3010_owner_capital: Initial capital / confirmed opening balance (Equity, Cr)
      - 3020_owner_drawings: Personal drawings from business (Equity Contra, Dr)
      - 4010_service_revenue: Billable freelance service receipts (Revenue, Cr)
      - 4020_other_income: Miscellaneous non-service inflows (Revenue, Cr)
      - 5010_operating_expenses: Business operating expenses (Expense, Dr)
      - 5020_direct_project_costs: Direct freelance subcontract costs (Expense, Dr)
      - 5030_platform_fees: Bank / payment gateway fees (Expense, Dr)
      - 5040_tax_expense: Advance tax & statutory income tax paid (Expense, Dr)
      - 5050_other_expenses: Other miscellaneous expenses (Expense, Dr)
    """
    sandbox_clause = "" if include_sandbox else "WHERE (is_sandbox = 0 OR is_sandbox IS NULL)"

    # Initialize chart of accounts with 0 debits and credits in paise
    accounts = {
        "1010_bank_cash": {"name": "Bank Cash Account", "type": "asset", "normal_balance": "debit", "debit_paise": 0, "credit_paise": 0},
        "1020_tds_receivable": {"name": "TDS Receivable (Tax Asset)", "type": "asset", "normal_balance": "debit", "debit_paise": 0, "credit_paise": 0},
        "2010_gst_payable": {"name": "GST Output Liability / ITC", "type": "liability", "normal_balance": "credit", "debit_paise": 0, "credit_paise": 0},
        "2020_customer_advances": {"name": "Customer Advance Liability", "type": "liability", "normal_balance": "credit", "debit_paise": 0, "credit_paise": 0},
        "3010_owner_capital": {"name": "Owner's Capital (Opening Balance)", "type": "equity", "normal_balance": "credit", "debit_paise": 0, "credit_paise": 0},
        "3020_owner_drawings": {"name": "Owner's Drawings", "type": "equity", "normal_balance": "debit", "debit_paise": 0, "credit_paise": 0},
        "4010_service_revenue": {"name": "Freelance Service Revenue", "type": "revenue", "normal_balance": "credit", "debit_paise": 0, "credit_paise": 0},
        "4020_other_income": {"name": "Other Miscellaneous Income", "type": "revenue", "normal_balance": "credit", "debit_paise": 0, "credit_paise": 0},
        "5010_operating_expenses": {"name": "Operating Expenses", "type": "expense", "normal_balance": "debit", "debit_paise": 0, "credit_paise": 0},
        "5020_direct_project_costs": {"name": "Direct Project Costs", "type": "expense", "normal_balance": "debit", "debit_paise": 0, "credit_paise": 0},
        "5030_platform_fees": {"name": "Platform & Gateway Fees", "type": "expense", "normal_balance": "debit", "debit_paise": 0, "credit_paise": 0},
        "5040_tax_expense": {"name": "Income Tax & Advance Tax", "type": "expense", "normal_balance": "debit", "debit_paise": 0, "credit_paise": 0},
        "5050_other_expenses": {"name": "Other Expenses", "type": "expense", "normal_balance": "debit", "debit_paise": 0, "credit_paise": 0},
    }

    with connect() as conn:
        # Opening bank balance post to Bank Cash (Dr) and Owner's Capital (Cr)
        bank_acc = get_bank_opening_balance()
        if bank_acc and bank_acc["is_confirmed"]:
            opening_paise = inr_to_paise(float(bank_acc["opening_balance_inr"] or 0.0))
            if opening_paise > 0:
                accounts["1010_bank_cash"]["debit_paise"] += opening_paise
                accounts["3010_owner_capital"]["credit_paise"] += opening_paise

        rows = conn.execute(
            f"""SELECT tx_type, 
                       COALESCE(amount_paise, CAST(ROUND(amount_inr * 100) AS INTEGER)) as amt_paise
                FROM financial_transactions {sandbox_clause}"""
        ).fetchall()

        for r in rows:
            tt = str(r["tx_type"])
            p = int(r["amt_paise"] or 0)
            if p <= 0:
                continue

            if tt == "client_receipt":
                accounts["1010_bank_cash"]["debit_paise"] += p
                accounts["4010_service_revenue"]["credit_paise"] += p
            elif tt == "gst_collected":
                accounts["1010_bank_cash"]["debit_paise"] += p
                accounts["2010_gst_payable"]["credit_paise"] += p
            elif tt == "tds_credit":
                accounts["1020_tds_receivable"]["debit_paise"] += p
                accounts["4010_service_revenue"]["credit_paise"] += p
            elif tt == "expense":
                accounts["5010_operating_expenses"]["debit_paise"] += p
                accounts["1010_bank_cash"]["credit_paise"] += p
            elif tt == "direct_cost":
                accounts["5020_direct_project_costs"]["debit_paise"] += p
                accounts["1010_bank_cash"]["credit_paise"] += p
            elif tt == "refund":
                accounts["4010_service_revenue"]["debit_paise"] += p
                accounts["1010_bank_cash"]["credit_paise"] += p
            elif tt == "gst_refund":
                accounts["2010_gst_payable"]["debit_paise"] += p
                accounts["1010_bank_cash"]["credit_paise"] += p
            elif tt == "customer_advance":
                accounts["1010_bank_cash"]["debit_paise"] += p
                accounts["2020_customer_advances"]["credit_paise"] += p
            elif tt == "customer_advance_applied":
                accounts["2020_customer_advances"]["debit_paise"] += p
                accounts["4010_service_revenue"]["credit_paise"] += p
            elif tt == "customer_advance_refunded":
                accounts["2020_customer_advances"]["debit_paise"] += p
                accounts["1010_bank_cash"]["credit_paise"] += p
            elif tt == "owner_draw":
                accounts["3020_owner_drawings"]["debit_paise"] += p
                accounts["1010_bank_cash"]["credit_paise"] += p
            elif tt == "tax_payment":
                accounts["5040_tax_expense"]["debit_paise"] += p
                accounts["1010_bank_cash"]["credit_paise"] += p
            elif tt == "gst_paid":
                accounts["2010_gst_payable"]["debit_paise"] += p
                accounts["1010_bank_cash"]["credit_paise"] += p
            elif tt == "platform_fee":
                accounts["5030_platform_fees"]["debit_paise"] += p
                accounts["1010_bank_cash"]["credit_paise"] += p
            elif tt == "other_cash_in":
                accounts["1010_bank_cash"]["debit_paise"] += p
                accounts["4020_other_income"]["credit_paise"] += p
            elif tt == "other_cash_out":
                accounts["5050_other_expenses"]["debit_paise"] += p
                accounts["1010_bank_cash"]["credit_paise"] += p

    total_gross_debit_paise = sum(acc["debit_paise"] for acc in accounts.values())
    total_gross_credit_paise = sum(acc["credit_paise"] for acc in accounts.values())

    trial_balance_lines = []
    total_net_debit_paise = 0
    total_net_credit_paise = 0

    for code, acc in sorted(accounts.items()):
        dr = acc["debit_paise"]
        cr = acc["credit_paise"]
        if acc["normal_balance"] == "debit":
            net_balance = dr - cr
            net_dr = net_balance if net_balance >= 0 else 0
            net_cr = -net_balance if net_balance < 0 else 0
        else:
            net_balance = cr - dr
            net_cr = net_balance if net_balance >= 0 else 0
            net_dr = -net_balance if net_balance < 0 else 0

        total_net_debit_paise += net_dr
        total_net_credit_paise += net_cr

        trial_balance_lines.append({
            "account_code": code,
            "account_name": acc["name"],
            "account_type": acc["type"],
            "normal_balance": acc["normal_balance"],
            "gross_debit_inr": paise_to_inr(dr),
            "gross_credit_inr": paise_to_inr(cr),
            "net_debit_inr": paise_to_inr(net_dr),
            "net_credit_inr": paise_to_inr(net_cr),
            "net_balance_paise": net_balance,
        })

    imbalance_paise = abs(total_net_debit_paise - total_net_credit_paise)
    is_balanced = (imbalance_paise == 0) and (total_gross_debit_paise == total_gross_credit_paise)

    return {
        "is_balanced": is_balanced,
        "imbalance_paise": imbalance_paise,
        "imbalance_inr": paise_to_inr(imbalance_paise),
        "total_net_debit_inr": paise_to_inr(total_net_debit_paise),
        "total_net_credit_inr": paise_to_inr(total_net_credit_paise),
        "total_gross_debit_inr": paise_to_inr(total_gross_debit_paise),
        "total_gross_credit_inr": paise_to_inr(total_gross_credit_paise),
        "accounts": trial_balance_lines,
        "generated_at": utc_now(),
    }


def audit_ledger_integrity(include_sandbox: bool = False) -> dict[str, Any]:
    """
    Performs comprehensive financial audit across:
      1. Double-entry trial balance equilibrium (Total Debits == Total Credits in exact paise).
      2. Integer paise parity across all financial_transactions.
      3. Paid invoice settlement consistency (Receipts + GST + TDS == Invoice Total).
      4. Bank statement reconciliation integrity.
      5. Active SQLite append-only immutability triggers.
    """
    sandbox_clause = "" if include_sandbox else "WHERE (is_sandbox = 0 OR is_sandbox IS NULL)"
    sandbox_clause_and = "" if include_sandbox else "AND (is_sandbox = 0 OR is_sandbox IS NULL)"

    with connect() as conn:
        # 1. Trial balance
        tb = generate_double_entry_trial_balance(include_sandbox=include_sandbox)
        double_entry_ok = tb["is_balanced"] and tb["imbalance_paise"] == 0

        # 2. Integer paise parity in financial_transactions
        ft_rows = conn.execute(
            f"""SELECT id, amount_inr, amount_paise 
                FROM financial_transactions {sandbox_clause}"""
        ).fetchall()
        paise_drift_count = 0
        for r in ft_rows:
            expected_paise = inr_to_paise(float(r["amount_inr"]))
            actual_paise = int(r["amount_paise"] if r["amount_paise"] is not None else expected_paise)
            if expected_paise != actual_paise:
                paise_drift_count += 1
        paise_parity_ok = (paise_drift_count == 0)

        # 3. Paid invoice settlement consistency
        paid_invs = conn.execute(
            f"""SELECT id, invoice_number, amount_total, amount_paid 
                FROM invoices 
                WHERE status = 'paid' {sandbox_clause_and}"""
        ).fetchall()
        inv_discrepancies = []
        for inv in paid_invs:
            inv_total_paise = inr_to_paise(float(inv["amount_total"]))
            inv_paid_paise = inr_to_paise(float(inv["amount_paid"] or 0.0))
            if inv_total_paise != inv_paid_paise:
                inv_discrepancies.append({
                    "invoice_number": inv["invoice_number"],
                    "total_paise": inv_total_paise,
                    "paid_paise": inv_paid_paise,
                })
        paid_inv_ok = (len(inv_discrepancies) == 0)

        # 4. Bank statement reconciliation integrity
        st_reconciled = conn.execute(
            """SELECT id, matched_entity_type, matched_entity_id, is_reconciled, confirmed_by_owner
               FROM bank_statement_rows
               WHERE is_reconciled = 1"""
        ).fetchall()
        st_discrepancies = []
        for row in st_reconciled:
            if not row["matched_entity_type"] or not row["matched_entity_id"] or not row["confirmed_by_owner"]:
                st_discrepancies.append(dict(row))
        st_ok = (len(st_discrepancies) == 0)

        # 5. Immutability triggers active in SQLite
        trigger_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN ("
            "'trg_financial_transactions_no_update', 'trg_financial_transactions_no_delete', "
            "'trg_invoices_paid_immutable', 'trg_invoices_paid_no_delete')"
        ).fetchall()
        found_triggers = {r["name"] for r in trigger_rows}
        required_triggers = {
            "trg_financial_transactions_no_update",
            "trg_financial_transactions_no_delete",
            "trg_invoices_paid_immutable",
            "trg_invoices_paid_no_delete",
        }
        triggers_ok = required_triggers.issubset(found_triggers)

        all_passed = double_entry_ok and paise_parity_ok and paid_inv_ok and st_ok and triggers_ok

    return {
        "status": "PASS" if all_passed else "FAIL",
        "checks": {
            "double_entry_balanced": double_entry_ok,
            "imbalance_paise": tb["imbalance_paise"],
            "paise_parity_verified": paise_parity_ok,
            "paise_drift_count": paise_drift_count,
            "paid_invoices_consistent": paid_inv_ok,
            "invoice_discrepancies": inv_discrepancies,
            "statement_reconciliations_consistent": st_ok,
            "statement_discrepancies_count": len(st_discrepancies),
            "immutability_triggers_active": triggers_ok,
            "active_triggers": sorted(list(found_triggers)),
        },
        "trial_balance_summary": {
            "total_net_debit_inr": tb["total_net_debit_inr"],
            "total_net_credit_inr": tb["total_net_credit_inr"],
            "is_balanced": tb["is_balanced"],
        },
        "audited_at": utc_now(),
    }
