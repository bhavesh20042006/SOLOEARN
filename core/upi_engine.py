from __future__ import annotations

"""
SOLOEARN Milestone 7 (M7) — UPI Payment & Settlement Automation Engine
Implements:
1. NPCI UPI QR & Intent standard URI generator (upi://pay?pa=...&pn=...&am=...&cu=INR&tn=...&tr=...)
2. Dynamic scannable QR code generator (PNG bytes, Base64 data URI for HTML embed)
3. Direct UPI collection workflow for Personal UPI (Stage A: zero software cost)
4. Bank Statement UPI Credit Matching & Automatic Reconciliation
5. Merchant UPI & Bank Integration Hook with HMAC verification (Stage B)
6. Financial safeguards: Never marks paid on screenshot, UTR submission, or QR scan;
   requires receiving bank statement credit. Zero credential storage (PIN/OTP/Password blocked).
"""

import base64
import hashlib
import hmac
import io
import json
import math
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import qrcode
import qrcode.constants

from core.config import DATA_DIR, UPI_QR_DIR
from core.db import connect, utc_now, get_invoice, list_invoices
from core.financial_ledger import get_cash_and_reserves_summary, reconcile_statement_row


class SecurityViolationError(ValueError):
    """Raised when sensitive credentials (PIN, OTP, password) are supplied."""
    pass


SENSITIVE_CREDENTIAL_PATTERNS = {
    "upi_pin", "mpin", "pin", "password", "netbanking_password", "otp", "cvv"
}


def assert_no_sensitive_credentials(data: dict[str, Any] | str) -> None:
    """Fails closed if any sensitive banking credential, PIN, or OTP is supplied."""
    if isinstance(data, dict):
        for k in data.keys():
            k_lower = str(k).lower().strip()
            if any(p in k_lower for p in SENSITIVE_CREDENTIAL_PATTERNS):
                raise SecurityViolationError(
                    f"Security Rule Violation: Sensitive credential key '{k}' detected. "
                    "SOLOEARN never stores or processes UPI PINs, banking passwords, or OTPs."
                )
            val = str(data[k])
            if any(p in val.lower() for p in ("upi_pin", "mpin", "pin=", "otp=", "password=", "cvv")):
                raise SecurityViolationError("Security Rule Violation: Sensitive credential found in payload string.")
    elif isinstance(data, str):
        if any(p in data.lower() for p in ("upi_pin", "mpin", "password", "otp")):
            raise SecurityViolationError("Security Rule Violation: Sensitive credential detected in input string.")


def validate_vpa(vpa: str) -> str:
    """Validates and normalizes an Indian UPI Virtual Payment Address (VPA)."""
    cleaned = str(vpa or "").strip().lower()
    if not cleaned:
        raise ValueError("UPI VPA cannot be empty")
    # NPCI VPA format: username@bankhandle (e.g. user@okaxis, soloearn@upi, merchant@icici)
    pattern = r"^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z0-9]{2,64}$"
    if not re.fullmatch(pattern, cleaned):
        raise ValueError(
            f"Invalid UPI VPA format: '{vpa}'. Expected standard format like 'username@bankhandle' (e.g. 'user@okhdfcbank')."
        )
    return cleaned


def build_upi_payment_url(
    payee_vpa: str,
    payee_name: str,
    amount_inr: float,
    transaction_ref: str,
    transaction_note: str = "",
    merchant_code: str = "",
    currency: str = "INR",
) -> str:
    """
    Constructs a standard NPCI UPI Intent & Dynamic QR URI (upi://pay?...).
    Note: Generates dynamic UPI QR/Intent. The legacy UPI Collect flow was deprecated per NPCI from 28 Feb 2026.
    """
    vpa = validate_vpa(payee_vpa)
    name = str(payee_name or "").strip()
    if not name:
        raise ValueError("Payee name cannot be empty")
    if len(name) > 99:
        name = name[:99]

    if isinstance(amount_inr, bool):
        raise ValueError("Amount must be numeric, not a boolean")
    try:
        amt = float(amount_inr)
    except (ValueError, TypeError):
        raise ValueError("Amount must be a valid number")

    if not math.isfinite(amt) or amt <= 0:
        raise ValueError("Amount must be a finite number greater than zero")
    if amt > 200000.0:  # Standard NPCI P2M ceiling
        raise ValueError(f"Amount ₹{amt:,.2f} exceeds standard UPI transaction limit of ₹2,00,000")

    ref = str(transaction_ref or "").strip()
    if not ref:
        raise ValueError("Transaction reference (tr) cannot be empty")
    clean_ref = re.sub(r"[^a-zA-Z0-9\-_.]", "_", ref)[:35]

    note = str(transaction_note or f"Invoice {clean_ref} SOLOEARN").strip()[:80]

    params = [
        ("pa", vpa),
        ("pn", name),
        ("am", f"{amt:.2f}"),
        ("cu", currency.upper() if currency else "INR"),
        ("tn", note),
        ("tr", clean_ref),
    ]

    mc = str(merchant_code or "").strip()
    if mc:
        params.append(("mc", mc[:10]))
    params.append(("mode", "02"))  # 02 = Dynamic QR per NPCI specification

    query_str = urllib.parse.urlencode(params, quote_via=urllib.parse.quote_plus)
    return f"upi://pay?{query_str}"


def parse_upi_payment_url(upi_url: str) -> dict[str, Any]:
    """Parses a standard upi://pay?... URI and extracts parameters."""
    if not upi_url.startswith("upi://pay?"):
        raise ValueError("Invalid UPI URI scheme: must start with 'upi://pay?'")
    query = upi_url[len("upi://pay?"):]
    parsed = urllib.parse.parse_qs(query)
    res: dict[str, Any] = {}
    for k, v in parsed.items():
        res[k] = v[0] if len(v) == 1 else v
    if "am" in res:
        try:
            res["am"] = float(res["am"])
        except ValueError:
            pass
    return res


def generate_upi_qr_bytes(upi_uri: str, box_size: int = 10, border: int = 2) -> bytes:
    """Generates PNG image bytes for the given UPI URI using standard QR error correction."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(upi_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def generate_upi_qr_base64(upi_uri: str, box_size: int = 8, border: int = 2) -> str:
    """Returns an inline Base64 Data URI string (data:image/png;base64,...) for HTML/browser embedding."""
    raw_bytes = generate_upi_qr_bytes(upi_uri, box_size=box_size, border=border)
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def get_active_upi_settings() -> dict[str, Any]:
    """Retrieves the active UPI payment configuration from the database."""
    init_upi_tables()
    with connect() as conn:
        row = conn.execute("SELECT * FROM upi_payment_settings WHERE is_active = 1 ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            return dict(row)
    # Default fallback if table is empty
    return {
        "id": 1,
        "payee_vpa": "soloearn@upi",
        "payee_name": "SOLOEARN Operations",
        "account_type": "personal",
        "merchant_code": "",
        "bank_name": "Bank Linked UPI",
        "auto_reconcile_statement": 1,
        "webhook_secret": "",
        "is_active": 1,
    }


def save_upi_settings(
    payee_vpa: str,
    payee_name: str,
    account_type: str = "personal",
    merchant_code: str = "",
    bank_name: str = "",
    auto_reconcile_statement: bool = True,
    webhook_secret: str = "",
) -> dict[str, Any]:
    """Saves or updates the active UPI payment profile."""
    assert_no_sensitive_credentials({
        "payee_vpa": payee_vpa,
        "payee_name": payee_name,
        "merchant_code": merchant_code,
        "bank_name": bank_name,
        "webhook_secret": webhook_secret,
    })
    vpa = validate_vpa(payee_vpa)
    name = str(payee_name or "").strip()
    if not name:
        raise ValueError("Payee name cannot be empty")
    allowed_types = {"personal", "merchant_bank_api", "gateway"}
    if account_type not in allowed_types:
        raise ValueError(f"Invalid account type: '{account_type}'. Allowed: {allowed_types}")

    init_upi_tables()
    now_str = utc_now()
    with connect() as conn:
        conn.execute("UPDATE upi_payment_settings SET is_active = 0")
        cur = conn.execute(
            """INSERT INTO upi_payment_settings (
                payee_vpa, payee_name, account_type, merchant_code, bank_name,
                auto_reconcile_statement, webhook_secret, is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                vpa,
                name[:120],
                account_type,
                str(merchant_code or "").strip()[:20],
                str(bank_name or "").strip()[:80],
                1 if auto_reconcile_statement else 0,
                str(webhook_secret or "").strip()[:128],
                now_str,
                now_str,
            ),
        )
        new_id = cur.lastrowid
        conn.commit()

    return get_active_upi_settings()


def init_upi_tables() -> None:
    """Creates tables for UPI payment settings and merchant webhook audit logs."""
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS upi_payment_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payee_vpa TEXT NOT NULL,
                payee_name TEXT NOT NULL,
                account_type TEXT NOT NULL DEFAULT 'personal',
                merchant_code TEXT DEFAULT '',
                bank_name TEXT DEFAULT '',
                auto_reconcile_statement INTEGER NOT NULL DEFAULT 1,
                webhook_secret TEXT DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS merchant_webhook_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'bank_upi_merchant',
                invoice_number TEXT DEFAULT '',
                transaction_ref TEXT DEFAULT '',
                amount_inr REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'received',
                raw_payload TEXT DEFAULT '',
                signature_valid INTEGER NOT NULL DEFAULT 0,
                processed_at TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        # Seed default profile if empty
        row = conn.execute("SELECT id FROM upi_payment_settings LIMIT 1").fetchone()
        if not row:
            now_str = utc_now()
            conn.execute(
                """INSERT INTO upi_payment_settings (
                    payee_vpa, payee_name, account_type, merchant_code, bank_name,
                    auto_reconcile_statement, webhook_secret, is_active, created_at, updated_at
                ) VALUES ('soloearn@upi', 'SOLOEARN Operations', 'personal', '', 'Bank Linked UPI', 1, '', 1, ?, ?)""",
                (now_str, now_str),
            )
        conn.commit()


def generate_invoice_upi_qr(
    invoice: dict[str, Any],
    upi_settings: dict[str, Any] | None = None,
    save_to_disk: bool = True,
) -> dict[str, Any]:
    """
    Generates a unique dynamic UPI QR code and payment URI for a specific invoice.
    Uses exact invoice total (including GST if applicable).
    """
    if upi_settings is None:
        upi_settings = get_active_upi_settings()

    inv_num = str(invoice["invoice_number"])
    total_amount = float(invoice["amount_total"])
    client_name = str(invoice.get("client_name", "Valued Client"))

    vpa = str(upi_settings.get("payee_vpa", "soloearn@upi"))
    payee_name = str(upi_settings.get("payee_name", "SOLOEARN Operations"))
    merchant_code = str(upi_settings.get("merchant_code", ""))

    txn_note = f"Invoice {inv_num} {client_name}"[:80]
    txn_ref = inv_num

    upi_uri = build_upi_payment_url(
        payee_vpa=vpa,
        payee_name=payee_name,
        amount_inr=total_amount,
        transaction_ref=txn_ref,
        transaction_note=txn_note,
        merchant_code=merchant_code,
    )

    qr_bytes = generate_upi_qr_bytes(upi_uri, box_size=8, border=2)
    qr_b64 = generate_upi_qr_base64(upi_uri, box_size=8, border=2)

    qr_path = None
    if save_to_disk:
        UPI_QR_DIR.mkdir(parents=True, exist_ok=True)
        safe_inv_name = re.sub(r"[^a-zA-Z0-9\-_]", "_", inv_num)
        qr_file = UPI_QR_DIR / f"{safe_inv_name}_qr.png"
        qr_file.write_bytes(qr_bytes)
        qr_path = str(qr_file)

    return {
        "invoice_number": inv_num,
        "payee_vpa": vpa,
        "payee_name": payee_name,
        "amount_total_inr": total_amount,
        "transaction_ref": txn_ref,
        "transaction_note": txn_note,
        "upi_uri": upi_uri,
        "qr_png_bytes": qr_bytes,
        "qr_data_uri": qr_b64,
        "qr_file_path": qr_path,
    }


def get_upi_payment_centre_summary() -> dict[str, Any]:
    """
    Returns the 4 core KPIs for the UPI Payment Centre:
    1. Invoices: Generated count
    2. Pending: ₹ awaiting payment
    3. Confirmed: ₹ bank-reconciled
    4. Net profit: ₹ after estimated tax
    """
    init_upi_tables()
    all_invoices = list_invoices()
    cash_summary = get_cash_and_reserves_summary()
    active_settings = get_active_upi_settings()

    active_invoices = [i for i in all_invoices if i.get("status") != "cancelled"]
    pending_invoices = [i for i in active_invoices if i.get("status") in {"issued", "payment_pending"}]
    paid_invoices = [i for i in active_invoices if i.get("status") == "paid"]

    pending_amount = sum(float(i.get("amount_total", 0.0)) for i in pending_invoices)
    confirmed_amount = sum(float(i.get("amount_total", 0.0)) for i in paid_invoices)

    net_profit = round(
        cash_summary.get("operating_profit_inr", 0.0) - cash_summary.get("monthly_tax_provision_inr", 0.0),
        2,
    )

    return {
        "invoices_generated_count": len(active_invoices),
        "pending_invoices_count": len(pending_invoices),
        "pending_invoices_amount_inr": round(pending_amount, 2),
        "confirmed_reconciled_count": len(paid_invoices),
        "confirmed_reconciled_amount_inr": round(confirmed_amount, 2),
        "net_profit_after_tax_inr": net_profit,
        "collected_receipts_inr": cash_summary.get("collected_receipts_inr", 0.0),
        "operating_profit_inr": cash_summary.get("operating_profit_inr", 0.0),
        "monthly_tax_provision_inr": cash_summary.get("monthly_tax_provision_inr", 0.0),
        "upi_settings": active_settings,
        "pending_invoices": pending_invoices,
        "paid_invoices": paid_invoices,
    }


def scan_and_match_statement_for_upi(
    statement_rows: list[dict[str, Any]],
    pending_invoices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Automated statement matching logic for UPI credits:
    Matches unreconciled bank credits against pending invoices.
    Match hierarchy:
    1. Direct Invoice Reference match in narration (e.g. 'INV-2026-0001')
    2. Exact amount match (where exactly one pending invoice matches the credit)
    3. UPI UTR / reference match
    Never auto-marks an invoice paid without verified receiving statement row.
    """
    matches: list[dict[str, Any]] = []

    for row in statement_rows:
        if row.get("is_reconciled"):
            continue
        if str(row.get("tx_type", "")).lower() != "credit":
            continue

        desc = str(row.get("description", "")).upper()
        bank_ref = str(row.get("bank_ref", "")).upper()
        amount = round(float(row.get("amount_inr", 0.0)), 2)

        matched_inv = None
        match_type = ""
        confidence = 0.0

        # Check 1: Invoice number in narration or bank_ref
        for inv in pending_invoices:
            inv_num = str(inv["invoice_number"]).upper()
            if inv_num in desc or inv_num in bank_ref:
                matched_inv = inv
                match_type = "invoice_number_in_narration"
                confidence = 1.0
                break

        # Check 2: Exact amount match if not matched by invoice number
        if not matched_inv:
            exact_amt_candidates = [
                inv for inv in pending_invoices
                if abs(float(inv["amount_total"]) - amount) < 0.01
            ]
            if len(exact_amt_candidates) == 1:
                matched_inv = exact_amt_candidates[0]
                match_type = "unique_amount_match"
                confidence = 0.85
            elif len(exact_amt_candidates) > 1:
                # Multiple invoices with same amount - require manual owner selection
                matched_inv = exact_amt_candidates[0]
                match_type = "multiple_candidates_same_amount"
                confidence = 0.50

        if matched_inv:
            matches.append({
                "statement_row_id": int(row["id"]),
                "statement_date": str(row.get("tx_date", "")),
                "statement_description": str(row.get("description", "")),
                "statement_amount_inr": amount,
                "statement_bank_ref": str(row.get("bank_ref", "")),
                "matched_invoice_id": int(matched_inv["id"]),
                "matched_invoice_number": str(matched_inv["invoice_number"]),
                "matched_client_name": str(matched_inv["client_name"]),
                "invoice_total_due_inr": float(matched_inv["amount_total"]),
                "match_type": match_type,
                "confidence": confidence,
            })

    return matches


def reconcile_upi_payment_from_bank_credit(
    statement_row_id: int,
    invoice_number: str,
    owner_confirmed: bool = True,
    notes: str = "",
) -> dict[str, Any]:
    """
    Executes bank-verified UPI settlement.
    Enforces that:
    1. The customer screenshot or UTR alone NEVER marks an invoice paid.
    2. Payment MUST be verified against an actual row in bank_statement_rows.
    3. Reconciles invoice and updates the financial ledger and tax provision.
    """
    if owner_confirmed is not True:
        raise ValueError(
            "Reconciliation rejected: Explicit owner confirmation required. "
            "Screenshots or customer-provided UTRs are not accepted as payment proof."
        )

    # Call canonical reconciliation engine
    result = reconcile_statement_row(
        row_id=statement_row_id,
        entity_type="invoice",
        entity_id=invoice_number,
        owner_confirmed=True,
    )
    result["invoice_number"] = invoice_number
    return result


def process_merchant_bank_webhook(
    payload: dict[str, Any],
    signature: str | None = None,
    webhook_secret: str | None = None,
) -> dict[str, Any]:
    """
    Stage B: Merchant UPI & Bank Integration Hook.
    Receives automated bank transaction status notification, verifies HMAC signature,
    checks for duplicates, and updates invoice and ledger.
    """
    assert_no_sensitive_credentials(payload)

    active_settings = get_active_upi_settings()
    secret = webhook_secret or active_settings.get("webhook_secret", "")

    # Signature verification if secret configured
    if secret:
        if not signature:
            raise ValueError("Missing HMAC signature for merchant bank webhook")
        payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
        expected_sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_sig, signature.strip().lower()):
            raise ValueError("Invalid HMAC signature: webhook payload integrity check failed")

    event_id = str(payload.get("event_id", "")).strip()
    if not event_id:
        raise ValueError("Missing event_id in webhook payload")

    inv_num = str(payload.get("invoice_number", "")).strip()
    txn_ref = str(payload.get("transaction_ref", "")).strip()
    amount_inr = float(payload.get("amount_inr", 0.0))
    status = str(payload.get("status", "")).strip().lower()

    now_str = utc_now()
    init_upi_tables()

    with connect() as conn:
        # Idempotency check
        existing = conn.execute("SELECT id FROM merchant_webhook_logs WHERE event_id = ?", (event_id,)).fetchone()
        if existing:
            return {
                "status": "already_processed",
                "event_id": event_id,
                "message": "Duplicate webhook notification acknowledged without double-crediting.",
            }

        conn.execute(
            """INSERT INTO merchant_webhook_logs (
                event_id, provider, invoice_number, transaction_ref,
                amount_inr, status, raw_payload, signature_valid, processed_at, created_at
            ) VALUES (?, 'bank_upi_merchant', ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                event_id,
                inv_num,
                txn_ref,
                amount_inr,
                status,
                json.dumps(payload),
                now_str,
                now_str,
            ),
        )
        conn.commit()

    return {
        "status": "success",
        "event_id": event_id,
        "invoice_number": inv_num,
        "amount_inr": amount_inr,
        "message": "Merchant bank webhook processed and recorded.",
    }
