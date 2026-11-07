from pathlib import Path
import tempfile
import unittest

from core.scoring import opportunity_score
from core.config import WORKSPACES_DIR, ensure_directories, safe_workspace_path
from core.analytics import create_analytics_bundle
from core.llm import OllamaClient


def setUpModule():
    ensure_directories()


class ScoringTests(unittest.TestCase):
    def test_unpermitted_source_scores_zero(self):
        self.assertEqual(opportunity_score(10000, 5, 1, 1, 0, False), 0)

    def test_score_decreases_with_risk(self):
        low = opportunity_score(10000, 5, 0.8, 0.8, 1, True)
        high = opportunity_score(10000, 5, 0.8, 0.8, 8, True)
        self.assertGreater(low, high)

    def test_invalid_hours_rejected(self):
        with self.assertRaises(ValueError):
            opportunity_score(1000, 0, 1, 1, 1, True)


class WorkspaceTests(unittest.TestCase):
    def test_ollama_client_rejects_non_local_host(self):
        with self.assertRaises(ValueError):
            OllamaClient("http://127.0.0.1.attacker.example:11434")

    def test_traversal_blocked(self):
        with self.assertRaises(ValueError):
            safe_workspace_path("..", "outside.txt")

    def test_path_stays_in_workspace(self):
        path = safe_workspace_path("unit_test")
        self.assertTrue(path == WORKSPACES_DIR.resolve() / "unit_test")


class AnalyticsTests(unittest.TestCase):
    def test_csv_bundle_created(self):
        csv = b"city,sales\nDelhi,100\nNoida,200\nDelhi,100\n"
        with tempfile.TemporaryDirectory(dir=WORKSPACES_DIR) as temp:
            # Run against a unique task identifier, then verify the resulting archive.
            task_id = 987654
            result = create_analytics_bundle(csv, "test.csv", task_id)
            self.assertTrue(result.exists())
            self.assertEqual(result.name, "deliverables.zip")

    def test_non_csv_rejected(self):
        with self.assertRaises(ValueError):
            create_analytics_bundle(b"hello", "test.txt", 987655)


if __name__ == "__main__":
    unittest.main()
