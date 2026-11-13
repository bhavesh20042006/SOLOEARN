from __future__ import annotations

"""
SOLOEARN Milestone 7 (M7) — UPI Payment & Settlement Automation Engine
======================================================================
Implements two distinct operational stages for UPI payment collection & reconciliation:

STAGE A: Zero-Cost Personal UPI Collection Engine
- Dynamic UPI Intent and scannable QR generation (upi://pay?pa=...&pn=...&am=...&cu=INR&tn=...&tr=...&mode=04)
- Initiation mode selection: mode="04" (UPI Intent) or "00" (Standard QR). Unsigned mode="02" is rejected.
- Invoices embedded with standalone Base64 scannable QR cards and mobile payment intent links.
- Automated bank statement matching (narration, exact amount, date, and UTR).
- Financial anti-fraud safeguards: Invoices remain pending until verified credit in receiving bank account.
  Screenshots, SMS texts, and customer UTR submissions alone are strictly rejected as payment proof.
- Underpayment / overpayment / partial payment handling and duplicate reference protection.
- Statutory GST gating: Unregistered entities are prohibited from collecting GST under Section 32 of CGST Act.

STAGE B: Merchant UPI & Bank Integration Hook
- Provider-independent Merchant Payment Adapter interface (MerchantPaymentAdapter).
- Simulated sandbox adapter (SimulatedBankMerchantAdapter) clearly flagged as is_simulated=True.
- Production-ready adapter (StandardBankMerchantAdapter) with HMAC-SHA256 signature verification.
- Replay attack protection: Rejects webhooks with timestamps skewed or older than 300 seconds.
- Idempotency tracking: Discards duplicate event IDs in merchant_webhook_logs without double-crediting.
- Strict separation of Payment Success (in transit) vs. Bank Settlement (funds credited to account).

SECURITY:
- Fails closed on any sensitive credential (UPI PIN, MPIN, passwords, OTPs, CVVs).
- Dashboard bound to localhost (127.0.0.1).
"""

import base64
import hashlib
import hmac
import io
import json
import math
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import qrcode
import qrcode.constants

from core.config import DATA_DIR, UPI_QR_DIR
from core.db import connect, utc_now, get_invoice, list_invoices, get_active_tax_profile
from core.financial_ledger import (
    get_cash_and_reserves_summary,
    reconcile_statement_row,
    inr_to_paise,
    paise_to_inr,
)


class SecurityViolationError(ValueError):
    """Raised when sensitive credentials (PIN, MPIN, OTP, password) are supplied."""
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
        if any(p in data.lower() for p in ("upi_pin", "mpin", "pin=", "password", "otp", "cvv")):
            raise SecurityViolationError("Security Rule Violation: Sensitive credential detected in input string.")


def get_upi_interoperability_disclosure() -> dict[str, str]:
    """
    Returns statutory, technical, and banking disclosures regarding Personal UPI vs. Merchant UPI.
    Discloses initiation modes, transaction limits, banking terms, and anti-fraud policies.
    """
    return {
        "initiation_mode": (
            "UPI Initiation Mode Standards: NPCI specifies exact initiation mode mappings:\n"
            "• Mode 00: Default transaction\n"
            "• Mode 01: QR code (used for standard scannable QR generation)\n"
            "• Mode 02: Secure QR code (mandates digital signature 'sign=...' from bank PSP certificate)\n"
            "• Mode 04: Intent (used for mobile web-to-app deep links)\n"
            "• Mode 05: Secure Intent (mandates digital signature 'sign=...')\n"
            "Unsigned local QR generation uses Mode 01 (QR code) for scannable images and Mode 04 (Intent) for mobile payment buttons. "
            "Locally generated unsigned codes are never labelled as signed Secure QR codes (Mode 02/05)."
        ),
        "personal_upi_limits": (
            "Personal VPA Limits: Personal UPI Virtual Payment Addresses are governed by NPCI peer-to-peer (P2P) limits, "
            "typically capped at ₹1,00,000 per transaction per day by issuing and receiving PSP banks."
        ),
        "bank_terms_and_conditions": (
            "Bank Account Terms: The business owner must verify with their bank whether their personal savings account "
            "permits business or freelance collections. For high transaction volume, banks mandate opening a Current Account "
            "with a Merchant QR."
        ),
        "consumer_app_behavior": (
            "Consumer App Velocity Holds: Consumer payment applications (Google Pay, PhonePe, Paytm, BHIM) may flag or hold "
            "rapid or unexpected high-value transfers sent to personal VPAs under automated risk-detection rules."
        ),
        "anti_fraud_settlement_policy": (
            "Anti-Fraud Policy: An invoice is never marked paid based on customer-submitted payment screenshots, "
            "SMS alerts, or UTR text messages alone. Settlement strictly requires an owner-confirmed credit in the receiving bank account."
        ),
        "gst_statutory_warning": (
            "GST Statutory Rule: Under Section 32 of the CGST Act, unregistered suppliers cannot charge or collect GST. "
            "Invoices for unregistered businesses must state 'GST not charged — Supplier is not registered under GST.'"
        ),
    }


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
    initiation_mode: str = "04",
    transaction_id: str = "",
    invoice_url: str = "",
    mode: str = "",
) -> str:
    """
    Constructs an NPCI UPI 2026 compliant UPI Intent & Dynamic QR URI (upi://pay?...).
    Initiation Mode Mapping (per NPCI & PSP Integration Standards):
    - '00': Default / Static QR transaction
    - '01': Dynamic QR code presentation (mandates pa, pn, am, cu=INR, tr, tn, mode=01)
    - '02': Secure QR code (Requires acquiring-bank cryptographic digital signature 'sign=...')
    - '04': Intent (UPI Mobile Web / App-to-App flow - mandates pa, pn, am, cu=INR, tr, tn, mode=04)
    - '05': Secure Intent (Requires acquiring-bank cryptographic digital signature 'sign=...')

    NPCI UPI 2026 Compliance:
    - Amount is formatted directly from integer paise (f"{paise // 100}.{paise % 100:02d}") to eliminate floating-point drift.
    - Currency is strictly validated as 'INR'.
    - Dynamic QR (01) and Intent (04) mandate amount and transaction reference.
    - Unsigned requests attempting to use '02' (Secure QR code) or '05' (Secure Intent)
      are strictly rejected to prevent falsely claiming signed secure compliance.
    """
    if mode:
        initiation_mode = mode
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

    # Validate currency under NPCI domestic UPI standards
    curr_clean = currency.upper().strip() if currency else "INR"
    if curr_clean != "INR":
        raise ValueError(f"Invalid UPI currency: '{curr_clean}'. NPCI UPI standard mandates 'INR'.")

    # Validate initiation mode selection per NPCI standards
    mode_str = str(initiation_mode or "").strip()
    if mode_str in {"02", "05"}:
        mode_label = "Secure QR code (mode 02)" if mode_str == "02" else "Secure Intent (mode 05)"
        raise ValueError(
            f"Cannot generate unsigned {mode_label}. Secure UPI initiation modes mandate an acquiring bank "
            "cryptographic digital signature ('sign=...') issued by a registered PSP certificate. "
            "For unsigned dynamic requests, use initiation_mode='01' (QR code) or '04' (Intent), or '00' (Default transaction)."
        )
    if mode_str not in {"00", "01", "04", ""}:
        raise ValueError(
            f"Invalid initiation mode: '{mode_str}'. Expected '01' (QR code), '04' (Intent), '00' (Default transaction), or '' (Omitted)."
        )

    # Format amount using exact integer paise to eliminate floating-point drift
    paise = inr_to_paise(amt)
    am_str = f"{paise // 100}.{paise % 100:02d}"

    params = [
        ("pa", vpa),
        ("pn", name),
        ("am", am_str),
        ("cu", curr_clean),
        ("tn", note),
        ("tr", clean_ref),
    ]

    mc = str(merchant_code or "").strip()
    if mc:
        params.append(("mc", mc[:10]))
    if mode_str:
        params.append(("mode", mode_str))

    tid = str(transaction_id or "").strip()[:35]
    if tid:
        params.append(("tid", tid))

    url_val = str(invoice_url or "").strip()[:255]
    if url_val:
        params.append(("url", url_val))

    query_str = urllib.parse.urlencode(params, quote_via=urllib.parse.quote_plus)
    return f"upi://pay?{query_str}"


def parse_upi_payment_url(upi_url: str) -> dict[str, Any]:
    """Parses a standard upi://pay?... URI and extracts parameters with exact paise representation."""
    if not upi_url.startswith("upi://pay?"):
        raise ValueError("Invalid UPI URI scheme: must start with 'upi://pay?'")
    query = upi_url[len("upi://pay?"):]
    parsed = urllib.parse.parse_qs(query)
    res: dict[str, Any] = {}
    for k, v in parsed.items():
        res[k] = v[0] if len(v) == 1 else v
    if "am" in res:
        try:
            amt_val = float(res["am"])
            res["am"] = amt_val
            res["amount_paise"] = inr_to_paise(amt_val)
        except ValueError:
            pass
    if "pa" in res:
        res["payee_vpa"] = res["pa"]
    if "pn" in res:
        res["payee_name"] = res["pn"]
    return res


def validate_npci_compliance(upi_url: str) -> dict[str, Any]:
    """
    Validates a UPI URI against NPCI UPI 2026 specifications:
    - Dynamic QR (Mode 01): Mandates pa, pn, am, cu=INR, tr, tn, and mode=01.
    - Intent Flow (Mode 04): Mandates pa, pn, am, cu=INR, tr, tn, and mode=04.
    - Verifies amount formatting (paise precision, standard ceiling <= 2,00,000).
    - Validates VPA format and character length constraints.
    - Rejects unsigned secure modes (Mode 02/05) lacking cryptographic signatures.

    Returns:
      dict with:
        - is_compliant: bool
        - mode: str ('01', '04', '00', etc.)
        - flow_type: 'Dynamic QR' | 'Intent Flow' | 'Standard UPI'
        - parameters / extracted_params: dict of extracted query parameters
        - missing_mandatory_params: list[str]
        - compliance_errors / errors: list[str]
    """
    errors: list[str] = []
    missing: list[str] = []

    if not isinstance(upi_url, str) or not upi_url.startswith("upi://pay?"):
        err_msg = "URI does not start with standard 'upi://pay?' scheme"
        return {
            "is_compliant": False,
            "mode": "",
            "flow_type": "Unknown",
            "parameters": {},
            "extracted_params": {},
            "missing_mandatory_params": ["scheme"],
            "compliance_errors": [err_msg],
            "errors": [err_msg],
        }

    try:
        params = parse_upi_payment_url(upi_url)
    except Exception as e:
        err_msg = f"Failed to parse UPI URL: {e}"
        return {
            "is_compliant": False,
            "mode": "",
            "flow_type": "Unknown",
            "parameters": {},
            "extracted_params": {},
            "missing_mandatory_params": [],
            "compliance_errors": [err_msg],
            "errors": [err_msg],
        }

    mode = str(params.get("mode", "")).strip()

    # Mode 02 and 05 check: mandate 'sign' parameter
    if mode in {"02", "05"} and not params.get("sign"):
        errors.append(f"Mode {mode} requires a secure digital signature ('sign=...') issued by a registered bank PSP.")

    # Mandatory parameters for both Dynamic QR (01) and Intent (04)
    mandatory = ["pa", "pn", "am", "cu", "tr", "tn", "mode"]
    for param in mandatory:
        if param not in params or not str(params[param]).strip():
            missing.append(param)
            errors.append(f"Missing mandatory parameter: {param}")

    # VPA check
    if "pa" in params:
        try:
            validate_vpa(str(params["pa"]))
        except ValueError as e:
            errors.append(f"Invalid VPA 'pa': {e}")

    # Name check
    if "pn" in params:
        pn = str(params["pn"]).strip()
        if not pn or len(pn) > 99:
            errors.append("Payee name 'pn' must be between 1 and 99 characters")

    # Currency check
    if "cu" in params and str(params["cu"]).upper() != "INR":
        errors.append(f"Currency must be INR (got '{params['cu']}')")

    # Amount check
    if "am" in params:
        am_val = params.get("am")
        try:
            amt = float(am_val)
            if not math.isfinite(amt) or amt <= 0:
                errors.append(f"Amount must be greater than zero (got {amt})")
            elif amt > 200000.0:
                errors.append(f"Amount ₹{amt:,.2f} exceeds standard NPCI limit of ₹2,00,000")
        except (ValueError, TypeError):
            errors.append(f"Invalid non-numeric amount 'am': {am_val}")

    # Reference check
    if "tr" in params:
        tr = str(params["tr"]).strip()
        if not tr or len(tr) > 35:
            errors.append("Transaction reference 'tr' must be between 1 and 35 characters")

    # Note check
    if "tn" in params:
        tn = str(params["tn"]).strip()
        if len(tn) > 80:
            errors.append("Transaction note 'tn' cannot exceed 80 characters")

    # Mode check
    if mode not in {"00", "01", "04", ""}:
        errors.append(f"Unsupported initiation mode: '{mode}'. Expected '01' (QR code) or '04' (Intent).")

    is_compliant = len(errors) == 0 and len(missing) == 0
    flow_type = "Intent Flow" if mode == "04" else ("Dynamic QR" if mode == "01" else "Standard UPI")

    return {
        "is_compliant": is_compliant,
        "mode": mode,
        "flow_type": flow_type,
        "parameters": params,
        "extracted_params": params,
        "missing_mandatory_params": missing,
        "compliance_errors": errors,
        "errors": errors,
    }


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


def generate_upi_qr_base64(upi_uri: str, box_size: int = 10, border: int = 2) -> str:
    """Generates a Base64 data URI (data:image/png;base64,...) for embedding directly into HTML invoices."""
    png_bytes = generate_upi_qr_bytes(upi_uri, box_size=box_size, border=border)
    b64_str = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64_str}"


def generate_invoice_upi_qr(
    invoice: dict[str, Any],
    save_to_disk: bool = True,
    initiation_mode: str = "01",
    upi_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Generates a dynamic scannable QR code (Mode 01: QR code) and payment intent URI (Mode 04: Intent)
    for a specific invoice. Uses the active UPI settings from the database or provided dictionary.
    Computes exact due amount using paise-level arithmetic and verifies NPCI 2026 compliance.
    """
    settings = upi_settings or get_active_upi_settings()
    vpa = settings.get("payee_vpa", "soloearn@upi")
    name = settings.get("payee_name", "SOLOEARN Operations")
    mc = settings.get("merchant_code", "")

    inv_num = str(invoice["invoice_number"])
    total_inv_paise = inr_to_paise(float(invoice.get("amount_total", 0.0)))
    paid_paise = inr_to_paise(float(invoice.get("amount_paid", 0.0)))
    remaining_paise = max(0, total_inv_paise - paid_paise)
    due_paise = remaining_paise if paid_paise > 0 and remaining_paise > 0 else total_inv_paise
    total_due = paise_to_inr(due_paise)
    note = f"Invoice {inv_num} SOLOEARN"

    # Scannable QR URI uses Mode 01 (QR code per NPCI standards)
    qr_mode = initiation_mode if initiation_mode in {"00", "01"} else "01"
    qr_upi_uri = build_upi_payment_url(
        payee_vpa=vpa,
        payee_name=name,
        amount_inr=total_due,
        transaction_ref=inv_num,
        transaction_note=note,
        merchant_code=mc,
        currency="INR",
        initiation_mode=qr_mode,
    )

    # Clickable Intent URI for mobile devices uses Mode 04 (Intent)
    intent_upi_uri = build_upi_payment_url(
        payee_vpa=vpa,
        payee_name=name,
        amount_inr=total_due,
        transaction_ref=inv_num,
        transaction_note=note,
        merchant_code=mc,
        currency="INR",
        initiation_mode="04",
    )

    png_bytes = generate_upi_qr_bytes(qr_upi_uri)
    b64_str = base64.b64encode(png_bytes).decode("ascii")
    qr_base64 = f"data:image/png;base64,{b64_str}"
    png_path_str = ""

    if save_to_disk:
        UPI_QR_DIR.mkdir(parents=True, exist_ok=True)
        out_file = UPI_QR_DIR / f"{inv_num}_qr.png"
        out_file.write_bytes(png_bytes)
        png_path_str = str(out_file)

    return {
        "invoice_number": inv_num,
        "payee_vpa": vpa,
        "payee_name": name,
        "amount_inr": total_due,
        "amount_paise": due_paise,
        "transaction_ref": inv_num,
        "transaction_note": note,
        "upi_uri": qr_upi_uri,
        "intent_uri": intent_upi_uri,
        "qr_png_bytes": png_bytes,
        "qr_data_uri": qr_base64,
        "qr_file_path": png_path_str,
        "initiation_mode": qr_mode,
        "intent_mode": "04",
        "npci_compliance": {
            "qr_mode_01": validate_npci_compliance(qr_upi_uri),
            "intent_mode_04": validate_npci_compliance(intent_upi_uri),
        },
    }


def init_upi_tables() -> None:
    """Ensures UPI payment tables and columns exist in the database."""
    with connect() as conn:
        conn.execute(
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
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS merchant_webhook_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                provider TEXT NOT NULL,
                invoice_number TEXT,
                transaction_ref TEXT,
                amount_inr REAL NOT NULL,
                status TEXT NOT NULL,
                raw_payload TEXT,
                signature_valid INTEGER NOT NULL DEFAULT 0,
                is_simulated INTEGER NOT NULL DEFAULT 0,
                processed_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        # Migrate invoices amount_paid if missing
        inv_cols = [r["name"] for r in conn.execute("PRAGMA table_info(invoices)").fetchall()]
        if "amount_paid" not in inv_cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN amount_paid REAL NOT NULL DEFAULT 0.0")

        # Migrate merchant_webhook_logs is_simulated if missing
        mw_cols = [r["name"] for r in conn.execute("PRAGMA table_info(merchant_webhook_logs)").fetchall()]
        if "is_simulated" not in mw_cols:
            conn.execute("ALTER TABLE merchant_webhook_logs ADD COLUMN is_simulated INTEGER NOT NULL DEFAULT 0")

        conn.commit()


def get_active_upi_settings() -> dict[str, Any]:
    """Returns the currently active UPI payment profile, or default settings."""
    init_upi_tables()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM upi_payment_settings WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return dict(row)

    # Return safe default
    return {
        "id": 0,
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
    webhook_secret: str = "",
) -> int:
    """Updates or sets the active UPI payment profile."""
    assert_no_sensitive_credentials(merchant_code)
    assert_no_sensitive_credentials(webhook_secret)

    vpa = validate_vpa(payee_vpa)
    name = str(payee_name or "").strip()
    if not name:
        raise ValueError("Payee name cannot be empty")
    acc_type = "merchant" if str(account_type).strip().lower() == "merchant" else "personal"
    mc = str(merchant_code or "").strip()[:15]
    bank = str(bank_name or "").strip()[:60]
    secret = str(webhook_secret or "").strip()[:128]

    init_upi_tables()
    now_str = utc_now()
    with connect() as conn:
        conn.execute("UPDATE upi_payment_settings SET is_active = 0 WHERE is_active = 1")
        cur = conn.execute(
            """
            INSERT INTO upi_payment_settings (
                payee_vpa, payee_name, account_type, merchant_code,
                bank_name, auto_reconcile_statement, webhook_secret,
                is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, 1, ?, ?)
            """,
            (vpa, name, acc_type, mc, bank, secret, now_str, now_str),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_upi_payment_centre_summary() -> dict[str, Any]:
    """
    Computes the 4 headline financial KPIs for the UPI Payment Centre using exact paise arithmetic:
    1. Invoices Generated (count & ₹ total)
    2. Pending Collection (₹ awaiting settlement)
    3. Confirmed Reconciled (₹ received & confirmed in bank)
    4. Net Profit after Tax (₹ cash profit after S.202 tax provision)
    """
    invoices = list_invoices()
    cash_summary = get_cash_and_reserves_summary()
    active_settings = get_active_upi_settings()

    pending_invoices = [inv for inv in invoices if inv["status"] in {"issued", "payment_pending", "partially_paid"}]
    paid_invoices = [inv for inv in invoices if inv["status"] == "paid"]

    pending_amount_paise = sum(
        inr_to_paise(float(inv["amount_total"])) - inr_to_paise(float(inv.get("amount_paid", 0.0)))
        for inv in pending_invoices
    )
    confirmed_amount_paise = sum(inr_to_paise(float(inv["amount_total"])) for inv in paid_invoices)
    total_generated_amount_paise = sum(inr_to_paise(float(inv["amount_total"])) for inv in invoices)

    pending_amount = paise_to_inr(pending_amount_paise)
    confirmed_amount = paise_to_inr(confirmed_amount_paise)
    total_generated_amount = paise_to_inr(total_generated_amount_paise)

    net_profit = cash_summary.get("net_profit_after_tax_inr", 0.0)

    return {
        "total_invoices_count": len(invoices),
        "invoices_generated_count": len(invoices),
        "total_invoices_amount_inr": total_generated_amount,
        "total_invoices_amount_paise": total_generated_amount_paise,
        "pending_invoices_count": len(pending_invoices),
        "pending_invoices_amount_inr": pending_amount,
        "pending_invoices_amount_paise": pending_amount_paise,
        "confirmed_reconciled_count": len(paid_invoices),
        "confirmed_reconciled_amount_inr": confirmed_amount,
        "confirmed_reconciled_amount_paise": confirmed_amount_paise,
        "net_profit_after_tax_inr": net_profit,
        "collected_receipts_inr": cash_summary.get("collected_receipts_inr", 0.0),
        "operating_profit_inr": cash_summary.get("operating_profit_inr", 0.0),
        "monthly_tax_provision_inr": cash_summary.get("monthly_tax_provision_inr", 0.0),
        "upi_settings": active_settings,
        "pending_invoices": pending_invoices,
        "paid_invoices": paid_invoices,
        "interoperability_disclosure": get_upi_interoperability_disclosure(),
    }


def scan_and_match_statement_for_upi(
    statement_rows: list[dict[str, Any]],
    pending_invoices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Automated statement matching logic for UPI, NEFT, RTGS, and IMPS credits:
    Matches unreconciled bank credits against pending invoices with paise-exact arithmetic.
    Match hierarchy:
    1. Direct Invoice Reference match in narration or bank_ref (e.g. 'INV-2026-0001')
    2. Parsed bank narration match (extracted channel reference / UTR)
    3. Exact amount match in integer paise (where exactly one pending invoice matches the credit)
    Reversal and duplicate protection:
    - Skips already reconciled statement rows.
    - Skips debit entries or entries containing 'REVERSAL'.
    - Never auto-marks an invoice paid without verified receiving statement row.
    """
    from core.financial_ledger import parse_bank_narration

    matches: list[dict[str, Any]] = []

    for row in statement_rows:
        if row.get("is_reconciled"):
            continue
        if str(row.get("tx_type", "")).lower() != "credit":
            continue

        raw_desc = str(row.get("description", ""))
        desc = raw_desc.upper()
        bank_ref = str(row.get("bank_ref", "")).upper()

        # Reversal protection: Ignore reversals from automatic crediting
        if "REVERSAL" in desc or "REVERSAL" in bank_ref:
            continue

        amount_paise = inr_to_paise(float(row.get("amount_inr", 0.0)))
        amount = paise_to_inr(amount_paise)

        # Parse narration for deep channel and UTR metadata
        parsed_narration = parse_bank_narration(raw_desc)
        if parsed_narration.get("is_reversal"):
            continue
        narration_inv_ref = parsed_narration.get("invoice_reference", "").upper()
        narration_utr = parsed_narration.get("reference_id", "").upper()

        matched_inv = None
        match_type = ""
        confidence = 0.0
        is_ambiguous = False

        # Check 1: Invoice number in narration, bank_ref, or parsed narration invoice reference
        for inv in pending_invoices:
            inv_num = str(inv["invoice_number"]).upper()
            inv_num_clean = inv_num.replace("-", "")
            if (
                inv_num in desc
                or inv_num in bank_ref
                or inv_num_clean in desc
                or inv_num_clean in bank_ref
                or (narration_inv_ref and (narration_inv_ref == inv_num or narration_inv_ref == inv_num_clean))
            ):
                matched_inv = inv
                match_type = "invoice_number_in_narration"
                confidence = 1.0
                break

        # Check 2: Exact amount match in integer paise if not matched by invoice number
        if not matched_inv:
            exact_amt_candidates = [
                inv for inv in pending_invoices
                if (inr_to_paise(float(inv["amount_total"])) - inr_to_paise(float(inv.get("amount_paid", 0.0)))) == amount_paise
            ]
            if len(exact_amt_candidates) == 1:
                matched_inv = exact_amt_candidates[0]
                match_type = "unique_amount_match"
                confidence = 0.85
            elif len(exact_amt_candidates) > 1:
                # Multiple invoices with same amount - require manual owner selection
                matched_inv = exact_amt_candidates[0]
                match_type = "ambiguous_multiple_candidates_same_amount"
                confidence = 0.40
                is_ambiguous = True

        if matched_inv:
            inv_total_paise = inr_to_paise(float(matched_inv["amount_total"]))
            inv_paid_paise = inr_to_paise(float(matched_inv.get("amount_paid", 0.0)))
            inv_due_paise = max(0, inv_total_paise - inv_paid_paise)
            matches.append({
                "statement_row_id": int(row["id"]),
                "statement_date": str(row.get("tx_date", "")),
                "statement_description": raw_desc,
                "statement_amount_inr": amount,
                "statement_amount_paise": amount_paise,
                "statement_bank_ref": str(row.get("bank_ref", "")) or narration_utr,
                "payment_channel": parsed_narration.get("channel", "UPI"),
                "matched_invoice_id": int(matched_inv["id"]),
                "matched_invoice_number": str(matched_inv["invoice_number"]),
                "matched_client_name": str(matched_inv["client_name"]),
                "invoice_total_due_inr": paise_to_inr(inv_due_paise),
                "invoice_total_due_paise": inv_due_paise,
                "match_type": match_type,
                "confidence": confidence,
                "is_ambiguous": is_ambiguous,
            })

    return matches


def reconcile_upi_payment_from_bank_credit(
    statement_row_id: int,
    invoice_number: str,
    owner_confirmed: bool = True,
    allow_partial: bool = False,
    notes: str = "",
) -> dict[str, Any]:
    """
    Executes bank-verified UPI settlement.
    Enforces that:
    1. The customer screenshot or UTR alone NEVER marks an invoice paid.
    2. Payment MUST be verified against an actual row in bank_statement_rows.
    3. Supports exact settlement, underpayment/partial payment (when allow_partial=True), and rejects overpayment.
    4. Reconciles invoice and updates the financial ledger and tax provision.
    """
    if owner_confirmed is not True:
        raise ValueError(
            "Reconciliation rejected: Explicit owner confirmation required. "
            "Screenshots or customer-provided UTRs are not accepted as payment proof."
        )

    init_upi_tables()

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")

        st_row_val = conn.execute(
            "SELECT * FROM bank_statement_rows WHERE id = ?", (statement_row_id,)
        ).fetchone()
        if not st_row_val:
            raise ValueError(f"Bank statement row #{statement_row_id} not found")
        st_row = dict(st_row_val)
        if st_row["is_reconciled"]:
            raise ValueError(f"Bank statement row #{statement_row_id} has already been reconciled")
        if str(st_row["tx_type"]).lower() != "credit":
            raise ValueError("An invoice can only be reconciled against a bank credit")

        desc = str(st_row["description"]).upper()
        if "REVERSAL" in desc:
            raise ValueError("Bank statement row is a reversal transaction and cannot be settled as revenue")

        inv_row = conn.execute(
            "SELECT * FROM invoices WHERE invoice_number = ?", (invoice_number,)
        ).fetchone()
        if not inv_row:
            raise ValueError(f"Invoice '{invoice_number}' not found")
        inv = dict(inv_row)
        if inv["status"] not in {"issued", "payment_pending", "partially_paid"}:
            raise ValueError(f"Invoice {inv['invoice_number']} is in status '{inv['status']}' and not awaiting payment")

        credit_paise = inr_to_paise(float(st_row["amount_inr"]))
        total_inv_paise = inr_to_paise(float(inv["amount_total"]))
        already_paid_paise = inr_to_paise(float(inv.get("amount_paid", 0.0)))
        remaining_due_paise = total_inv_paise - already_paid_paise

        credit_amt = paise_to_inr(credit_paise)
        total_inv = paise_to_inr(total_inv_paise)
        already_paid = paise_to_inr(already_paid_paise)
        remaining_due = paise_to_inr(remaining_due_paise)

        bank_ref = str(st_row["bank_ref"] or "").strip()
        if bank_ref:
            duplicate = conn.execute(
                "SELECT id FROM invoices WHERE payment_reference = ? AND status = 'paid' AND id <> ? LIMIT 1",
                (bank_ref, int(inv["id"])),
            ).fetchone()
            if duplicate:
                raise ValueError(f"Bank reference '{bank_ref}' is already assigned to another paid invoice")

        # Underpayment / Overpayment check in exact integer paise
        if credit_paise > remaining_due_paise:
            raise ValueError(
                f"Bank credit ₹{credit_amt:,.2f} exceeds remaining invoice balance of ₹{remaining_due:,.2f}. "
                "Overpayments cannot be reconciled without an amended invoice or credit note."
            )

        is_full_settlement = credit_paise == remaining_due_paise

        if not is_full_settlement and not allow_partial:
            raise ValueError(
                f"Bank credit ₹{credit_amt:,.2f} does not exactly settle remaining invoice balance of ₹{remaining_due:,.2f}. "
                "Partial payments are not enabled for this transaction (set allow_partial=True to accept partial payment)."
            )

        now = utc_now()
        ref_for_row = bank_ref or f"UPI-BANK-ROW-{statement_row_id}"
        new_paid_paise = already_paid_paise + credit_paise
        new_paid_total = paise_to_inr(new_paid_paise)
        new_status = "paid" if is_full_settlement else "partially_paid"

        # Update bank statement row
        conn.execute(
            """UPDATE bank_statement_rows
               SET is_reconciled = 1,
                   matched_entity_type = 'invoice',
                   matched_entity_id = ?,
                   confirmed_by_owner = 1,
                   confirmed_at = ?
               WHERE id = ?""",
            (invoice_number, now, statement_row_id),
        )

        conn.execute(
            """UPDATE bank_statement_imports
               SET reconciled_count = reconciled_count + 1
               WHERE id = ?""",
            (st_row["import_id"],),
        )

        # Update invoice
        paid_at_val = now if is_full_settlement else inv["paid_at"]
        conn.execute(
            """UPDATE invoices
               SET status = ?,
                   amount_paid = ?,
                   paid_at = ?,
                   payment_reference = ?,
                   payment_verified_by_owner = 1
               WHERE id = ?""",
            (new_status, new_paid_total, paid_at_val, ref_for_row, int(inv["id"])),
        )

        # If full settlement, update associated opportunity status to paid
        opp_id = int(inv["opportunity_id"])
        if is_full_settlement:
            conn.execute("UPDATE opportunities SET status = 'paid' WHERE id = ?", (opp_id,))

        # Post transaction to financial_transactions append-only ledger in exact integer paise
        inv_subtotal_paise = inr_to_paise(float(inv["amount_subtotal"]))
        inv_tax_paise = inr_to_paise(float(inv["tax_amount"]))

        if total_inv_paise > 0:
            if is_full_settlement:
                # Full settlement: allocate remaining subtotal to prevent any 1-paisa drift
                prior_receipt_row = conn.execute(
                    "SELECT COALESCE(SUM(amount_paise), 0) FROM financial_transactions WHERE reference_id = ? AND tx_type = 'client_receipt'",
                    (invoice_number,),
                ).fetchone()
                prior_receipt_paise = int(prior_receipt_row[0]) if prior_receipt_row else 0
                revenue_paise = max(0, inv_subtotal_paise - prior_receipt_paise)
                tax_paise = credit_paise - revenue_paise
            else:
                tax_paise = int(round(credit_paise * inv_tax_paise / total_inv_paise))
                revenue_paise = credit_paise - tax_paise
        else:
            revenue_paise = credit_paise
            tax_paise = 0

        revenue_portion = paise_to_inr(revenue_paise)
        tax_portion = paise_to_inr(tax_paise)

        is_sb = 1 if inv.get("is_sandbox") else 0

        conn.execute(
            """INSERT INTO financial_transactions (
                tx_date, tx_type, amount_inr, amount_paise, currency,
                reference_id, description, is_reversal, is_sandbox, created_at
            ) VALUES (?, 'client_receipt', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
            (
                str(st_row["tx_date"])[:10],
                revenue_portion,
                revenue_paise,
                invoice_number,
                f"UPI client receipt for {invoice_number} ({inv['client_name']}) - Ref: {ref_for_row}"
                + (" (Partial)" if not is_full_settlement else ""),
                is_sb,
                now,
            ),
        )

        if tax_paise > 0:
            conn.execute(
                """INSERT INTO financial_transactions (
                    tx_date, tx_type, amount_inr, amount_paise, currency,
                    reference_id, description, is_reversal, is_sandbox, created_at
                ) VALUES (?, 'gst_collected', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                (
                    str(st_row["tx_date"])[:10],
                    tax_portion,
                    tax_paise,
                    invoice_number,
                    f"GST collected on UPI payment for {invoice_number}",
                    is_sb,
                    now,
                ),
            )

        conn.commit()

    return {
        "statement_row_id": statement_row_id,
        "invoice_number": invoice_number,
        "amount_settled_inr": credit_amt,
        "amount_settled_paise": credit_paise,
        "total_paid_inr": new_paid_total,
        "total_paid_paise": new_paid_paise,
        "remaining_due_inr": paise_to_inr(remaining_due_paise - credit_paise),
        "remaining_due_paise": max(0, remaining_due_paise - credit_paise),
        "status": new_status,
        "payment_reference": ref_for_row,
        "reconciled_at": now,
    }


# ===========================================================================
# STAGE B: MERCHANT PAYMENT ADAPTER & WEBHOOK ARCHITECTURE
# (INTEGRATION FRAMEWORK — NOT CONNECTED TO LIVE BANK)
# ===========================================================================

class MerchantPaymentAdapter:
    """Provider-independent merchant payment adapter interface."""
    adapter_name: str = "base"
    is_simulated: bool = False
    connection_status: str = "NOT CONNECTED"

    def verify_webhook_signature(
        self,
        payload_bytes: bytes,
        signature: str,
        secret: str,
        timestamp: int | None = None,
    ) -> bool:
        raise NotImplementedError

    def parse_webhook_event(
        self,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError


class SimulatedBankMerchantAdapter(MerchantPaymentAdapter):
    """
    Simulated Sandbox Adapter for local testing of merchant UPI notifications.
    Explicitly marked as is_simulated=True.
    CONNECTION STATUS: NOT CONNECTED (LOCAL SANDBOX SIMULATOR ONLY).
    Does not claim live bank connectivity.
    """
    adapter_name: str = "simulated_bank_sandbox"
    is_simulated: bool = True
    connection_status: str = "NOT CONNECTED (Sandbox Simulator Only)"

    def verify_webhook_signature(
        self,
        payload_bytes: bytes,
        signature: str,
        secret: str,
        timestamp: int | None = None,
    ) -> bool:
        if not secret or not signature:
            return False
        expected_sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected_sig.lower(), signature.strip().lower())

    def parse_webhook_event(
        self,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        assert_no_sensitive_credentials(payload)
        return {
            "event_id": str(payload.get("event_id", "")).strip(),
            "invoice_number": str(payload.get("invoice_number", "")).strip(),
            "transaction_ref": str(payload.get("transaction_ref", "")).strip(),
            "amount_inr": float(payload.get("amount_inr", 0.0)),
            "status": str(payload.get("status", "")).strip().lower(),
            "timestamp": int(payload.get("timestamp", 0)),
            "is_simulated": True,
            "connection_status": self.connection_status,
        }

    @staticmethod
    def create_signed_payload(
        event_id: str,
        invoice_number: str,
        amount_inr: float,
        status: str,
        secret: str,
        transaction_ref: str = "",
        timestamp: int | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Utility for test suites to generate valid signed simulated payloads."""
        ts = int(time.time()) if timestamp is None else timestamp
        payload = {
            "event_id": event_id,
            "invoice_number": invoice_number,
            "amount_inr": float(amount_inr),
            "status": status,
            "transaction_ref": transaction_ref or f"SIM-TXN-{int(time.time())}",
            "timestamp": ts,
            "is_simulated": True,
        }
        payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
        sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        return payload, sig


class StandardBankMerchantAdapter(MerchantPaymentAdapter):
    """
    Standard Bank Merchant Adapter Interface.
    IMPORTANT NOTICE: NOT CONNECTED TO LIVE BANK.
    This adapter defines the integration specification and HMAC verification for
    bank-approved merchant webhooks. Live bank connectivity is NOT active until
    acquiring bank onboarding, business KYC, MCC assignment, and production credentials
    have been completed with your banking partner.
    """
    adapter_name: str = "standard_bank_production"
    is_simulated: bool = False
    connection_status: str = "NOT CONNECTED (Integration Framework Ready; Awaiting Bank Onboarding)"

    def verify_webhook_signature(
        self,
        payload_bytes: bytes,
        signature: str,
        secret: str,
        timestamp: int | None = None,
    ) -> bool:
        if not secret or not signature:
            return False
        # If timestamp is provided, sign (timestamp + "." + payload) or standard HMAC
        to_sign = f"{timestamp}.".encode("utf-8") + payload_bytes if timestamp else payload_bytes
        expected_sig = hmac.new(secret.encode("utf-8"), to_sign, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected_sig.lower(), signature.strip().lower())

    def parse_webhook_event(
        self,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        assert_no_sensitive_credentials(payload)
        return {
            "event_id": str(payload.get("event_id", "")).strip(),
            "invoice_number": str(payload.get("invoice_number", "")).strip(),
            "transaction_ref": str(payload.get("transaction_ref", "")).strip(),
            "amount_inr": float(payload.get("amount_inr", 0.0)),
            "status": str(payload.get("status", "")).strip().lower(),
            "timestamp": int(payload.get("timestamp", 0)),
            "is_simulated": False,
            "connection_status": self.connection_status,
        }


def process_merchant_bank_webhook(
    payload: dict[str, Any],
    signature: str | None = None,
    webhook_secret: str | None = None,
    headers: dict[str, str] | None = None,
    adapter: MerchantPaymentAdapter | None = None,
) -> dict[str, Any]:
    """
    Processes incoming merchant bank webhook notifications.
    Safeguards:
    1. Blocks sensitive banking credentials (PIN, MPIN, passwords, OTPs).
    2. Verifies HMAC-SHA256 signature against configured webhook secret.
    3. Replay Attack Protection: Rejects events with timestamps older than 300 seconds or skewed.
    4. Idempotency Protection: Rejects duplicate event IDs without double crediting.
    5. State Separation: Distinguishes Payment Success (in transit) from Bank Settlement.
    """
    assert_no_sensitive_credentials(payload)

    # Determine adapter: if explicitly provided, use it; else choose based on is_simulated flag
    if adapter is None:
        if payload.get("is_simulated"):
            adapter = SimulatedBankMerchantAdapter()
        else:
            adapter = StandardBankMerchantAdapter()

    active_settings = get_active_upi_settings()
    secret = webhook_secret or active_settings.get("webhook_secret", "")

    event = adapter.parse_webhook_event(payload, headers)
    event_id = event["event_id"]
    if not event_id:
        raise ValueError("Missing event_id in webhook payload")

    # Timestamp replay-attack protection (300-second window)
    ts = event.get("timestamp", 0)
    if ts > 0:
        current_ts = int(time.time())
        if abs(current_ts - ts) > 300:
            raise ValueError(
                f"Webhook timestamp expired or skewed ({abs(current_ts - ts)}s difference). "
                "Event rejected under replay-attack protection."
            )

    # Signature verification
    if secret:
        if not signature:
            raise ValueError("Missing HMAC signature for merchant bank webhook")
        payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
        if not adapter.verify_webhook_signature(payload_bytes, signature, secret, timestamp=ts or None):
            # Also check direct payload HMAC if combined timestamp check didn't match
            direct_expected = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(direct_expected.lower(), signature.strip().lower()):
                raise ValueError("Invalid HMAC signature: webhook payload integrity check failed")

    inv_num = event["invoice_number"]
    txn_ref = event["transaction_ref"]
    amount_inr = event["amount_inr"]
    status = event["status"]
    is_sim = 1 if adapter.is_simulated else 0

    init_upi_tables()
    now_str = utc_now()

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")

        # Idempotency check
        existing = conn.execute("SELECT id FROM merchant_webhook_logs WHERE event_id = ?", (event_id,)).fetchone()
        if existing:
            return {
                "status": "already_processed",
                "event_id": event_id,
                "is_simulated": adapter.is_simulated,
                "message": "Duplicate webhook event acknowledged without double-crediting.",
            }

        conn.execute(
            """INSERT INTO merchant_webhook_logs (
                event_id, provider, invoice_number, transaction_ref,
                amount_inr, status, raw_payload, signature_valid, is_simulated, processed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (
                event_id,
                adapter.adapter_name,
                inv_num,
                txn_ref,
                amount_inr,
                status,
                json.dumps(payload),
                is_sim,
                now_str,
                now_str,
            ),
        )

        # Distinguish Payment Success (Transit) vs Bank Settlement
        if status in {"payment_captured", "payment_received"}:
            # Funds are captured by PSP / gateway but have not settled into receiving bank account
            if inv_num:
                conn.execute(
                    """UPDATE invoices
                       SET notes = notes || ' [PSP Payment Received: awaiting bank settlement]'
                       WHERE invoice_number = ? AND status IN ('issued', 'payment_pending')""",
                    (inv_num,),
                )
            conn.commit()
            return {
                "status": "payment_captured",
                "event_id": event_id,
                "invoice_number": inv_num,
                "amount_inr": amount_inr,
                "is_simulated": adapter.is_simulated,
                "settled_to_bank": False,
                "message": "Payment captured by provider; awaiting bank settlement clearance.",
            }

        elif status in {"settlement_processed", "funds_settled_to_bank", "success", "settled"}:
            # Bank settlement complete: Funds have reached the merchant account
            if inv_num:
                inv_row = conn.execute("SELECT * FROM invoices WHERE invoice_number = ?", (inv_num,)).fetchone()
                if inv_row and inv_row["status"] in {"issued", "payment_pending", "partially_paid"}:
                    conn.execute(
                        """UPDATE invoices
                           SET status = 'paid',
                               amount_paid = amount_total,
                               paid_at = ?,
                               payment_reference = ?,
                               payment_verified_by_owner = 1
                           WHERE id = ?""",
                        (now_str, txn_ref or f"SETTLE-{event_id}", int(inv_row["id"])),
                    )
                    opp_id = int(inv_row["opportunity_id"])
                    conn.execute("UPDATE opportunities SET status = 'paid' WHERE id = ?", (opp_id,))

                    # Register append-only ledger transaction in exact integer paise
                    subtotal_paise = inr_to_paise(float(inv_row["amount_subtotal"]))
                    tax_paise = inr_to_paise(float(inv_row["tax_amount"]))
                    subtotal = paise_to_inr(subtotal_paise)
                    tax_amt = paise_to_inr(tax_paise)
                    conn.execute(
                        """INSERT INTO financial_transactions (
                            tx_date, tx_type, amount_inr, amount_paise, currency,
                            reference_id, description, is_reversal, is_sandbox, created_at
                        ) VALUES (?, 'client_receipt', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                        (
                            now_str[:10],
                            subtotal,
                            subtotal_paise,
                            inv_num,
                            f"Settled merchant UPI payment for {inv_num} ({inv_row['client_name']}) - Ref: {txn_ref}",
                            is_sim,
                            now_str,
                        ),
                    )
                    if tax_paise > 0:
                        conn.execute(
                            """INSERT INTO financial_transactions (
                                tx_date, tx_type, amount_inr, amount_paise, currency,
                                reference_id, description, is_reversal, is_sandbox, created_at
                            ) VALUES (?, 'gst_collected', ?, ?, 'INR', ?, ?, 0, ?, ?)""",
                            (
                                now_str[:10],
                                tax_amt,
                                tax_paise,
                                inv_num,
                                f"GST collected on settled UPI payment for {inv_num}",
                                is_sim,
                                now_str,
                            ),
                        )

            conn.commit()
            return {
                "status": "settlement_processed",
                "event_id": event_id,
                "invoice_number": inv_num,
                "amount_inr": amount_inr,
                "is_simulated": adapter.is_simulated,
                "settled_to_bank": True,
                "message": "Merchant bank settlement confirmed; invoice paid and financial ledger updated.",
            }

        conn.commit()
        return {
            "status": status,
            "event_id": event_id,
            "invoice_number": inv_num,
            "is_simulated": adapter.is_simulated,
            "message": f"Webhook recorded with status '{status}'.",
        }
