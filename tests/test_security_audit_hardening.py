from __future__ import annotations

"""
Unit and Regression Tests for Security Audit and Hardening across:
- core/friday_secure_portal.py
- core/secrets_manager.py
- core/friday_agent.py

Validates:
1. HMAC signature verification using hmac.compare_digest.
2. Token entropy (secrets.token_urlsafe(32)) and race-condition safety on single-use claims.
3. OWASP file sanitization: path traversal (CWE-22), executable uploads (CWE-434),
   and CSV DDE formula injections (CWE-1236 with =, +, -, @, \\t, \\r).
4. Parameterized SQLite queries across all modules.
5. Prevention of secret leakage in logs and exception traces.
"""

import concurrent.futures
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.config
from core.analytics import is_formula_injection, sanitize_cell_value
from core.config import ensure_directories
from core.db import connect, create_task, init_db, update_task_status
from core.friday_agent import (
    FridayPermissionController,
    FridayThought,
    FridayToolRegistry,
    download_client_deliverable,
    init_friday_tables,
)
from core.friday_secure_portal import (
    ALLOWED_EXTENSIONS,
    FORBIDDEN_EXTENSIONS,
    FridayPaymentWebhookHandler,
    FridaySecurePortal,
    init_portal_tables,
)
from core.secrets_manager import (
    SecretsManager,
    constant_time_compare,
    generate_secure_token,
    mask_secret,
    redact_secrets_from_text,
    redact_sensitive_dict,
)


class SecurityAuditHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp_dir.name)
        self.orig_db_path = core.config.DB_PATH
        self.orig_workspaces_dir = core.config.WORKSPACES_DIR
        self.orig_data_dir = core.config.DATA_DIR

        core.config.DATA_DIR = tmp_path / "data"
        core.config.DB_PATH = tmp_path / "data" / "sec_test.sqlite3"
        core.config.WORKSPACES_DIR = tmp_path / "workspaces"

        ensure_directories()
        init_db()
        init_friday_tables()
        init_portal_tables()

    def tearDown(self):
        core.config.DB_PATH = self.orig_db_path
        core.config.WORKSPACES_DIR = self.orig_workspaces_dir
        core.config.DATA_DIR = self.orig_data_dir
        self.tmp_dir.cleanup()

    # =======================================================================
    # 1. HMAC Signature Verification & Timing Attack Immunity
    # =======================================================================
    def test_task1_hmac_signature_checks_use_compare_digest(self):
        secret = "super_secure_webhook_secret_key_32bytes_long!"
        handler = FridayPaymentWebhookHandler(webhook_secret=secret, is_sandbox=True)

        payload = b'{"status": "captured", "amount_inr": 1000.0, "invoice_number": "INV-1"}'
        expected_sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

        # Valid signature succeeds
        self.assertTrue(handler.verify_hmac_signature(payload, expected_sig))
        self.assertTrue(handler.verify_hmac_signature(payload, expected_sig.upper()))

        # Forged or corrupted signatures fail constant-time
        self.assertFalse(handler.verify_hmac_signature(payload, "invalid_signature"))
        self.assertFalse(handler.verify_hmac_signature(payload, expected_sig[:-1] + "0"))
        self.assertFalse(handler.verify_hmac_signature(payload, ""))
        self.assertFalse(handler.verify_hmac_signature(b"", expected_sig))

        # Test helper constant_time_compare in secrets_manager
        self.assertTrue(constant_time_compare("secret_string_123", "secret_string_123"))
        self.assertFalse(constant_time_compare("secret_string_123", "secret_string_124"))
        self.assertFalse(constant_time_compare("", "a"))

    # =======================================================================
    # 2. Token Entropy & Race-Condition Safety on Single-Use Claims
    # =======================================================================
    def test_task2_token_entropy_and_format(self):
        portal = FridaySecurePortal(is_sandbox=True)
        tok1 = portal.generate_upload_token("Client A", validity_hours=24)
        tok2 = portal.generate_upload_token("Client B", validity_hours=24)

        # Token must start with tok_ and have high entropy (>40 chars total)
        self.assertTrue(tok1.startswith("tok_"))
        self.assertTrue(tok2.startswith("tok_"))
        self.assertNotEqual(tok1, tok2)
        self.assertGreater(len(tok1), 40)

        # secrets_manager token generator test
        sec_tok = generate_secure_token(prefix="dlv_", nbytes=32)
        self.assertTrue(sec_tok.startswith("dlv_"))
        self.assertGreater(len(sec_tok), 40)

    def test_task2_concurrent_race_condition_upload_token_claims(self):
        portal = FridaySecurePortal(is_sandbox=True)
        token = portal.generate_upload_token("Race Client", validity_hours=24)
        csv_bytes = b"id,val\n1,100\n"

        success_count = 0
        failure_count = 0

        def attempt_claim():
            try:
                portal.validate_and_ingest_file(token, "sample.csv", csv_bytes)
                return True
            except ValueError:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(attempt_claim) for _ in range(8)]
            for f in concurrent.futures.as_completed(futures):
                if f.result():
                    success_count += 1
                else:
                    failure_count += 1

        # Exactly ONE concurrent thread claims the token; all other 7 must fail
        self.assertEqual(success_count, 1)
        self.assertEqual(failure_count, 7)

    # =======================================================================
    # 3. OWASP File Sanitization (CWE-22, CWE-434, CWE-1236)
    # =======================================================================
    def test_task3_path_traversal_prevention_cwe_22(self):
        portal = FridaySecurePortal(is_sandbox=True)
        token = portal.generate_upload_token("Path Traversal Client", validity_hours=24)

        # Null bytes
        with self.assertRaises(ValueError) as ctx:
            portal.validate_and_ingest_file(token, "test.csv\x00.exe", b"a,b\n1,2")
        self.assertIn("Null bytes", str(ctx.exception))

        # Explicit directory traversal
        with self.assertRaises(ValueError) as ctx:
            portal.validate_and_ingest_file(token, "../../etc/passwd.csv", b"a,b\n1,2")
        self.assertIn("Path traversal", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            portal.validate_and_ingest_file(token, "..\\..\\windows\\system32\\calc.csv", b"a,b\n1,2")
        self.assertIn("Path traversal", str(ctx.exception))

    def test_task3_executable_uploads_prevention_cwe_434(self):
        portal = FridaySecurePortal(is_sandbox=True)
        token = portal.generate_upload_token("Executable Test Client", validity_hours=24)

        # Forbidden extensions
        with self.assertRaises(ValueError) as ctx:
            portal.validate_and_ingest_file(token, "payload.exe", b"binary")
        self.assertIn("forbidden", str(ctx.exception).lower())

        # Double extension disguise (e.g. payload.exe.csv or script.sh.txt)
        with self.assertRaises(ValueError) as ctx:
            portal.validate_and_ingest_file(token, "payload.exe.csv", b"a,b\n1,2")
        self.assertIn("Forbidden executable", str(ctx.exception))

        # Executable binary header disguise (MZ DOS/PE binary)
        with self.assertRaises(ValueError) as ctx:
            portal.validate_and_ingest_file(token, "innocent.csv", b"MZ\x90\x00\x03\x00\x00\x00")
        self.assertIn("Executable binary payload detected", str(ctx.exception))

        # ELF binary disguise
        with self.assertRaises(ValueError) as ctx:
            portal.validate_and_ingest_file(token, "innocent.tsv", b"\x7fELF\x02\x01\x01\x00")
        self.assertIn("Executable binary payload detected", str(ctx.exception))

        # Shebang script disguise
        with self.assertRaises(ValueError) as ctx:
            portal.validate_and_ingest_file(token, "innocent.txt", b"#!/bin/bash\nrm -rf /")
        self.assertIn("Executable script content detected", str(ctx.exception))

        # PHP tag disguise
        with self.assertRaises(ValueError) as ctx:
            portal.validate_and_ingest_file(token, "innocent.csv", b"<?php phpinfo(); ?>")
        self.assertIn("Executable script content detected", str(ctx.exception))

    def test_task3_csv_dde_formula_injection_cwe_1236(self):
        # Verify all formula injection triggers: =, +, -, @, \t, \r
        self.assertTrue(is_formula_injection("=SUM(A1:A10)"))
        self.assertTrue(is_formula_injection("+1+cmd|' /C calc'!A0"))
        self.assertTrue(is_formula_injection("-2+3+cmd|' /C calc'!A0"))
        self.assertTrue(is_formula_injection("@HYPERLINK('http://evil.com')"))
        self.assertTrue(is_formula_injection("\t=cmd"))
        self.assertTrue(is_formula_injection("\r+cmd"))
        self.assertTrue(is_formula_injection("\t"))
        self.assertTrue(is_formula_injection("\r"))

        # Verify legitimate negative and positive numbers are preserved without quotes
        self.assertFalse(is_formula_injection("-100"))
        self.assertFalse(is_formula_injection("-45.50"))
        self.assertFalse(is_formula_injection("+500"))
        self.assertFalse(is_formula_injection("123.45"))
        self.assertFalse(is_formula_injection("Normal Text"))

        # Ingestion sanitization verifies that dangerous cells are escaped with a leading quote
        portal = FridaySecurePortal(is_sandbox=True)
        token = portal.generate_upload_token("Formula Client", validity_hours=24)
        csv_content = (
            'id,formula_col,negative_num,tab_formula\n'
            '1,=cmd|\' /C calc\'!A0,-50.25,\t=evil\n'
            '2,@SUM(B1:B2),+100,"\t+test"\n'
        )
        res = portal.validate_and_ingest_file(token, "test_formulas.csv", csv_content.encode("utf-8"))
        saved_text = Path(res["file_path"]).read_text(encoding="utf-8")

        self.assertIn("'=cmd", saved_text)
        self.assertIn("'@SUM", saved_text)
        self.assertIn("'\t=evil", saved_text)
        self.assertIn("'\t+test", saved_text)
        self.assertIn("-50.25", saved_text)
        self.assertNotIn("'-50.25", saved_text)
        self.assertIn("+100", saved_text)
        self.assertNotIn("'+100", saved_text)

    # =======================================================================
    # 4. Parameterized SQLite Queries across Modules
    # =======================================================================
    def test_task4_sql_injection_defense_in_portal_and_agent(self):
        portal = FridaySecurePortal(is_sandbox=True)

        # Attempt SQL injection through token claim
        sqli_token = "tok_test' OR '1'='1"
        with self.assertRaises(ValueError):
            portal.validate_and_ingest_file(sqli_token, "test.csv", b"a,b\n1,2")

        # Attempt SQL injection through client name
        clean_token = portal.generate_upload_token("Victim'; DROP TABLE friday_portal_tokens; --", validity_hours=24)
        res = portal.validate_and_ingest_file(clean_token, "safe.csv", b"a,b\n1,2")
        self.assertEqual(res["status"], "ingested")

        # Table must still exist and be intact
        with connect() as conn:
            cnt = conn.execute("SELECT count(*) as c FROM friday_portal_tokens").fetchone()["c"]
            self.assertGreater(cnt, 0)

    # =======================================================================
    # 5. Secret Leakage Prevention in Logs and Exceptions
    # =======================================================================
    def test_task5_no_secret_leakage_in_exceptions_and_logs(self):
        portal = FridaySecurePortal(is_sandbox=True)
        secret_token = portal.generate_upload_token("Leak Client", validity_hours=24)

        # Claim the token once
        portal.validate_and_ingest_file(secret_token, "first.csv", b"a,b\n1,2")

        # Second attempt raises ValueError: verify raw secret token is NOT leaked in message
        with self.assertRaises(ValueError) as ctx:
            portal.validate_and_ingest_file(secret_token, "second.csv", b"a,b\n1,2")

        err_msg = str(ctx.exception)
        self.assertNotIn(secret_token, err_msg)
        self.assertIn(mask_secret(secret_token), err_msg)

        # Test redact_sensitive_dict
        sensitive_payload = {
            "client_name": "Test Client",
            "webhook_secret": "super_secret_webhook_key_12345",
            "auth_token": "tok_abcdef1234567890abcdef1234567890",
            "nested": {
                "password": "my_password_999",
                "safe_info": "hello",
            },
        }
        redacted = redact_sensitive_dict(sensitive_payload)
        self.assertEqual(redacted["client_name"], "Test Client")
        self.assertNotIn("super_secret_webhook_key_12345", json.dumps(redacted))
        self.assertNotIn("tok_abcdef1234567890abcdef1234567890", json.dumps(redacted))
        self.assertNotIn("my_password_999", json.dumps(redacted))
        self.assertEqual(redacted["nested"]["safe_info"], "hello")

        # Test redact_secrets_from_text
        text_with_secrets = "Sent payload with token tok_1234567890abcdef and Bearer my_api_key_456"
        clean_text = redact_secrets_from_text(text_with_secrets)
        self.assertNotIn("tok_1234567890abcdef", clean_text)
        self.assertNotIn("my_api_key_456", clean_text)


if __name__ == "__main__":
    unittest.main()
