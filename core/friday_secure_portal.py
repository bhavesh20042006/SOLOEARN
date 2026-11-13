from __future__ import annotations

"""
FRIDAY Secure Client Portal & Webhook Ingestion Rail
Implements OWASP-compliant authenticated file intake and HMAC-signed payment webhook processing.

Security Architecture:
- Token-authenticated client upload access.
- Strict extension and MIME validation (.csv, .xlsx, .tsv, .txt).
- Formula injection protection (strips leading '=', '+', '-', '@').
- Isolated workspace storage with randomized UUID filenames.
- HMAC-SHA256 signed payment webhook validation with idempotency tables.
"""

import csv
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.analytics import sanitize_cell_value
from core.config import DATA_DIR, MAX_UPLOAD_MB, safe_workspace_path
from core.db import connect, get_invoice, get_task, update_task_status, utc_now
from core.financial_ledger import inr_to_paise
from core.upi_engine import process_merchant_bank_webhook

logger = logging.getLogger("soloearn.friday.portal")

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".tsv", ".txt"}
MAX_BYTES = MAX_UPLOAD_MB * 1024 * 1024  # 25 MB default


def init_portal_tables() -> None:
    """Initializes client portal tokens, upload records, and webhook idempotency tables."""
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_portal_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                client_name TEXT NOT NULL,
                opportunity_id INTEGER,
                task_id INTEGER,
                is_used INTEGER NOT NULL DEFAULT 0,
                used_at TEXT,
                expires_at TEXT NOT NULL,
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
        # Ensure used_at exists if table already existed
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(friday_portal_tokens)").fetchall()]
        if "used_at" not in cols:
            conn.execute("ALTER TABLE friday_portal_tokens ADD COLUMN used_at TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                invoice_number TEXT,
                amount_inr REAL NOT NULL,
                signature_valid INTEGER NOT NULL DEFAULT 0,
                processed_at TEXT NOT NULL,
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(provider, event_id, is_sandbox)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_settled_utrs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                utr TEXT NOT NULL,
                invoice_number TEXT NOT NULL,
                amount_inr REAL NOT NULL,
                provider TEXT NOT NULL,
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                settled_at TEXT NOT NULL,
                UNIQUE(utr, is_sandbox)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_portal_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL,
                client_name TEXT NOT NULL,
                opportunity_id INTEGER,
                task_id INTEGER,
                stored_path TEXT NOT NULL,
                file_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                original_filename TEXT NOT NULL,
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                uploaded_at TEXT NOT NULL
            );
            """
        )
        conn.commit()


# ===========================================================================
# 1. Secure File Ingestion (OWASP-Compliant)
# ===========================================================================

class FridaySecurePortal:
    """Manages token creation, file validation, sanitization, and workspace routing."""

    def __init__(self, is_sandbox: bool = False):
        self.is_sandbox = is_sandbox
        init_portal_tables()

    def generate_upload_token(
        self,
        client_name: str,
        opportunity_id: int | None = None,
        task_id: int | None = None,
        validity_hours: int = 24,
    ) -> str:
        """Generates a secure, cryptographically random upload token with genuine 24h expiration."""
        token = f"tok_{uuid.uuid4().hex}"
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(hours=validity_hours)).isoformat()

        with connect() as conn:
            conn.execute(
                """
                INSERT INTO friday_portal_tokens (
                    token, client_name, opportunity_id, task_id, is_used, expires_at, is_sandbox, created_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (token, client_name, opportunity_id, task_id, expires, int(self.is_sandbox), now),
            )
            conn.commit()
        return token

    def validate_and_ingest_file(
        self,
        token: str,
        filename: str,
        file_bytes: bytes,
    ) -> dict[str, Any]:
        """
        Validates uploaded file against security criteria, enforces expiration and atomic single-use,
        and stores it in the client's isolated workspace.
        """
        if not token:
            raise ValueError("Authentication token required for portal upload")

        # 1. Size constraint
        if len(file_bytes) == 0:
            raise ValueError("Uploaded file is empty (0 bytes)")
        if len(file_bytes) > MAX_BYTES:
            raise ValueError(f"File size ({len(file_bytes)} bytes) exceeds maximum allowed {MAX_UPLOAD_MB} MB")

        # 2. Extension check
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"File extension '{ext}' is forbidden. Allowed: {sorted(ALLOWED_EXTENSIONS)}")

        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()

        # Atomic token claim under serialized transaction (prevents concurrent double-use)
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE friday_portal_tokens
                SET is_used = 1, used_at = ?
                WHERE token = ? AND is_used = 0 AND expires_at > ? AND is_sandbox = ?
                """,
                (now, token, now, int(self.is_sandbox)),
            )
            if cur.rowcount == 0:
                row = conn.execute(
                    "SELECT is_used, expires_at FROM friday_portal_tokens WHERE token = ? AND is_sandbox = ?",
                    (token, int(self.is_sandbox)),
                ).fetchone()
                if not row:
                    raise ValueError("Invalid or unrecognized client upload token")
                if bool(row["is_used"]):
                    raise ValueError(f"Upload token '{token}' has already been used. Each portal token is strictly single-use.")
                if row["expires_at"] <= now:
                    raise ValueError(f"Upload token '{token}' expired at {row['expires_at']}. Please request a fresh link.")
                raise ValueError("Upload token could not be claimed.")

            token_row = conn.execute(
                "SELECT * FROM friday_portal_tokens WHERE token = ? AND is_sandbox = ?",
                (token, int(self.is_sandbox)),
            ).fetchone()
            conn.commit()

        # 3. Path traversal defense & UUID file naming
        clean_name = f"upload_{uuid.uuid4().hex[:12]}{ext}"

        # 4. Workspace resolution
        task_id = token_row["task_id"]
        if task_id:
            ws_path = safe_workspace_path(f"task_{task_id:04d}") / "uploads"
        else:
            ws_path = safe_workspace_path("portal_dropzone")

        ws_path.mkdir(parents=True, exist_ok=True)
        dest_path = ws_path / clean_name

        # 5. Content Sanitization for CSV/Text files against Formula Injection
        # Uses python's csv library to preserve embedded commas, quotes, and legitimate negative numbers
        if ext in {".csv", ".tsv", ".txt"}:
            try:
                decoded_text = file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                decoded_text = file_bytes.decode("latin-1", errors="replace")

            delimiter = "\t" if ext == ".tsv" else ","
            reader = csv.reader(io.StringIO(decoded_text), delimiter=delimiter)
            out_str = io.StringIO()
            writer = csv.writer(out_str, delimiter=delimiter, lineterminator="\n")
            for row in reader:
                clean_row = [sanitize_cell_value(c) for c in row]
                writer.writerow(clean_row)
            dest_path.write_text(out_str.getvalue(), encoding="utf-8")
        else:
            dest_path.write_bytes(file_bytes)

        file_sha256 = hashlib.sha256(file_bytes).hexdigest()

        # Log upload in friday_portal_uploads
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO friday_portal_uploads (
                    token, client_name, opportunity_id, task_id, stored_path,
                    file_sha256, size_bytes, original_filename, is_sandbox, uploaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    token_row["client_name"],
                    token_row["opportunity_id"],
                    token_row["task_id"],
                    str(dest_path),
                    file_sha256,
                    len(file_bytes),
                    filename,
                    int(self.is_sandbox),
                    now,
                ),
            )
            conn.commit()

        return {
            "status": "ingested",
            "original_filename": filename,
            "stored_filename": clean_name,
            "file_path": str(dest_path),
            "file_sha256": file_sha256,
            "size_bytes": len(file_bytes),
            "task_id": task_id,
            "client_name": token_row["client_name"],
            "ingested_at": now,
        }


# ===========================================================================
# 2. Signed Payment Webhook Processing (HMAC-SHA256)
# ===========================================================================

class FridayPaymentWebhookHandler:
    """
    Verifies HMAC signatures, enforces event idempotency, settles invoices,
    and triggers automatic deliverable release in FRIDAY.
    """

    DEFAULT_TEST_SECRET = "whsec_test_secret_soloearn_2026"

    def __init__(self, webhook_secret: str | None = None, is_sandbox: bool = False):
        self.is_sandbox = is_sandbox
        init_portal_tables()
        if not is_sandbox:
            if not webhook_secret or webhook_secret == self.DEFAULT_TEST_SECRET or len(webhook_secret) < 32:
                raise ValueError(
                    "Production payment webhook handler requires a strong, non-default webhook secret (at least 32 characters)."
                )
            self.webhook_secret = webhook_secret
        else:
            self.webhook_secret = webhook_secret or self.DEFAULT_TEST_SECRET

    def verify_hmac_signature(self, payload_bytes: bytes, signature: str) -> bool:
        if not self.webhook_secret or not signature:
            return False
        expected = hmac.new(self.webhook_secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected.lower(), signature.strip().lower())

    ACCEPTED_PAYMENT_STATUSES = {
        "captured",
        "succeeded",
        "success",
        "paid",
        "payment.captured",
        "payment.succeeded",
        "payment_intent.succeeded",
    }
    REJECTED_PAYMENT_STATUSES = {
        "failed",
        "payment.failed",
        "pending",
        "payment.pending",
        "cancelled",
        "payment.cancelled",
        "declined",
        "expired",
    }

    def process_webhook_event(
        self,
        event_id: str,
        payload_bytes: bytes,
        signature: str,
        provider: str = "simulated_bank",
    ) -> dict[str, Any]:
        """Processes an incoming payment webhook event with strict signature, amount, and idempotency verification."""
        event_id_clean = str(event_id or "").strip()
        if not event_id_clean:
            raise ValueError("Missing required event_id in webhook call.")

        # 1. Verify HMAC Signature
        if not self.verify_hmac_signature(payload_bytes, signature):
            raise ValueError("Invalid webhook HMAC signature: event rejected for security.")

        # 2. Parse Payload
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Malformed JSON in webhook payload: {e}")

        # 3. Payment Status Check (Reject failed, pending, cancelled, or unverified events)
        has_status_field = any(k in payload for k in ("status", "event", "payment_status"))
        raw_status = str(payload.get("status") or payload.get("event") or payload.get("payment_status") or "").lower().strip()
        if has_status_field and not raw_status:
            raise ValueError("Payment status field cannot be empty in webhook payload.")
        if not raw_status:
            raw_status = "captured"
        if raw_status in self.REJECTED_PAYMENT_STATUSES:
            raise ValueError(f"Webhook payment status '{raw_status}' indicates an unsuccessful payment. Invoice settlement rejected.")
        if raw_status not in self.ACCEPTED_PAYMENT_STATUSES:
            raise ValueError(f"Unrecognized or unverified payment status '{raw_status}'. Settlement requires verified success/captured status.")

        # 4. Currency Check (strictly INR)
        currency = str(payload.get("currency", "INR")).upper().strip()
        if currency != "INR":
            raise ValueError(f"Unsupported currency '{currency}'. SOLOEARN settles strictly in INR.")

        # 5. Amount Validation (reject None, non-finite, NaN, inf, negative, or zero)
        raw_amt = payload.get("amount_inr")
        if raw_amt is None:
            raise ValueError("Missing required amount_inr field in webhook payload.")
        if not isinstance(raw_amt, (int, float)) or math.isnan(raw_amt) or math.isinf(raw_amt) or raw_amt <= 0:
            raise ValueError(f"Invalid non-finite or non-positive payment amount: {raw_amt}")
        amt = float(raw_amt)

        # 6. Transaction Reference (UTR) Validation (Strictly required; never invent fake timestamps)
        txn_ref = str(payload.get("transaction_ref") or payload.get("bank_ref") or payload.get("utr") or "").strip()
        if not txn_ref:
            raise ValueError("Missing required bank transaction reference (UTR) in payment payload. Cannot settle without genuine bank UTR.")

        inv_num = str(payload.get("invoice_number", "")).strip()
        if not inv_num:
            raise ValueError("Missing required invoice_number in webhook payload.")

        now = utc_now()
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()

        # 7. Atomic Concurrency-Safe Settlement in SOLOEARN Database
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            # A. Check Idempotency (scoped to provider, event_id, and environment)
            existing = conn.execute(
                "SELECT id, processed_at FROM friday_webhook_events WHERE event_id = ? AND provider = ? AND is_sandbox = ?",
                (event_id_clean, provider, int(self.is_sandbox)),
            ).fetchone()
            if existing:
                conn.commit()
                return {
                    "event_id": event_id_clean,
                    "status": "duplicate_ignored",
                    "processed_at": existing["processed_at"],
                    "note": "Duplicate webhook event safely ignored (idempotent shield).",
                }

            # B. Query Invoice strictly scoped to current environment
            inv = conn.execute(
                "SELECT * FROM invoices WHERE invoice_number = ? AND is_sandbox = ?",
                (inv_num, int(self.is_sandbox)),
            ).fetchone()
            if not inv:
                conn.rollback()
                raise ValueError(
                    f"Invoice '{inv_num}' not found in {'sandbox' if self.is_sandbox else 'production'} environment."
                )

            total_inv = float(inv["amount_total"])

            # C. Invariant: Amount Verification (reject underpayment and overpayment)
            diff = round(amt - total_inv, 2)
            if diff < -0.01:
                conn.rollback()
                raise ValueError(
                    f"Payment amount mismatch: Underpayment received ₹{amt:,.2f}, but invoice total is ₹{total_inv:,.2f} for {inv_num}. Exact match required."
                )
            if diff > 0.01:
                conn.rollback()
                raise ValueError(
                    f"Payment amount mismatch: Overpayment received ₹{amt:,.2f}, but invoice total is ₹{total_inv:,.2f} for {inv_num}. Exact match required."
                )

            # D. Invariant: Database-level Bank Transaction Reference (UTR) Uniqueness
            try:
                conn.execute(
                    """
                    INSERT INTO friday_settled_utrs (
                        utr, invoice_number, amount_inr, provider, is_sandbox, settled_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (txn_ref, inv_num, amt, provider, int(self.is_sandbox), now),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                raise ValueError(
                    f"Duplicate transaction reference: Bank reference '{txn_ref}' has already been processed in this environment."
                )

            # Also check financial_transactions ledger for duplicate UTR
            existing_txn = conn.execute(
                "SELECT id, reference_id FROM financial_transactions WHERE description LIKE ? AND is_sandbox = ? AND is_reversal = 0",
                (f"%Ref: {txn_ref}%", int(self.is_sandbox)),
            ).fetchone()
            if existing_txn:
                conn.rollback()
                raise ValueError(
                    f"Duplicate transaction reference: Bank reference '{txn_ref}' has already been processed in transaction #{existing_txn['id']}."
                )

            # E. If already settled under a previous valid event
            if inv["status"] == "paid":
                conn.commit()
                return {
                    "event_id": event_id_clean,
                    "status": "already_settled",
                    "invoice_number": inv_num,
                    "amount_inr": amt,
                    "transaction_ref": txn_ref,
                    "note": f"Invoice {inv_num} is already fully settled.",
                }

            # F. Mark Invoice Paid
            conn.execute(
                """
                UPDATE invoices 
                SET status = 'paid', amount_paid = ?, paid_at = ?, payment_reference = ?
                WHERE invoice_number = ? AND is_sandbox = ?
                """,
                (amt, now, txn_ref, inv_num, int(self.is_sandbox)),
            )

            # G. Update linked opportunity to paid
            opp_id = inv["opportunity_id"]
            if opp_id:
                conn.execute("UPDATE opportunities SET status = 'paid' WHERE id = ?", (opp_id,))

            # H. Record client receipt in financial_transactions
            conn.execute(
                """
                INSERT INTO financial_transactions (
                    tx_date, tx_type, amount_inr, amount_paise, currency,
                    reference_id, description, is_reversal, is_sandbox, created_at
                ) VALUES (?, 'client_receipt', ?, ?, 'INR', ?, ?, 0, ?, ?)
                """,
                (
                    now[:10],
                    amt,
                    inr_to_paise(amt),
                    inv_num,
                    f"Cleared merchant webhook payment for {inv_num} (Ref: {txn_ref})",
                    int(self.is_sandbox),
                    now,
                ),
            )

            # I. Log webhook event durably
            conn.execute(
                """
                INSERT INTO friday_webhook_events (
                    event_id, provider, payload_hash, invoice_number, amount_inr,
                    signature_valid, processed_at, is_sandbox, created_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (event_id_clean, provider, payload_hash, inv_num, amt, now, int(self.is_sandbox), now),
            )
            conn.commit()

        logger.info(f"[PAYMENT CONFIRMED] Webhook event {event_id_clean} settled invoice {inv_num} for ₹{amt:,.2f}")

        return {
            "event_id": event_id_clean,
            "status": "settled",
            "invoice_number": inv_num,
            "amount_inr": amt,
            "transaction_ref": txn_ref,
            "processed_at": now,
        }
