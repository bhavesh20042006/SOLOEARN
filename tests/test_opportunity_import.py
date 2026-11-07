from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config import ensure_directories
from core.db import init_db, add_opportunity, connect
from core.opportunity_import import (
    parse_and_validate_opportunities_csv,
    commit_imported_opportunities,
    validate_url,
    parse_bool,
)


class OpportunityImportTests(unittest.TestCase):
    def setUp(self):
        ensure_directories()
        init_db()
        with connect() as conn:
            conn.execute("DELETE FROM proposals")
            conn.execute("DELETE FROM revenue")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM opportunities")
            conn.execute("DELETE FROM audit_log")

    def test_parse_bool(self):
        self.assertTrue(parse_bool("true"))
        self.assertTrue(parse_bool("TRUE"))
        self.assertTrue(parse_bool("yes"))
        self.assertTrue(parse_bool("1"))
        self.assertTrue(parse_bool(True))
        self.assertTrue(parse_bool(1))
        self.assertFalse(parse_bool("false"))
        self.assertFalse(parse_bool("0"))
        self.assertFalse(parse_bool("no"))
        self.assertFalse(parse_bool(False))
        self.assertFalse(parse_bool(0))
        with self.assertRaises(ValueError):
            parse_bool("maybe")
        with self.assertRaises(ValueError):
            parse_bool(5)

    def test_validate_url(self):
        # Empty is permitted
        url, err = validate_url("")
        self.assertEqual(url, "")
        self.assertIsNone(err)

        url, err = validate_url(None)
        self.assertEqual(url, "")
        self.assertIsNone(err)

        # Valid http/https
        url, err = validate_url("https://example.com/job/123")
        self.assertEqual(url, "https://example.com/job/123")
        self.assertIsNone(err)

        url, err = validate_url("http://example.org/project")
        self.assertEqual(url, "http://example.org/project")
        self.assertIsNone(err)

        # Unsafe schemes
        _, err = validate_url("javascript:alert(1)")
        self.assertIsNotNone(err)
        self.assertIn("Unsafe URL scheme", err)

        _, err = validate_url("file:///C:/Windows/win.ini")
        self.assertIsNotNone(err)
        self.assertIn("Unsafe URL scheme", err)

        _, err = validate_url("ftp://files.example.com/data")
        self.assertIsNotNone(err)
        self.assertIn("Unsafe URL scheme", err)

        _, err = validate_url("data:text/html,test")
        self.assertIsNotNone(err)
        self.assertIn("Unsafe URL scheme", err)

        # Missing scheme or domain
        _, err = validate_url("www.example.com")
        self.assertIsNotNone(err)
        self.assertIn("missing scheme", err.lower())

    def test_missing_header_columns(self):
        csv_data = "title,source,amount_inr\nTask 1,Client A,5000\n"
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_opportunities_csv(csv_data)
        self.assertIn("Missing required columns", str(ctx.exception))

    def test_empty_csv(self):
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_opportunities_csv("")
        self.assertIn("empty", str(ctx.exception).lower())

    def test_valid_import_flow(self):
        csv_data = (
            "title,source,url,description,amount_inr,estimated_hours,skill_match_pct,payment_confidence_pct,risk_level,source_permitted\n"
            "Build KPI Dashboard,Direct Referral,https://example.com/rfp/1,Sales and KPI analysis,12000,6,90,85,3,true\n"
            "Inventory Clean,Internal Partner,,Cleaning up warehouse CSVs,4000,2,80,95,2,1\n"
        )
        preview = parse_and_validate_opportunities_csv(csv_data, check_db_duplicates=False)
        self.assertEqual(preview.total_rows, 2)
        self.assertEqual(len(preview.valid_rows), 2)
        self.assertEqual(len(preview.invalid_rows), 0)
        self.assertEqual(len(preview.duplicate_rows), 0)

        # Check conversions
        row1 = preview.valid_rows[0]
        self.assertEqual(row1.data["title"], "Build KPI Dashboard")
        self.assertEqual(row1.data["amount_inr"], 12000.0)
        self.assertEqual(row1.data["estimated_hours"], 6.0)
        self.assertAlmostEqual(row1.data["skill_match"], 0.9)
        self.assertAlmostEqual(row1.data["payment_confidence"], 0.85)
        self.assertEqual(row1.data["risk_level"], 3.0)
        self.assertTrue(row1.data["source_permitted"])
        self.assertGreater(row1.score, 0)

        row2 = preview.valid_rows[1]
        self.assertEqual(row2.data["url"], "")
        self.assertTrue(row2.data["source_permitted"])

    def test_malformed_and_missing_required_fields(self):
        csv_data = (
            "title,source,url,description,amount_inr,estimated_hours,skill_match_pct,payment_confidence_pct,risk_level,source_permitted\n"
            ",Direct Client,https://example.com/job/1,,5000,4,80,80,3,true\n"
            "Data Report,,https://example.com/job/2,,5000,4,80,80,3,true\n"
        )
        preview = parse_and_validate_opportunities_csv(csv_data, check_db_duplicates=False)
        self.assertEqual(preview.total_rows, 2)
        self.assertEqual(len(preview.valid_rows), 0)
        self.assertEqual(len(preview.invalid_rows), 2)

        # Row 2 (index 2) had empty title
        self.assertTrue(any("Title is required" in err for err in preview.invalid_rows[0].errors))
        # Row 3 (index 3) had empty source
        self.assertTrue(any("Source is required" in err for err in preview.invalid_rows[1].errors))

    def test_invalid_numeric_ranges(self):
        header = "title,source,url,description,amount_inr,estimated_hours,skill_match_pct,payment_confidence_pct,risk_level,source_permitted\n"
        rows = [
            # Negative amount
            ("Negative Budget", "Client A", "", "", "-100", "5", "80", "80", "2", "true"),
            # Zero estimated hours
            ("Zero Hours", "Client B", "", "", "5000", "0", "80", "80", "2", "true"),
            # Negative estimated hours
            ("Negative Hours", "Client C", "", "", "5000", "-2", "80", "80", "2", "true"),
            # Skill match pct > 100
            ("Skill Match Too High", "Client D", "", "", "5000", "5", "120", "80", "2", "true"),
            # Skill match pct < 0
            ("Skill Match Too Low", "Client E", "", "", "5000", "5", "-5", "80", "2", "true"),
            # Confidence pct > 100
            ("Confidence Too High", "Client F", "", "", "5000", "5", "80", "150", "2", "true"),
            # Risk level > 10
            ("Risk Too High", "Client G", "", "", "5000", "5", "80", "80", "15", "true"),
            # Non-numeric text in numbers
            ("Bad Numeric", "Client H", "", "", "not_a_number", "5", "80", "80", "2", "true"),
            # Invalid boolean
            ("Bad Boolean", "Client I", "", "", "5000", "5", "80", "80", "2", "unknown"),
        ]
        csv_data = header + "\n".join([",".join(r) for r in rows]) + "\n"
        preview = parse_and_validate_opportunities_csv(csv_data, check_db_duplicates=False)
        self.assertEqual(preview.total_rows, 9)
        self.assertEqual(len(preview.valid_rows), 0)
        self.assertEqual(len(preview.invalid_rows), 9)

        self.assertTrue(any("amount_inr cannot be negative" in err for err in preview.invalid_rows[0].errors))
        self.assertTrue(any("estimated_hours must be greater than zero" in err for err in preview.invalid_rows[1].errors))
        self.assertTrue(any("estimated_hours must be greater than zero" in err for err in preview.invalid_rows[2].errors))
        self.assertTrue(any("skill_match_pct must be between 0 and 100" in err for err in preview.invalid_rows[3].errors))
        self.assertTrue(any("skill_match_pct must be between 0 and 100" in err for err in preview.invalid_rows[4].errors))
        self.assertTrue(any("payment_confidence_pct must be between 0 and 100" in err for err in preview.invalid_rows[5].errors))
        self.assertTrue(any("risk_level must be between 0 and 10" in err for err in preview.invalid_rows[6].errors))
        self.assertTrue(any("must be a valid number" in err for err in preview.invalid_rows[7].errors))
        self.assertTrue(any("Invalid boolean" in err for err in preview.invalid_rows[8].errors))

    def test_unsafe_url_in_csv_row(self):
        csv_data = (
            "title,source,url,description,amount_inr,estimated_hours,skill_match_pct,payment_confidence_pct,risk_level,source_permitted\n"
            "Audit Script,Direct Client,javascript:doEvil(),Scope,3000,2,90,90,1,true\n"
            "File Theft,Direct Client,file:///etc/passwd,Scope,3000,2,90,90,1,true\n"
        )
        preview = parse_and_validate_opportunities_csv(csv_data, check_db_duplicates=False)
        self.assertEqual(preview.total_rows, 2)
        self.assertEqual(len(preview.valid_rows), 0)
        self.assertEqual(len(preview.invalid_rows), 2)
        self.assertTrue(any("Unsafe URL scheme" in err for err in preview.invalid_rows[0].errors))
        self.assertTrue(any("Unsafe URL scheme" in err for err in preview.invalid_rows[1].errors))

    def test_duplicate_handling_within_file(self):
        csv_data = (
            "title,source,url,description,amount_inr,estimated_hours,skill_match_pct,payment_confidence_pct,risk_level,source_permitted\n"
            "Unique Task A,Source Alpha,https://example.com/taskA,Desc,5000,4,80,80,2,true\n"
            "Unique Task A,Source Alpha,https://example.com/taskA-dup,Desc,5000,4,80,80,2,true\n"
            "Unique Task B,Source Beta,https://example.com/taskB,Desc,6000,4,80,80,2,true\n"
            "Unique Task C,Source Gamma,https://example.com/taskB,Desc,6000,4,80,80,2,true\n"
        )
        preview = parse_and_validate_opportunities_csv(csv_data, check_db_duplicates=False)
        self.assertEqual(preview.total_rows, 4)
        self.assertEqual(len(preview.valid_rows), 4)
        self.assertEqual(len(preview.duplicate_rows), 2)
        self.assertEqual(len(preview.importable_rows), 2)

        # Row 1 is not duplicate
        self.assertFalse(preview.valid_rows[0].is_duplicate)
        # Row 2 is duplicate by (title, source)
        self.assertTrue(preview.valid_rows[1].is_duplicate)
        self.assertIn("title & source", preview.valid_rows[1].duplicate_reason)
        # Row 3 is not duplicate
        self.assertFalse(preview.valid_rows[2].is_duplicate)
        # Row 4 is duplicate by URL
        self.assertTrue(preview.valid_rows[3].is_duplicate)
        self.assertIn("URL", preview.valid_rows[3].duplicate_reason)

    def test_duplicate_handling_against_database(self):
        # Insert a pre-existing opportunity into database
        existing_data = {
            "title": "Existing Client Gig",
            "source": "Platform Direct",
            "url": "https://example.com/existing-gig",
            "description": "Pre-existing",
            "amount_inr": 8000,
            "estimated_hours": 5,
            "skill_match": 0.8,
            "payment_confidence": 0.8,
            "risk_level": 2,
            "source_permitted": True,
        }
        add_opportunity(existing_data, score=800.0)

        csv_data = (
            "title,source,url,description,amount_inr,estimated_hours,skill_match_pct,payment_confidence_pct,risk_level,source_permitted\n"
            "Existing Client Gig,Platform Direct,,Desc,8000,5,80,80,2,true\n"
            "Brand New Gig,Platform Direct,https://example.com/existing-gig,Desc,9000,5,80,80,2,true\n"
            "Brand New Gig 2,Platform Direct,https://example.com/novel-gig,Desc,9000,5,80,80,2,true\n"
        )
        preview = parse_and_validate_opportunities_csv(csv_data, check_db_duplicates=True)
        self.assertEqual(preview.total_rows, 3)
        self.assertEqual(len(preview.duplicate_rows), 2)
        self.assertEqual(len(preview.importable_rows), 1)

        # Check reasons
        self.assertIn("database", preview.valid_rows[0].duplicate_reason)
        self.assertIn("database", preview.valid_rows[1].duplicate_reason)
        self.assertFalse(preview.valid_rows[2].is_duplicate)

        # Committing with skip_duplicates=True only creates 1 item
        new_ids = commit_imported_opportunities(preview.valid_rows, skip_duplicates=True)
        self.assertEqual(len(new_ids), 1)


if __name__ == "__main__":
    unittest.main()
