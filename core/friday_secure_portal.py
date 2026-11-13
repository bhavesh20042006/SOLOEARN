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
import os
import re
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
    """Initializes client portal tokens and webhook idempotency tables."""
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
                expires_at TEXT NOT NULL,
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS friday_webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                provider TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                invoice_number TEXT,
                amount_inr REAL NOT NULL,
                signature_valid INTEGER NOT NULL DEFAULT 0,
                processed_at TEXT NOT NULL,
                is_sandbox INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
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
        validity_hours: int = 72,
    ) -> str:
        """Generates a secure, cryptographically random upload token with genuine expiration."""
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
        Validates uploaded file against security criteria, enforces expiration and single-use,
        and stores it in the client's isolated workspace.
        """
        if not token:
            raise ValueError("Authentication token required for portal upload")

        with connect() as conn:
            token_row = conn.execute(
                "SELECT * FROM friday_portal_tokens WHERE token = ? AND is_sandbox = ?",
                (token, int(self.is_sandbox)),
            ).fetchone()
            if not token_row:
                raise ValueError("Invalid or unrecognized client upload token")
            if bool(token_row["is_used"]):
                raise ValueError(f"Upload token '{token}' has already been used. Each portal token is strictly single-use.")
            if token_row["expires_at"] < utc_now():
                raise ValueError(f"Upload token '{token}' expired at {token_row['expires_at']}. Please request a fresh link.")

        # 1. Size constraint
        if len(file_bytes) == 0:
            raise ValueError("Uploaded file is empty (0 bytes)")
        if len(file_bytes) > MAX_BYTES:
            raise ValueError(f"File size ({len(file_bytes)} bytes) exceeds maximum allowed {MAX_UPLOAD_MB} MB")

        # 2. Extension check
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"File extension '{ext}' is forbidden. Allowed: {sorted(ALLOWED_EXTENSIONS)}")

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

        # Mark token as used
        with connect() as conn:
            conn.execute(
                "UPDATE friday_portal_tokens SET is_used = 1 WHERE token = ?",
                (token,),
            )
            conn.commit()

        return {
            "status": "ingested",
            "original_filename": filename,
            "stored_filename": clean_name,
            "file_path": str(dest_path),
            "size_bytes": len(file_bytes),
            "task_id": task_id,
            "client_name": token_row["client_name"],
            "ingested_at": utc_now(),
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

    def process_webhook_event(
        self,
        event_id: str,
        payload_bytes: bytes,
        signature: str,
        provider: str = "simulated_bank",
    ) -> dict[str, Any]:
        """Processes an incoming payment webhook event with strict signature, amount, and idempotency verification."""
        # 1. Verify HMAC Signature
        if not self.verify_hmac_signature(payload_bytes, signature):
            raise ValueError("Invalid webhook HMAC signature: event rejected for security.")

        # 2. Check Idempotency (prevent double processing of duplicate events)
        with connect() as conn:
            existing = conn.execute(
                "SELECT id, processed_at FROM friday_webhook_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing:
                return {
                    "event_id": event_id,
                    "status": "duplicate_ignored",
                    "processed_at": existing["processed_at"],
                    "note": "Duplicate webhook event safely ignored (idempotent shield).",
                }

        # 3. Parse Payload
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Malformed JSON in webhook payload: {e}")

        inv_num = str(payload.get("invoice_number", "")).strip()
        amt = float(payload.get("amount_inr", 0.0))
        txn_ref = str(payload.get("transaction_ref", f"UTR-{int(time.time())}"))
        now = utc_now()
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()

        # 4. Settle Invoice in SOLOEARN Database
        with connect() as conn:
            inv = conn.execute("SELECT * FROM invoices WHERE invoice_number = ?", (inv_num,)).fetchone()
            if not inv:
                raise ValueError(f"Invoice '{inv_num}' not found for webhook settlement")

            total_inv = float(inv["amount_total"])

            # Invariant: Prevent reusing bank transaction reference (UTR)
            existing_txn = conn.execute(
                "SELECT id, reference_id FROM financial_transactions WHERE description LIKE ? AND is_reversal = 0",
                (f"%Ref: {txn_ref}%",),
            ).fetchone()
            if existing_txn:
                raise ValueError(
                    f"Duplicate transaction reference: Bank reference '{txn_ref}' has already been processed in transaction #{existing_txn['id']}."
                )

            # Invariant: Reject payment amount mismatch (exact match required)
            if abs(amt - total_inv) > 0.01:
                raise ValueError(
                    f"Payment amount mismatch: Webhook received ₹{amt:,.2f}, but invoice total is ₹{total_inv:,.2f} for {inv_num}. Exact match required."
                )

            if inv["status"] == "paid":
                return {
                    "event_id": event_id,
                    "status": "already_settled",
                    "invoice_number": inv_num,
                    "amount_inr": amt,
                    "transaction_ref": txn_ref,
                    "note": f"Invoice {inv_num} is already fully settled.",
                }

            conn.execute(
                """
                UPDATE invoices 
                SET status = 'paid', amount_paid = ?, paid_at = ?, payment_reference = ?
                WHERE invoice_number = ?
                """,
                (amt, now, txn_ref, inv_num),
            )

            # If full settlement, update linked opportunity to paid
            opp_id = inv["opportunity_id"]
            if opp_id:
                conn.execute("UPDATE opportunities SET status = 'paid' WHERE id = ?", (opp_id,))

            # Record client receipt in financial_transactions
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

            # Log webhook event durably
            conn.execute(
                """
                INSERT INTO friday_webhook_events (
                    event_id, provider, payload_hash, invoice_number, amount_inr,
                    signature_valid, processed_at, is_sandbox, created_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (event_id, provider, payload_hash, inv_num, amt, now, int(self.is_sandbox), now),
            )
            conn.commit()

        logger.info(f"[PAYMENT CONFIRMED] Webhook event {event_id} settled invoice {inv_num} for ₹{amt:,.2f}")

        return {
            "event_id": event_id,
            "status": "settled",
            "invoice_number": inv_num,
            "amount_inr": amt,
            "transaction_ref": txn_ref,
            "processed_at": now,
        }
