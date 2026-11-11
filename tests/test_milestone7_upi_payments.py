from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path

import core.config
from core.db import (
    add_opportunity,
    connect,
    create_invoice,
    create_task,
    get_invoice,
    init_db,
    utc_now,
)
from core.financial_ledger import (
    get_cash_and_reserves_summary,
    import_bank_statement_csv,
    reconcile_statement_row,
)
from core.invoice_generator import render_invoice_html
from core.upi_engine import (
    SecurityViolationError,
    assert_no_sensitive_credentials,
    build_upi_payment_url,
    generate_invoice_upi_qr,
    generate_upi_qr_base64,
    generate_upi_qr_bytes,
    get_active_upi_settings,
    get_upi_payment_centre_summary,
    init_upi_tables,
    parse_upi_payment_url,
    process_merchant_bank_webhook,
    reconcile_upi_payment_from_bank_credit,
    save_upi_settings,
    scan_and_match_statement_for_upi,
    validate_vpa,
)


class Milestone7UPIPaymentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orig_db_path = core.config.DB_PATH
        self.orig_qr_dir = core.config.UPI_QR_DIR
        core.config.DB_PATH = Path(self.tmp_dir.name) / "test_m7.sqlite3"
        core.config.UPI_QR_DIR = Path(self.tmp_dir.name) / "upi_qr"
        init_db()
        init_upi_tables()

    def tearDown(self):
        core.config.DB_PATH = self.orig_db_path
        core.config.UPI_QR_DIR = self.orig_qr_dir
        self.tmp_dir.cleanup()

    def _create_sample_invoice(self, amount: float = 5000.0) -> dict:
        opp_id = add_opportunity({
            "title": "UPI Test Project",
            "source": "verified_direct",
            "source_permitted": True,
            "amount_inr": amount,
            "estimated_hours": 2.0,
            "skill_match": 0.9,
            "payment_confidence": 0.9,
            "risk_level": 1.0,
        }, score=9.0)
        with connect() as conn:
            conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opp_id,))
        task_id = create_task(
            title="Analysis Task",
            task_type="csv_business_analysis",
            workspace="ws_test",
            opportunity_id=opp_id,
        )
        with connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='submission_ready', artifact_path='deliverables.zip' WHERE id=?",
                (task_id,),
            )
        inv = create_invoice(
            opportunity_id=opp_id,
            client_name="Test Client D2C",
            service_tier="data_analytics",
            amount_subtotal=amount,
            tax_rate=0.0,
            task_id=task_id,
        )
        return inv

    def test_vpa_validation(self):
        # Valid VPAs
        self.assertEqual(validate_vpa("user@okhdfcbank"), "user@okhdfcbank")
        self.assertEqual(validate_vpa("merchant.store@icici"), "merchant.store@icici")
        self.assertEqual(validate_vpa("soloearn@upi"), "soloearn@upi")

        # Invalid VPAs
        with self.assertRaises(ValueError):
            validate_vpa("")
        with self.assertRaises(ValueError):
            validate_vpa("invalidvpa")
        with self.assertRaises(ValueError):
            validate_vpa("@okhdfcbank")
        with self.assertRaises(ValueError):
            validate_vpa("user@")

    def test_npci_upi_url_builder_and_parser(self):
        url = build_upi_payment_url(
            payee_vpa="founder@okaxis",
            payee_name="SOLOEARN Operations",
            amount_inr=5000.0,
            transaction_ref="INV-2026-0001",
            transaction_note="Invoice INV-2026-0001",
            merchant_code="8931",
        )
        self.assertTrue(url.startswith("upi://pay?"))
        self.assertIn("pa=founder%40okaxis", url)
        self.assertIn("pn=SOLOEARN+Operations", url)
        self.assertIn("am=5000.00", url)
        self.assertIn("cu=INR", url)
        self.assertIn("tr=INV-2026-0001", url)
        self.assertIn("mc=8931", url)
        self.assertIn("mode=02", url)

        # Parse back
        parsed = parse_upi_payment_url(url)
        self.assertEqual(parsed["pa"], "founder@okaxis")
        self.assertEqual(parsed["pn"], "SOLOEARN Operations")
        self.assertEqual(parsed["am"], 5000.0)
        self.assertEqual(parsed["cu"], "INR")
        self.assertEqual(parsed["tr"], "INV-2026-0001")
        self.assertEqual(parsed["mc"], "8931")

    def test_upi_url_builder_rejects_invalid_inputs(self):
        with self.assertRaises(ValueError):
            build_upi_payment_url("user@okaxis", "Owner", -500.0, "REF1")
        with self.assertRaises(ValueError):
            build_upi_payment_url("user@okaxis", "Owner", 0.0, "REF1")
        with self.assertRaises(ValueError):
            build_upi_payment_url("user@okaxis", "Owner", float("inf"), "REF1")
        with self.assertRaises(ValueError):
            build_upi_payment_url("user@okaxis", "Owner", 500000.0, "REF1")  # exceeds limit
        with self.assertRaises(ValueError):
            build_upi_payment_url("user@okaxis", "", 5000.0, "REF1")  # empty name
        with self.assertRaises(ValueError):
            build_upi_payment_url("user@okaxis", "Owner", 5000.0, "")  # empty ref

    def test_dynamic_qr_code_generation(self):
        test_uri = "upi://pay?pa=soloearn@upi&pn=Soloearn&am=5000.00&cu=INR&tr=INV-2026-0001"
        png_bytes = generate_upi_qr_bytes(test_uri)
        self.assertIsInstance(png_bytes, bytes)
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))  # Standard PNG header

        b64_uri = generate_upi_qr_base64(test_uri)
        self.assertTrue(b64_uri.startswith("data:image/png;base64,"))
        self.assertGreater(len(b64_uri), 100)

    def test_invoice_qr_generation_and_html_embed(self):
        inv = self._create_sample_invoice(amount=5000.0)
        qr_data = generate_invoice_upi_qr(inv, save_to_disk=True)
        self.assertEqual(qr_data["invoice_number"], inv["invoice_number"])
        self.assertEqual(qr_data["amount_total_inr"], 5000.0)
        self.assertTrue(qr_data["upi_uri"].startswith("upi://pay?"))
        self.assertIsNotNone(qr_data["qr_file_path"])
        self.assertTrue(Path(qr_data["qr_file_path"]).exists())

        # Render HTML invoice and check embedded UPI elements
        html_file = render_invoice_html(inv, output_dir=Path(self.tmp_dir.name) / "invoices")
        self.assertTrue(html_file.exists())
        html_text = html_file.read_text(encoding="utf-8")
        self.assertIn("Instant UPI Payment", html_text)
        self.assertIn("Scan with Google Pay, PhonePe, Paytm, or BHIM", html_text)
        self.assertIn("data:image/png;base64,", html_text)
        self.assertIn("upi://pay?", html_text)

    def test_upi_settings_management(self):
        settings = get_active_upi_settings()
        self.assertEqual(settings["account_type"], "personal")

        updated = save_upi_settings(
            payee_vpa="business@okhdfcbank",
            payee_name="SOLOEARN Studio",
            account_type="merchant_bank_api",
            merchant_code="8931",
            bank_name="HDFC Bank",
            auto_reconcile_statement=True,
            webhook_secret="secret_test_key_123",
        )
        self.assertEqual(updated["payee_vpa"], "business@okhdfcbank")
        self.assertEqual(updated["payee_name"], "SOLOEARN Studio")
        self.assertEqual(updated["account_type"], "merchant_bank_api")
        self.assertEqual(updated["merchant_code"], "8931")
        self.assertEqual(updated["bank_name"], "HDFC Bank")

    def test_security_rules_block_sensitive_credentials(self):
        # Attempting to store or pass a UPI PIN, MPIN, or banking password must fail closed!
        with self.assertRaises(SecurityViolationError):
            assert_no_sensitive_credentials({"upi_pin": "123456"})
        with self.assertRaises(SecurityViolationError):
            assert_no_sensitive_credentials({"mpin": "4321"})
        with self.assertRaises(SecurityViolationError):
            assert_no_sensitive_credentials({"netbanking_password": "mypassword"})
        with self.assertRaises(SecurityViolationError):
            assert_no_sensitive_credentials({"otp": "654321"})
        with self.assertRaises(SecurityViolationError):
            save_upi_settings(
                payee_vpa="user@okhdfcbank",
                payee_name="User",
                merchant_code="pin=1234",
            )

    def test_financial_safeguards_screenshot_and_utr_alone_cannot_settle(self):
        inv = self._create_sample_invoice(amount=5000.0)

        # 1. Unconfirmed settlement must fail
        with self.assertRaises(ValueError):
            reconcile_upi_payment_from_bank_credit(
                statement_row_id=999,
                invoice_number=inv["invoice_number"],
                owner_confirmed=False,
            )

        # 2. Non-existent bank statement row must fail
        with self.assertRaises(ValueError):
            reconcile_upi_payment_from_bank_credit(
                statement_row_id=99999,
                invoice_number=inv["invoice_number"],
                owner_confirmed=True,
            )

        # Invoice remains unpaid
        fresh_inv = get_invoice(invoice_number=inv["invoice_number"])
        self.assertEqual(fresh_inv["status"], "issued")

    def test_statement_upi_matching_and_automatic_reconciliation(self):
        inv = self._create_sample_invoice(amount=5000.0)
        inv_num = inv["invoice_number"]

        # Import bank statement CSV with UPI credit entry
        statement_csv = f"""Date,Description,Amount,Type,Ref
2026-11-10,UPI/402588112233/CR/KOPARO/{inv_num}/HDFC,5000.00,Credit,402588112233
"""
        import_res = import_bank_statement_csv(statement_csv, "bank_statement.csv")
        self.assertEqual(import_res["rows_imported"], 1)

        # Fetch statement row
        with connect() as conn:
            st_rows = [dict(r) for r in conn.execute("SELECT * FROM bank_statement_rows").fetchall()]
        self.assertEqual(len(st_rows), 1)
        st_row = st_rows[0]

        # Scan and match
        matches = scan_and_match_statement_for_upi(
            statement_rows=st_rows,
            pending_invoices=[inv],
        )
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertEqual(match["matched_invoice_number"], inv_num)
        self.assertEqual(match["statement_amount_inr"], 5000.0)
        self.assertEqual(match["match_type"], "invoice_number_in_narration")

        # Reconcile from verified bank credit
        res = reconcile_upi_payment_from_bank_credit(
            statement_row_id=st_row["id"],
            invoice_number=inv_num,
            owner_confirmed=True,
        )
        self.assertEqual(res["invoice_number"], inv_num)

        # Verify invoice is now paid
        paid_inv = get_invoice(invoice_number=inv_num)
        self.assertEqual(paid_inv["status"], "paid")
        self.assertEqual(paid_inv["payment_verified_by_owner"], 1)

        # Verify summary KPIs update
        summary = get_upi_payment_centre_summary()
        self.assertEqual(summary["confirmed_reconciled_count"], 1)
        self.assertEqual(summary["confirmed_reconciled_amount_inr"], 5000.0)
        self.assertEqual(summary["pending_invoices_count"], 0)
        self.assertEqual(summary["pending_invoices_amount_inr"], 0.0)

    def test_stage_b_merchant_bank_webhook_hmac_and_idempotency(self):
        secret = "bank_hmac_secret_key_9988"
        save_upi_settings(
            payee_vpa="merchant@okhdfcbank",
            payee_name="SOLOEARN Merchant",
            account_type="merchant_bank_api",
            webhook_secret=secret,
        )

        payload = {
            "event_id": "EVT_2026_TEST_001",
            "invoice_number": "INV-2026-0001",
            "amount_inr": 5000.0,
            "transaction_ref": "402599001122",
            "status": "success",
        }
        payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
        valid_signature = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()

        # Invalid signature must fail closed
        with self.assertRaises(ValueError):
            process_merchant_bank_webhook(payload, signature="invalid_bad_sig")

        # Valid signature succeeds
        res = process_merchant_bank_webhook(payload, signature=valid_signature)
        self.assertEqual(res["status"], "success")

        # Duplicate event_id is idempotent (does not double process)
        res_dup = process_merchant_bank_webhook(payload, signature=valid_signature)
        self.assertEqual(res_dup["status"], "already_processed")


if __name__ == "__main__":
    unittest.main()
