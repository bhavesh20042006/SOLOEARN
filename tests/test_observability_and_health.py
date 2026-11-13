import os
import unittest
from pathlib import Path
from unittest.mock import patch

from core.health import (
    check_database,
    check_llm,
    check_secrets_and_config,
    check_storage,
    run_system_health_audit,
)
from core.secrets_manager import SecretsManager, mask_secret


class SecretsManagerTests(unittest.TestCase):
    def test_mask_secret(self):
        self.assertEqual(mask_secret(""), "***")
        self.assertEqual(mask_secret("short"), "***")
        self.assertEqual(mask_secret("very_long_secret_12345"), "ver***345")

    def test_secrets_manager_validation(self):
        sm = SecretsManager()
        val = sm.validate_secrets()
        self.assertIn("required", val)
        self.assertIn("optional", val)
        self.assertIn("OLLAMA_BASE_URL", val["required"])

    def test_secrets_manager_get_secret(self):
        sm = SecretsManager()
        val = sm.get_secret("OLLAMA_BASE_URL", required=True)
        self.assertTrue(val.startswith("http"))

        with self.assertRaises(ValueError):
            sm.get_secret("NON_EXISTENT_KEY_12345", required=True)

        fallback = sm.get_secret("NON_EXISTENT_KEY_12345", required=False, default="def_val")
        self.assertEqual(fallback, "def_val")

    def test_safe_summary_does_not_expose_full_secrets(self):
        sm = SecretsManager()
        summary = sm.get_safe_summary()
        self.assertIn("OLLAMA_BASE_URL", summary)
        # Should either be masked or (not set), never full plain secret
        val = summary["OLLAMA_BASE_URL"]
        self.assertTrue("***" in val or val == "(not set)")


class HealthCheckTests(unittest.TestCase):
    def test_database_health(self):
        res = check_database()
        self.assertIn(res.status, ("ok", "degraded"))
        self.assertEqual(res.component, "database")
        self.assertGreaterEqual(res.latency_ms, 0)
        self.assertIn("journal_mode", res.details)

    def test_storage_health(self):
        res = check_storage()
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.component, "storage")
        self.assertGreater(res.details["free_space_mb"], 500)

    def test_secrets_health(self):
        res = check_secrets_and_config()
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.component, "secrets_config")

    def test_llm_health_handles_unreachable(self):
        res = check_llm()
        self.assertIn(res.status, ("ok", "degraded"))
        self.assertEqual(res.component, "local_llm")

    def test_full_system_health_audit(self):
        rep = run_system_health_audit()
        self.assertIn(rep.status, ("healthy", "degraded", "critical"))
        self.assertEqual(len(rep.checks), 4)
        rep_dict = rep.to_dict()
        self.assertIn("checks", rep_dict)
        self.assertIn("status", rep_dict)


if __name__ == "__main__":
    unittest.main()
