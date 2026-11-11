from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
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
    upsert_tax_profile,
    utc_now,
)
from core.financial_ledger import (
    get_cash_and_reserves_summary,
    import_bank_statement_csv,
    reconcile_statement_row,
    record_invoice_refund,
    list_ledger_transactions,
)
from core.invoice_generator import render_invoice_html
from core.upi_engine import (
    MerchantPaymentAdapter,
    SecurityViolationError,
    SimulatedBankMerchantAdapter,
    StandardBankMerchantAdapter,
    assert_no_sensitive_credentials,
    build_upi_payment_url,
    generate_invoice_upi_qr,
    generate_upi_qr_base64,
    generate_upi_qr_bytes,
    get_active_upi_settings,
    get_upi_interoperability_disclosure,
    get_upi_payment_centre_summary,
    init_upi_tables,
    parse_upi_payment_url,
    process_merchant_bank_webhook,
    reconcile_upi_payment_from_bank_credit,
    save_upi_settings,
    scan_and_match_statement_for_upi,
    validate_vpa,
)
from core.preflight import (
    get_kotak_onboarding_guide,
    run_m7_local_preflight,
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

    def _create_sample_invoice(self, amount: float = 5000.0, tax_rate: float = 0.0) -> dict:
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
            tax_rate=tax_rate,
            task_id=task_id,
        )
        return inv

    def test_vpa_validation(self):
        # Valid VPAs across major Indian PSPs
        self.assertEqual(validate_vpa("user@okhdfcbank"), "user@okhdfcbank")
        self.assertEqual(validate_vpa("merchant.store@icici"), "merchant.store@icici")
        self.assertEqual(validate_vpa("soloearn@upi"), "soloearn@upi")
        self.assertEqual(validate_vpa("developer@paytm"), "developer@paytm")

        # Invalid VPAs
        with self.assertRaises(ValueError):
            validate_vpa("")
        with self.assertRaises(ValueError):
            validate_vpa("invalidvpa")
        with self.assertRaises(ValueError):
            validate_vpa("@okhdfcbank")
        with self.assertRaises(ValueError):
            validate_vpa("user@")

    def test_npci_upi_url_builder_and_initiation_mode_selection(self):
        # 1. Mode 04: Intent (Mobile Web / App-to-App flow)
        url_intent = build_upi_payment_url(
            payee_vpa="founder@okaxis",
            payee_name="SOLOEARN Operations",
            amount_inr=5000.0,
            transaction_ref="INV-2026-0001",
            transaction_note="Invoice INV-2026-0001",
            merchant_code="8931",
            initiation_mode="04",
        )
        self.assertTrue(url_intent.startswith("upi://pay?"))
        self.assertIn("pa=founder%40okaxis", url_intent)
        self.assertIn("pn=SOLOEARN+Operations", url_intent)
        self.assertIn("am=5000.00", url_intent)
        self.assertIn("cu=INR", url_intent)
        self.assertIn("tr=INV-2026-0001", url_intent)
        self.assertIn("mc=8931", url_intent)
        self.assertIn("mode=04", url_intent)

        # 2. Mode 01: QR Code (Standard Scannable Dynamic QR)
        url_qr = build_upi_payment_url(
            payee_vpa="founder@okaxis",
            payee_name="SOLOEARN Operations",
            amount_inr=5000.0,
            transaction_ref="INV-2026-0001",
            initiation_mode="01",
        )
        self.assertIn("mode=01", url_qr)

        # 3. Mode 00: Default Transaction
        url_mode00 = build_upi_payment_url(
            payee_vpa="founder@okaxis",
            payee_name="SOLOEARN Operations",
            amount_inr=5000.0,
            transaction_ref="INV-2026-0001",
            initiation_mode="00",
        )
        self.assertIn("mode=00", url_mode00)

        # 4. CRITICAL TECHNICAL RULE: Unsigned Mode 02 (Secure QR) must be rejected!
        with self.assertRaises(ValueError) as ctx_02:
            build_upi_payment_url(
                payee_vpa="founder@okaxis",
                payee_name="SOLOEARN Operations",
                amount_inr=5000.0,
                transaction_ref="INV-2026-0001",
                initiation_mode="02",
            )
        self.assertIn("Secure QR code (mode 02)", str(ctx_02.exception))

        # 5. CRITICAL TECHNICAL RULE: Unsigned Mode 05 (Secure Intent) must be rejected!
        with self.assertRaises(ValueError) as ctx_05:
            build_upi_payment_url(
                payee_vpa="founder@okaxis",
                payee_name="SOLOEARN Operations",
                amount_inr=5000.0,
                transaction_ref="INV-2026-0001",
                initiation_mode="05",
            )
        self.assertIn("Secure Intent (mode 05)", str(ctx_05.exception))

        # Parse back roundtrip verification
        parsed = parse_upi_payment_url(url_intent)
        self.assertEqual(parsed["pa"], "founder@okaxis")
        self.assertEqual(parsed["pn"], "SOLOEARN Operations")
        self.assertEqual(parsed["am"], 5000.0)
        self.assertEqual(parsed["cu"], "INR")
        self.assertEqual(parsed["tr"], "INV-2026-0001")
        self.assertEqual(parsed["mc"], "8931")
        self.assertEqual(parsed["mode"], "04")

    def test_upi_interoperability_disclosure(self):
        disclosure = get_upi_interoperability_disclosure()
        self.assertIn("initiation_mode", disclosure)
        self.assertIn("personal_upi_limits", disclosure)
        self.assertIn("bank_terms_and_conditions", disclosure)
        self.assertIn("consumer_app_behavior", disclosure)
        self.assertIn("anti_fraud_settlement_policy", disclosure)
        self.assertIn("gst_statutory_warning", disclosure)
        self.assertIn("Mode 00: Default transaction", disclosure["initiation_mode"])
        self.assertIn("Mode 01: QR code", disclosure["initiation_mode"])
        self.assertIn("Mode 02: Secure QR code", disclosure["initiation_mode"])
        self.assertIn("Mode 04: Intent", disclosure["initiation_mode"])
        self.assertIn("Mode 05: Secure Intent", disclosure["initiation_mode"])
        self.assertIn("GST not charged — Supplier is not registered under GST.", disclosure["gst_statutory_warning"])

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
        with self.assertRaises(ValueError):
            build_upi_payment_url("user@okaxis", "Owner", 5000.0, "REF1", initiation_mode="99")  # invalid mode

    def test_dynamic_qr_code_generation(self):
        test_uri = "upi://pay?pa=soloearn@upi&pn=Soloearn&am=5000.00&cu=INR&tr=INV-2026-0001&mode=01"
        png_bytes = generate_upi_qr_bytes(test_uri)
        self.assertIsInstance(png_bytes, bytes)
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))  # Standard PNG header

        b64_uri = generate_upi_qr_base64(test_uri)
        self.assertTrue(b64_uri.startswith("data:image/png;base64,"))
        self.assertGreater(len(b64_uri), 100)

    def test_invoice_qr_generation_and_html_embed(self):
        inv = self._create_sample_invoice(amount=5000.0)
        qr_data = generate_invoice_upi_qr(inv, save_to_disk=True, initiation_mode="01")
        self.assertEqual(qr_data["invoice_number"], inv["invoice_number"])
        self.assertEqual(qr_data["amount_inr"], 5000.0)
        self.assertTrue(qr_data["upi_uri"].startswith("upi://pay?"))
        self.assertIn("mode=01", qr_data["upi_uri"])
        self.assertIn("mode=04", qr_data["intent_uri"])
        self.assertIsNotNone(qr_data["qr_file_path"])
        self.assertTrue(Path(qr_data["qr_file_path"]).exists())

        # Render HTML invoice and check embedded UPI elements
        html_file = render_invoice_html(inv, output_dir=Path(self.tmp_dir.name) / "invoices")
        self.assertTrue(html_file.exists())
        html_text = html_file.read_text(encoding="utf-8")
        self.assertIn("Direct UPI Payment", html_text)
        self.assertIn("Standard QR (Mode 01)", html_text)
        self.assertIn("data:image/png;base64,", html_text)
        self.assertIn("upi://pay?", html_text)
        self.assertIn("GST not charged — Supplier is not registered under GST.", html_text)

    def test_gst_collection_gating_unregistered_vs_registered(self):
        # 1. When tax profile indicates unregistered (is_gst_registered=0), charging tax must fail!
        upsert_tax_profile(
            profile_name="Sole Proprietor Unregistered",
            is_gst_registered=False,
            gstin="",
        )
        opp_id = add_opportunity({
            "title": "GST Test Project",
            "source": "verified_direct",
            "source_permitted": True,
            "amount_inr": 10000.0,
            "estimated_hours": 2.0,
            "skill_match": 0.9,
            "payment_confidence": 0.9,
            "risk_level": 1.0,
        }, score=9.0)
        with connect() as conn:
            conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opp_id,))
        task_id = create_task(
            title="Task 1",
            task_type="csv_business_analysis",
            workspace="ws_test1",
            opportunity_id=opp_id,
        )
        with connect() as conn:
            conn.execute("UPDATE tasks SET status='submission_ready', artifact_path='deliverables.zip' WHERE id=?", (task_id,))

        with self.assertRaises(ValueError) as ctx:
            create_invoice(
                opportunity_id=opp_id,
                client_name="Corporate Client",
                amount_subtotal=10000.0,
                tax_rate=18.0,
                task_id=task_id,
                require_gst_registration=True,
            )
        self.assertIn("GST collection prohibited", str(ctx.exception))
        self.assertIn("Section 32, CGST Act", str(ctx.exception))

        # Charging 0% GST succeeds for unregistered entity
        inv_unreg = create_invoice(
            opportunity_id=opp_id,
            client_name="Corporate Client",
            amount_subtotal=10000.0,
            tax_rate=0.0,
            task_id=task_id,
            require_gst_registration=True,
        )
        self.assertEqual(inv_unreg["tax_amount"], 0.0)
        self.assertEqual(inv_unreg["amount_total"], 10000.0)

        # 2. When registered with active GSTIN, charging GST is permitted
        upsert_tax_profile(
            profile_name="Sole Proprietor Registered",
            is_gst_registered=True,
            gstin="27ABCDE1234F1Z5",
        )
        opp2_id = add_opportunity({
            "title": "GST Registered Project",
            "source": "verified_direct",
            "source_permitted": True,
            "amount_inr": 10000.0,
            "estimated_hours": 2.0,
            "skill_match": 0.9,
            "payment_confidence": 0.9,
            "risk_level": 1.0,
        }, score=9.0)
        with connect() as conn:
            conn.execute("UPDATE opportunities SET status='submitted_by_owner' WHERE id=?", (opp2_id,))
        task2_id = create_task(
            title="Task 2",
            task_type="csv_business_analysis",
            workspace="ws_test2",
            opportunity_id=opp2_id,
        )
        with connect() as conn:
            conn.execute("UPDATE tasks SET status='submission_ready', artifact_path='deliverables.zip' WHERE id=?", (task2_id,))

        inv_reg = create_invoice(
            opportunity_id=opp2_id,
            client_name="Corporate Client 2",
            amount_subtotal=10000.0,
            tax_rate=18.0,
            task_id=task2_id,
            require_gst_registration=True,
        )
        self.assertEqual(inv_reg["tax_amount"], 1800.0)
        self.assertEqual(inv_reg["amount_total"], 11800.0)

    def test_upi_settings_management(self):
        settings = get_active_upi_settings()
        self.assertEqual(settings["account_type"], "personal")

        save_upi_settings(
            payee_vpa="business@okhdfcbank",
            payee_name="SOLOEARN Studio",
            account_type="merchant",
            merchant_code="8931",
            bank_name="HDFC Bank",
            webhook_secret="secret_test_key_123",
        )
        updated = get_active_upi_settings()
        self.assertEqual(updated["payee_vpa"], "business@okhdfcbank")
        self.assertEqual(updated["payee_name"], "SOLOEARN Studio")
        self.assertEqual(updated["account_type"], "merchant")
        self.assertEqual(updated["merchant_code"], "8931")
        self.assertEqual(updated["bank_name"], "HDFC Bank")
        self.assertEqual(updated["webhook_secret"], "secret_test_key_123")

    def test_security_rules_block_sensitive_credentials(self):
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
        self.assertEqual(res["status"], "paid")

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

    def test_underpayment_overpayment_and_partial_payment_handling(self):
        inv = self._create_sample_invoice(amount=5000.0)
        inv_num = inv["invoice_number"]

        # Import an underpaid credit (₹3000 instead of ₹5000)
        st_csv = f"""Date,Description,Amount,Type,Ref
2026-11-10,UPI/402500000001/CR/PARTIAL/{inv_num},3000.00,Credit,402500000001
"""
        import_bank_statement_csv(st_csv, "underpay.csv")
        with connect() as conn:
            st_rows = [dict(r) for r in conn.execute("SELECT * FROM bank_statement_rows").fetchall()]
        st_row_under = st_rows[0]

        # 1. Underpayment without allow_partial=True must fail closed
        with self.assertRaises(ValueError) as ctx:
            reconcile_upi_payment_from_bank_credit(
                statement_row_id=st_row_under["id"],
                invoice_number=inv_num,
                owner_confirmed=True,
                allow_partial=False,
            )
        self.assertIn("Partial payments are not enabled", str(ctx.exception))

        # 2. Underpayment with allow_partial=True records partial payment state
        res_part = reconcile_upi_payment_from_bank_credit(
            statement_row_id=st_row_under["id"],
            invoice_number=inv_num,
            owner_confirmed=True,
            allow_partial=True,
        )
        self.assertEqual(res_part["status"], "partially_paid")
        self.assertEqual(res_part["amount_settled_inr"], 3000.0)
        self.assertEqual(res_part["remaining_due_inr"], 2000.0)

        # Invoice remains in partially_paid status
        part_inv = get_invoice(invoice_number=inv_num)
        self.assertEqual(part_inv["status"], "partially_paid")
        self.assertEqual(part_inv["amount_paid"], 3000.0)

        # 3. Overpayment test: attempt to pay ₹3000 when only ₹2000 is remaining
        st_csv_over = f"""Date,Description,Amount,Type,Ref
2026-11-11,UPI/402500000002/CR/OVER/{inv_num},3000.00,Credit,402500000002
"""
        import_bank_statement_csv(st_csv_over, "overpay.csv")
        with connect() as conn:
            st_row_over = conn.execute("SELECT * FROM bank_statement_rows WHERE bank_ref='402500000002'").fetchone()

        with self.assertRaises(ValueError) as ctx_over:
            reconcile_upi_payment_from_bank_credit(
                statement_row_id=st_row_over["id"],
                invoice_number=inv_num,
                owner_confirmed=True,
                allow_partial=True,
            )
        self.assertIn("exceeds remaining invoice balance", str(ctx_over.exception))

        # 4. Final remaining settlement: pay exact remaining balance ₹2000
        st_csv_final = f"""Date,Description,Amount,Type,Ref
2026-11-11,UPI/402500000003/CR/FINAL/{inv_num},2000.00,Credit,402500000003
"""
        import_bank_statement_csv(st_csv_final, "final.csv")
        with connect() as conn:
            st_row_final = conn.execute("SELECT * FROM bank_statement_rows WHERE bank_ref='402500000003'").fetchone()

        res_final = reconcile_upi_payment_from_bank_credit(
            statement_row_id=st_row_final["id"],
            invoice_number=inv_num,
            owner_confirmed=True,
        )
        self.assertEqual(res_final["status"], "paid")
        self.assertEqual(res_final["remaining_due_inr"], 0.0)

        paid_inv = get_invoice(invoice_number=inv_num)
        self.assertEqual(paid_inv["status"], "paid")
        self.assertEqual(paid_inv["amount_paid"], 5000.0)

    def test_reversal_and_duplicate_reference_protection(self):
        inv1 = self._create_sample_invoice(amount=2000.0)
        inv2 = self._create_sample_invoice(amount=2000.0)

        # Bank statement with a reversal entry
        reversal_csv = """Date,Description,Amount,Type,Ref
2026-11-10,UPI REVERSAL/402599/WRONG_TXN,2000.00,Debit,402599
"""
        import_bank_statement_csv(reversal_csv, "rev.csv")
        with connect() as conn:
            rev_row = conn.execute("SELECT * FROM bank_statement_rows WHERE bank_ref='402599'").fetchone()

        # Reversal row is skipped in auto-matching
        matches = scan_and_match_statement_for_upi([dict(rev_row)], [inv1])
        self.assertEqual(len(matches), 0)

        # Reconciling against reversal or debit fails
        with self.assertRaises(ValueError) as ctx:
            reconcile_upi_payment_from_bank_credit(
                statement_row_id=rev_row["id"],
                invoice_number=inv1["invoice_number"],
                owner_confirmed=True,
            )
        self.assertTrue(any(word in str(ctx.exception).lower() for word in ("reversal", "credit")))

        # Duplicate reference protection
        st_dup_csv = f"""Date,Description,Amount,Type,Ref
2026-11-10,UPI/402577777777/CR/{inv1['invoice_number']},2000.00,Credit,402577777777
"""
        import_bank_statement_csv(st_dup_csv, "dup1.csv")
        with connect() as conn:
            r1 = conn.execute("SELECT * FROM bank_statement_rows WHERE bank_ref='402577777777'").fetchone()

        # Settle invoice 1 with this reference
        reconcile_upi_payment_from_bank_credit(
            statement_row_id=r1["id"],
            invoice_number=inv1["invoice_number"],
            owner_confirmed=True,
        )

        # Attempting to assign the same bank reference to invoice 2 must fail
        st_dup_csv2 = f"""Date,Description,Amount,Type,Ref
2026-11-11,UPI/402577777777/CR/{inv2['invoice_number']},2000.00,Credit,402577777777
"""
        import_bank_statement_csv(st_dup_csv2, "dup2.csv")
        with connect() as conn:
            r2 = conn.execute("SELECT * FROM bank_statement_rows WHERE id <> ? AND bank_ref='402577777777'", (r1["id"],)).fetchone()

        with self.assertRaises(ValueError) as ctx_dup:
            reconcile_upi_payment_from_bank_credit(
                statement_row_id=r2["id"],
                invoice_number=inv2["invoice_number"],
                owner_confirmed=True,
            )
        self.assertIn("already assigned", str(ctx_dup.exception))

    def test_refund_integration_on_reconciled_invoice(self):
        inv = self._create_sample_invoice(amount=4000.0)
        inv_num = inv["invoice_number"]

        st_csv = f"""Date,Description,Amount,Type,Ref
2026-11-10,UPI/402511223344/CR/{inv_num},4000.00,Credit,402511223344
"""
        import_bank_statement_csv(st_csv, "refund_test.csv")
        with connect() as conn:
            r = conn.execute("SELECT * FROM bank_statement_rows WHERE bank_ref='402511223344'").fetchone()

        reconcile_upi_payment_from_bank_credit(
            statement_row_id=r["id"],
            invoice_number=inv_num,
            owner_confirmed=True,
        )

        # Record a verified partial refund of ₹1,000
        refund_res = record_invoice_refund(
            invoice_number=inv_num,
            gross_refund_inr=1000.0,
            refund_reference="REFUND-UPI-001",
            owner_confirmed=True,
        )
        self.assertEqual(refund_res["status"], "recorded_owner_confirmed")
        self.assertEqual(refund_res["gross_refund_inr"], 1000.0)

        # Ensure append-only ledger recorded refund
        txs = list_ledger_transactions(limit=10)
        refund_txs = [t for t in txs if t["tx_type"] == "refund"]
        self.assertEqual(len(refund_txs), 1)
        self.assertEqual(refund_txs[0]["amount_inr"], 1000.0)

    def test_stage_b_merchant_adapters_and_webhook_security(self):
        secret = "bank_hmac_secret_key_9988"
        save_upi_settings(
            payee_vpa="merchant@okhdfcbank",
            payee_name="SOLOEARN Merchant",
            account_type="merchant",
            webhook_secret=secret,
        )
        inv = self._create_sample_invoice(amount=5000.0)
        inv_num = inv["invoice_number"]

        # 1. Test Adapter Classes & NOT CONNECTED Status
        sim_adapter = SimulatedBankMerchantAdapter()
        self.assertTrue(sim_adapter.is_simulated)
        self.assertEqual(sim_adapter.adapter_name, "simulated_bank_sandbox")
        self.assertTrue(sim_adapter.connection_status.startswith("NOT CONNECTED"))

        prod_adapter = StandardBankMerchantAdapter()
        self.assertFalse(prod_adapter.is_simulated)
        self.assertEqual(prod_adapter.adapter_name, "standard_bank_production")
        self.assertTrue(prod_adapter.connection_status.startswith("NOT CONNECTED"))

        # 2. Test Replay Attack Protection (Timestamp Skew)
        old_timestamp = int(time.time()) - 600  # 10 minutes ago
        old_payload, old_sig = SimulatedBankMerchantAdapter.create_signed_payload(
            event_id="EVT_REPLAY_TEST",
            invoice_number=inv_num,
            amount_inr=5000.0,
            status="settlement_processed",
            secret=secret,
            timestamp=old_timestamp,
        )
        with self.assertRaises(ValueError) as ctx_replay:
            process_merchant_bank_webhook(old_payload, signature=old_sig, adapter=sim_adapter)
        self.assertIn("replay-attack protection", str(ctx_replay.exception).lower())

        # 3. Test Invalid HMAC Signature
        current_payload, current_sig = SimulatedBankMerchantAdapter.create_signed_payload(
            event_id="EVT_VALID_001",
            invoice_number=inv_num,
            amount_inr=5000.0,
            status="payment_captured",
            secret=secret,
        )
        with self.assertRaises(ValueError) as ctx_sig:
            process_merchant_bank_webhook(current_payload, signature="invalid_tampered_sig", adapter=sim_adapter)
        self.assertIn("Invalid HMAC signature", str(ctx_sig.exception))

        # 4. Test State Separation: payment_captured (In Transit) vs settlement_processed
        res_captured = process_merchant_bank_webhook(current_payload, signature=current_sig, adapter=sim_adapter)
        self.assertEqual(res_captured["status"], "payment_captured")
        self.assertFalse(res_captured["settled_to_bank"])

        # Invoice should NOT be marked paid yet (funds are in gateway/bank transit)
        fresh_inv = get_invoice(invoice_number=inv_num)
        self.assertNotEqual(fresh_inv["status"], "paid")

        # 5. Test Bank Settlement Event
        settle_payload, settle_sig = SimulatedBankMerchantAdapter.create_signed_payload(
            event_id="EVT_SETTLE_001",
            invoice_number=inv_num,
            amount_inr=5000.0,
            status="settlement_processed",
            secret=secret,
        )
        res_settled = process_merchant_bank_webhook(settle_payload, signature=settle_sig, adapter=sim_adapter)
        self.assertEqual(res_settled["status"], "settlement_processed")
        self.assertTrue(res_settled["settled_to_bank"])

        # Invoice is now marked PAID and ledger updated
        fresh_inv_settled = get_invoice(invoice_number=inv_num)
        self.assertEqual(fresh_inv_settled["status"], "paid")
        self.assertEqual(fresh_inv_settled["amount_paid"], 5000.0)

        # 6. Test Idempotency (duplicate webhook notification ignored without double-crediting)
        res_dup = process_merchant_bank_webhook(settle_payload, signature=settle_sig, adapter=sim_adapter)
        self.assertEqual(res_dup["status"], "already_processed")

    def test_ambiguous_statement_matching_and_audit(self):
        # Create two different invoices with the exact same amount
        inv1 = self._create_sample_invoice(amount=6000.0)
        inv2 = self._create_sample_invoice(amount=6000.0)

        # Bank statement row without invoice number in narration
        ambig_csv = """Date,Description,Amount,Type,Ref
2026-11-10,UPI/402599887766/CR/GENERIC_CLIENT/HDFC,6000.00,Credit,402599887766
"""
        import_bank_statement_csv(ambig_csv, "ambig.csv")
        with connect() as conn:
            ambig_row = conn.execute("SELECT * FROM bank_statement_rows WHERE bank_ref='402599887766'").fetchone()

        matches = scan_and_match_statement_for_upi([dict(ambig_row)], [inv1, inv2])
        self.assertEqual(len(matches), 1)
        m = matches[0]
        self.assertEqual(m["match_type"], "ambiguous_multiple_candidates_same_amount")
        self.assertTrue(m.get("is_ambiguous", False))
        self.assertEqual(m["confidence"], 0.40)

    def test_kotak_bank_statement_parsing_with_metadata_and_utr(self):
        """Tests that Kotak statements with metadata rows and Dr/Cr columns parse accurately."""
        kotak_sample = """Account Number: 9876543210
Customer Name: SOLOEARN OPERATIONS
Product: Kotak Safalta Current Account
Statement Period: 01-Nov-2026 to 10-Nov-2026

Sl No,Transaction Date,Description / Narration,Chq / Ref No.,Amount,Dr / Cr,Balance
1,05-Nov-2026,UPI/432199887766/CR/CLIENT_A/KOTAK/INV-2026-0001,,5000.00,CR,25000.00
2,07-Nov-2026,ATM WDL/432199000000/CASH,,2000.00,DR,23000.00
"""
        res = import_bank_statement_csv(kotak_sample, "kotak_sample.csv")
        self.assertEqual(res["rows_imported"], 2)
        with connect() as conn:
            rows = conn.execute("SELECT * FROM bank_statement_rows WHERE import_id=?", (res["import_id"],)).fetchall()
        self.assertEqual(len(rows), 2)
        cr_row = [r for r in rows if r["tx_type"] == "credit"][0]
        dr_row = [r for r in rows if r["tx_type"] == "debit"][0]
        self.assertEqual(cr_row["amount_inr"], 5000.0)
        self.assertEqual(cr_row["bank_ref"], "432199887766")
        self.assertEqual(dr_row["amount_inr"], 2000.0)
        self.assertEqual(dr_row["tx_type"], "debit")

    def test_m7_local_preflight_verification_suite(self):
        """Tests the end-to-end local preflight sandbox verification flow."""
        preflight_res = run_m7_local_preflight(verbose=False)
        self.assertTrue(preflight_res["success"])
        self.assertEqual(preflight_res["steps_completed"], 7)
        self.assertEqual(preflight_res["total_steps"], 7)

    def test_kotak_onboarding_guide_specs(self):
        """Verifies Kotak Safalta specs, Udyam NIC codes, and branch inquiry questions."""
        guide = get_kotak_onboarding_guide()
        self.assertEqual(guide["recommended_account"], "Kotak Safalta Current Account")
        self.assertEqual(guide["average_quarterly_balance_inr"], 5000.0)
        self.assertIn("udyamregistration.gov.in", guide["udyam_portal_url"])
        self.assertEqual(len(guide["four_questions_for_kotak_branch"]), 4)
        nic_codes = [n["code"] for n in guide["recommended_nic_codes"]]
        self.assertIn("62011", nic_codes)
        self.assertIn("62020", nic_codes)
        self.assertIn("63112", nic_codes)


if __name__ == "__main__":
    unittest.main()
