"""
Comprehensive Audit & Verification Test Suite for:
1. NPCI UPI 2026 Compliance (Dynamic QR Mode 01 & Intent Flow Mode 04).
2. Paise-Level Integer Arithmetic (zero floating-point drift across invoicing, payments, refunds, taxes).
3. Kotak Bank Statement Narration Parser (UPI, NEFT, RTGS, IMPS, ACH, Invoice extraction).
4. Double-Entry General Ledger Balance & Production Immutability Verification.
"""

import os
import unittest
import sqlite3

from core.config import DB_PATH
from core.db import connect, init_db, utc_now
from core.tax_engine import (
    inr_to_paise,
    paise_to_inr,
    calculate_section_393_tds,
    compute_income_tax_india_2026,
    compute_business_incremental_tax,
)
from core.upi_engine import (
    build_upi_payment_url,
    validate_npci_compliance,
    parse_upi_payment_url,
    generate_invoice_upi_qr,
)
from core.financial_ledger import (
    parse_bank_narration,
    record_invoice_refund,
    generate_double_entry_trial_balance,
    audit_ledger_integrity,
    record_ledger_transaction,
    get_cash_and_reserves_summary,
)


class TestNPCIUPICompliance2026(unittest.TestCase):
    """NPCI UPI Specifications 2026 Verification."""

    def test_dynamic_qr_mode_01_mandatory_parameters(self):
        url = build_upi_payment_url(
            payee_vpa="freelancer@kotak",
            payee_name="Soloearn Solutions",
            amount_inr=15499.50,
            transaction_ref="INV-2026-901",
            transaction_note="Settlement for Web Development",
            mode="01",
        )
        self.assertTrue(url.startswith("upi://pay?"))
        self.assertIn("mode=01", url)
        self.assertIn("cu=INR", url)
        self.assertIn("am=15499.50", url)
        self.assertIn("pa=freelancer%40kotak", url)

        comp = validate_npci_compliance(url)
        self.assertTrue(comp["is_compliant"])
        self.assertEqual(len(comp["errors"]), 0)
        self.assertEqual(comp["extracted_params"]["mode"], "01")
        self.assertEqual(comp["extracted_params"]["cu"], "INR")
        self.assertEqual(comp["extracted_params"]["amount_paise"], 1549950)

    def test_intent_flow_mode_04_mandatory_parameters(self):
        url = build_upi_payment_url(
            payee_vpa="merchant@icici",
            payee_name="Consulting Services",
            amount_inr=5000.0,
            transaction_ref="TXN-INTENT-789",
            transaction_note="Retainer Fee",
            mode="04",
        )
        self.assertIn("mode=04", url)
        comp = validate_npci_compliance(url)
        self.assertTrue(comp["is_compliant"])
        self.assertEqual(comp["flow_type"], "Intent Flow")

    def test_npci_validation_rejects_missing_mandatory_fields(self):
        # Missing cu parameter
        invalid_url = "upi://pay?pa=test@upi&pn=Test&am=100.00&tr=TXN01&tn=Note&mode=01"
        comp = validate_npci_compliance(invalid_url)
        self.assertFalse(comp["is_compliant"])
        self.assertTrue(any("Missing mandatory parameter: cu" in err for err in comp["errors"]))

        # Invalid currency (non-INR)
        non_inr_url = "upi://pay?pa=test@upi&pn=Test&am=100.00&cu=USD&tr=TXN01&tn=Note&mode=01"
        comp = validate_npci_compliance(non_inr_url)
        self.assertFalse(comp["is_compliant"])
        self.assertTrue(any("Currency must be INR" in err for err in comp["errors"]))

        # Non-positive amount
        zero_amt_url = "upi://pay?pa=test@upi&pn=Test&am=0.00&cu=INR&tr=TXN01&tn=Note&mode=01"
        comp = validate_npci_compliance(zero_amt_url)
        self.assertFalse(comp["is_compliant"])
        self.assertTrue(any("Amount must be greater than zero" in err for err in comp["errors"]))

    def test_npci_transaction_ceiling_limit(self):
        # NPCI general standard P2M ceiling is ₹2,00,000 (20,000,000 paise)
        # 1. Generator rejects excess amount upfront
        with self.assertRaises(ValueError) as ctx:
            build_upi_payment_url(
                payee_vpa="corp@hdfc",
                payee_name="Big Corp",
                amount_inr=250000.0,
                transaction_ref="BIG-01",
                mode="01",
            )
        self.assertIn("exceeds standard UPI transaction limit", str(ctx.exception))

        # 2. Validator rejects URLs claiming > ₹2,00,000
        excess_url = "upi://pay?pa=corp@hdfc&pn=Big+Corp&am=250000.00&cu=INR&tr=BIG-01&tn=Note&mode=01"
        comp = validate_npci_compliance(excess_url)
        self.assertFalse(comp["is_compliant"])
        self.assertTrue(any("exceeds standard NPCI limit" in err for err in comp["errors"]))

    def test_npci_unsigned_mode_02_rejection(self):
        # Static QR (Mode 02) or signed flow without digital signature fails closed
        unsigned_mode02 = "upi://pay?pa=test@upi&pn=Test&am=500.00&cu=INR&tr=T1&mode=02"
        comp = validate_npci_compliance(unsigned_mode02)
        self.assertFalse(comp["is_compliant"])
        self.assertTrue(any("Mode 02 requires a secure digital signature" in err for err in comp["errors"]))

    def test_upi_parser_extracts_exact_paise(self):
        url = "upi://pay?pa=vendor@okaxis&pn=Design+Studio&am=12345.67&cu=INR&tr=INV-999&mode=01"
        parsed = parse_upi_payment_url(url)
        self.assertEqual(parsed["amount_paise"], 1234567)
        self.assertEqual(parsed["payee_vpa"], "vendor@okaxis")
        self.assertEqual(parsed["payee_name"], "Design Studio")


class TestPaiseLevelIntegerArithmetic(unittest.TestCase):
    """Paise-level integer arithmetic across invoicing, payments, refunds, taxes."""

    def test_inr_to_paise_and_paise_to_inr_conversion(self):
        self.assertEqual(inr_to_paise(100.00), 10000)
        self.assertEqual(inr_to_paise(0.01), 1)
        self.assertEqual(inr_to_paise(0.00), 0)
        self.assertEqual(inr_to_paise(15499.50), 1549950)
        self.assertEqual(inr_to_paise(0.1 + 0.2), 30)  # Handles classic float drift (0.30000000000000004)

        self.assertEqual(paise_to_inr(10000), 100.00)
        self.assertEqual(paise_to_inr(1), 0.01)
        self.assertEqual(paise_to_inr(1549950), 15499.50)

    def test_section_194j_tds_exact_paise(self):
        # ₹55,555.55 at 10% TDS
        subtotal = 55555.55
        tds_val = calculate_section_393_tds(subtotal, "professional_services_10")
        expected_tds_paise = int(round(inr_to_paise(subtotal) * 0.10))
        self.assertEqual(inr_to_paise(tds_val), expected_tds_paise)
        self.assertEqual(tds_val, paise_to_inr(expected_tds_paise))

    def test_income_tax_2026_paise_equilibrium(self):
        # ₹12,34,567 annual taxable profit
        tax_res = compute_income_tax_india_2026(1234567.0, regime="new_2026")
        tax_before_cess_paise = tax_res["tax_before_cess_paise"]
        cess_paise = tax_res["cess_paise"]
        total_tax_paise = tax_res["total_tax_paise"]

        # Critical equilibrium: total tax MUST EQUAL tax before cess + cess in paise
        self.assertEqual(total_tax_paise, tax_before_cess_paise + cess_paise)
        self.assertEqual(tax_res["total_tax"], paise_to_inr(total_tax_paise))

    def test_refund_exact_paise_split(self):
        init_db()
        with connect() as conn:
            # Clean sandbox entries
            conn.execute("DELETE FROM financial_transactions WHERE is_sandbox = 1")
            conn.execute("DELETE FROM invoices WHERE is_sandbox = 1")

            now = utc_now()
            # Ensure parent opportunity exists
            conn.execute(
                """INSERT OR IGNORE INTO opportunities (id, title, source, amount_inr, status, created_at)
                   VALUES (9999, 'Audit Task', 'direct', 11800.0, 'won', ?)""",
                (now,),
            )
            # Invoice: Subtotal ₹10,000, 18% GST (₹1,800), Total ₹11,800
            conn.execute(
                """INSERT INTO invoices (
                    invoice_number, opportunity_id, client_name,
                    amount_subtotal, tax_rate, tax_amount, amount_total,
                    status, issued_at, due_at, paid_at, is_sandbox, amount_paid
                ) VALUES ('INV-REF-AUDIT-01', 9999, 'Audit Client',
                          10000.0, 18.0, 1800.0, 11800.0,
                          'paid', ?, ?, ?, 1, 11800.0)""",
                (now, now, now),
            )

        # Refund 50% = ₹5,900.00
        refund_res = record_invoice_refund(
            invoice_number="INV-REF-AUDIT-01",
            gross_refund_inr=5900.0,
            refund_reference="REF-AUDIT-01",
            owner_confirmed=True,
        )
        self.assertEqual(refund_res["gross_refund_paise"], 590000)
        self.assertEqual(refund_res["refund_subtotal_paise"] + refund_res["refund_tax_paise"], 590000)

        with connect() as conn:
            txs = conn.execute(
                "SELECT tx_type, amount_paise FROM financial_transactions WHERE reference_id LIKE 'REFUND:INV-REF-AUDIT-01%'"
            ).fetchall()
            refund_paise = sum(int(r["amount_paise"]) for r in txs if r["tx_type"] in ("refund", "gst_refund"))
            self.assertEqual(refund_paise, 590000)


class TestKotakBankStatementNarrationParser(unittest.TestCase):
    """Kotak statement parser for UPI, NEFT, RTGS, IMPS, ACH, and invoice references."""

    def test_parse_kotak_upi_narration(self):
        narration = "UPI/432199887766/CR/buyer@oksbi/Payment for INV-2026-101/Kotak"
        res = parse_bank_narration(narration)
        self.assertEqual(res["channel"], "UPI")
        self.assertEqual(res["direction"], "credit")
        self.assertEqual(res["utr_rrn"], "432199887766")
        self.assertEqual(res["counterparty"], "buyer@oksbi")
        self.assertEqual(res["inferred_invoice_ref"], "INV-2026-101")

    def test_parse_kotak_neft_narration(self):
        narration = "NEFT CR-KKBKH26123456789-ACME TECHNOLOGIES PVT LTD-INV-2026-0042"
        res = parse_bank_narration(narration)
        self.assertEqual(res["channel"], "NEFT")
        self.assertEqual(res["direction"], "credit")
        self.assertEqual(res["utr_rrn"], "KKBKH26123456789")
        self.assertIn("ACME TECHNOLOGIES", res["counterparty"])
        self.assertEqual(res["inferred_invoice_ref"], "INV-2026-0042")

    def test_parse_kotak_rtgs_narration(self):
        narration = "RTGS CR-KKBKR52026111200123456-GLOBAL CLIENTS INC-CONTRACT SETTLEMENT"
        res = parse_bank_narration(narration)
        self.assertEqual(res["channel"], "RTGS")
        self.assertEqual(res["direction"], "credit")
        self.assertEqual(res["utr_rrn"], "KKBKR52026111200123456")
        self.assertIn("GLOBAL CLIENTS", res["counterparty"])

    def test_parse_kotak_imps_narration(self):
        narration = "IMPS/P2A/432199887766/CLIENT CORP/PAYMENT FOR INV-882"
        res = parse_bank_narration(narration)
        self.assertEqual(res["channel"], "IMPS")
        self.assertEqual(res["direction"], "credit")
        self.assertEqual(res["utr_rrn"], "432199887766")
        self.assertEqual(res["inferred_invoice_ref"], "INV-882")

    def test_parse_ach_dividend_interest(self):
        narration = "ACH CR-000000000123456-TCS DIVIDEND"
        res = parse_bank_narration(narration)
        self.assertEqual(res["channel"], "ACH")
        self.assertEqual(res["direction"], "credit")
        self.assertEqual(res["utr_rrn"], "000000000123456")


class TestDoubleEntryLedgerAndImmutability(unittest.TestCase):
    """Double-entry trial balance equilibrium and production immutability."""

    def test_trial_balance_debits_equal_credits(self):
        init_db()
        # Audit integrity includes trial balance equilibrium
        audit_res = audit_ledger_integrity(include_sandbox=True)
        self.assertTrue(audit_res["checks"]["double_entry_balanced"])
        self.assertEqual(audit_res["checks"]["imbalance_paise"], 0)
        self.assertTrue(audit_res["checks"]["paise_parity_verified"])
        self.assertTrue(audit_res["checks"]["immutability_triggers_active"])

    def test_paid_invoice_immutability_triggers(self):
        init_db()
        with connect() as conn:
            # Delete prior test production records if any
            try:
                conn.execute("UPDATE invoices SET status = 'cancelled' WHERE invoice_number = 'INV-PROD-TEST-IMM'")
                conn.execute("DELETE FROM invoices WHERE invoice_number = 'INV-PROD-TEST-IMM'")
            except sqlite3.IntegrityError:
                pass

            now = utc_now()
            conn.execute(
                """INSERT OR IGNORE INTO opportunities (id, title, source, amount_inr, status, created_at)
                   VALUES (8888, 'Prod Task', 'direct', 59000.0, 'won', ?)""",
                (now,),
            )
            conn.execute(
                """INSERT INTO invoices (
                    invoice_number, opportunity_id, client_name,
                    amount_subtotal, tax_rate, tax_amount, amount_total,
                    status, issued_at, due_at, paid_at, is_sandbox, amount_paid
                ) VALUES ('INV-PROD-TEST-IMM', 8888, 'Prod Client',
                          50000.0, 18.0, 9000.0, 59000.0,
                          'paid', ?, ?, ?, 0, 59000.0)""",
                (now, now, now),
            )
            conn.commit()

            # Attempting to modify amount on a paid production invoice MUST raise IntegrityError
            with self.assertRaises(sqlite3.IntegrityError) as ctx_upd:
                conn.execute(
                    "UPDATE invoices SET amount_total = 20000.0 WHERE invoice_number = 'INV-PROD-TEST-IMM'"
                )
            self.assertIn("modifying amounts of settled invoices is prohibited", str(ctx_upd.exception))

            # Attempting to delete a paid production invoice MUST raise IntegrityError
            with self.assertRaises(sqlite3.IntegrityError) as ctx_del:
                conn.execute(
                    "DELETE FROM invoices WHERE invoice_number = 'INV-PROD-TEST-IMM'"
                )
            self.assertIn("deleting settled invoices is prohibited", str(ctx_del.exception))

            # Cleanup
            conn.execute("UPDATE invoices SET status = 'cancelled' WHERE invoice_number = 'INV-PROD-TEST-IMM'")
            conn.execute("DELETE FROM invoices WHERE invoice_number = 'INV-PROD-TEST-IMM'")
            conn.commit()


if __name__ == "__main__":
    unittest.main()
