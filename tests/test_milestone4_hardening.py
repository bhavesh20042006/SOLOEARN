from __future__ import annotations

import socket
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from core.capability_matcher import CapabilityMatcher
from core.config import LEADS_DROPZONE_DIR, ensure_directories
from core.db import add_opportunity, connect, init_db
from core.discovery_engine import (
    DiscoveryEngine,
    extract_budget_inr,
    fetch_public_feed,
    parse_feed_xml,
    parse_json_leads,
    safe_dropzone_path,
    validate_source_url,
)
from core.proposal_generator import generate_proposal_template


class Milestone4HardeningTests(unittest.TestCase):
    def setUp(self):
        ensure_directories()
        init_db()
        with connect() as conn:
            conn.execute("DELETE FROM proposals")
            conn.execute("DELETE FROM revenue")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM opportunities")
            conn.execute("DELETE FROM discovery_sources")

    def test_unknown_budget_is_zero_not_invented(self):
        self.assertEqual(extract_budget_inr("Data cleaning project; budget negotiable"), 0.0)
        self.assertEqual(extract_budget_inr("Budget $100"), 0.0)
        self.assertEqual(extract_budget_inr("Budget INR 12,500"), 12500.0)

    def test_unknown_budget_not_committed_as_5000(self):
        batch = DiscoveryEngine.process_raw_leads([
            {"title": "Clean CSV sales data", "description": "Clean and profile CSV sales data", "url": "https://example.com/a"}
        ], "Test", auto_commit=False)
        self.assertEqual(batch.leads[0].amount_inr, 0.0)
        self.assertEqual(batch.leads[0].score, 0.0)

    def test_mixed_supported_and_unsupported_scope_is_rejected(self):
        result = CapabilityMatcher.match(
            "Clean CSV and build a mobile app",
            "Create Excel sales report and implement the Android app backend.",
        )
        self.assertFalse(result.is_suitable)
        self.assertTrue(result.unsupported_requirements)

    def test_mixed_scope_is_not_imported(self):
        batch = DiscoveryEngine.process_raw_leads([
            {"title": "CSV analytics plus React Native app", "description": "Clean sales CSV and build React Native mobile app", "url": "https://example.com/mixed"}
        ], "Test", auto_commit=True)
        self.assertEqual(batch.leads_imported, 0)
        self.assertEqual(batch.unsuitable_skipped, 1)

    def test_dns_resolution_to_private_ip_is_blocked(self):
        def fake_resolver(host, port, type=None):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]
        ok, reason = validate_source_url("https://feed.example/jobs", resolve_dns=True, resolver=fake_resolver)
        self.assertFalse(ok)
        self.assertIn("non-public", reason.lower())

    def test_fetch_pins_and_rejects_private_dns_before_connect(self):
        def fake_resolver(host, port, type=None):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        def must_not_connect(address, timeout=None):
            raise AssertionError("Socket must not be opened for a private DNS result")
        with self.assertRaisesRegex(ValueError, "non-public"):
            fetch_public_feed("https://public-looking.example/feed.xml", resolver=fake_resolver, socket_factory=must_not_connect)

    def test_nonstandard_ports_and_embedded_credentials_rejected(self):
        self.assertFalse(validate_source_url("https://example.com:8443/feed")[0])
        self.assertFalse(validate_source_url("https://user:pass@example.com/feed")[0])



    def test_public_fetch_connects_to_validated_ip_and_returns_limited_body(self):
        class FakeSocket:
            def settimeout(self, _timeout):
                pass

        class FakeResponse:
            status = 200
            def getheader(self, name):
                return "4" if name.lower() == "content-length" else None
            def read(self, _limit):
                return b"<rss/>"

        class FakeConnection:
            def __init__(self, *args, **kwargs):
                pass
            def putrequest(self, *args, **kwargs):
                pass
            def putheader(self, *args, **kwargs):
                pass
            def endheaders(self):
                pass
            def getresponse(self):
                return FakeResponse()
            def close(self):
                pass

        connected = []
        def fake_resolver(host, port, type=None):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        def fake_socket(address, timeout=None):
            connected.append(address)
            return FakeSocket()

        with patch("core.discovery_engine.http.client.HTTPConnection", FakeConnection):
            body = fetch_public_feed(
                "http://feeds.example/public.xml",
                resolver=fake_resolver,
                socket_factory=fake_socket,
            )
        self.assertEqual(body, b"<rss/>")
        self.assertEqual(connected, [("93.184.216.34", 80)])

    def test_redirect_to_private_ip_is_blocked_before_second_connection(self):
        import http.client

        class FakeSocket:
            def settimeout(self, _timeout):
                pass

        class FakeResponse:
            status = 302
            def getheader(self, name):
                return "http://127.0.0.1:11434/api/tags" if name.lower() == "location" else None
            def read(self, _limit):
                return b""

        class FakeConnection:
            def __init__(self, *args, **kwargs):
                self.sock = None
            def putrequest(self, *args, **kwargs):
                pass
            def putheader(self, *args, **kwargs):
                pass
            def endheaders(self):
                pass
            def getresponse(self):
                return FakeResponse()
            def close(self):
                pass

        def fake_resolver(host, port, type=None):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

        with patch("core.discovery_engine.http.client.HTTPConnection", FakeConnection):
            with self.assertRaisesRegex(ValueError, "Redirect blocked"):
                fetch_public_feed(
                    "http://feed.example/public.xml",
                    resolver=fake_resolver,
                    socket_factory=lambda address, timeout=None: FakeSocket(),
                )

    def test_local_path_outside_dropzone_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "leads.json"
            outside.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                safe_dropzone_path(outside, expect_directory=False)

    def test_symlink_inside_dropzone_is_rejected(self):
        ensure_directories()
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.json"
            outside.write_text("[]", encoding="utf-8")
            link = LEADS_DROPZONE_DIR / "audit_symlink.json"
            try:
                if link.exists() or link.is_symlink():
                    link.unlink()
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("Symlink creation is not available on this platform")
            try:
                with self.assertRaises(ValueError):
                    safe_dropzone_path(link, expect_directory=False)
            finally:
                if link.exists() or link.is_symlink():
                    link.unlink()


    def test_bundled_sample_feeds_stay_in_dropzone_and_are_readable(self):
        rss = LEADS_DROPZONE_DIR / "sample_rss.xml"
        js = LEADS_DROPZONE_DIR / "sample_leads.json"
        self.assertTrue(safe_dropzone_path(rss).is_file())
        self.assertTrue(safe_dropzone_path(js).is_file())
        self.assertGreater(len(parse_feed_xml(rss.read_bytes(), "Sample RSS")), 0)
        self.assertGreater(len(parse_json_leads(js.read_bytes(), "Sample JSON")), 0)


    def test_bundled_rss_source_polls_inside_dropzone(self):
        from core.db import add_discovery_source, get_discovery_source
        source_id = add_discovery_source(
            "Sample RSS in dropzone", "rss", str(LEADS_DROPZONE_DIR / "sample_rss.xml"),
            is_permitted=True, is_active=True,
        )
        result = DiscoveryEngine.poll_source(get_discovery_source(source_id), auto_commit=True)
        self.assertGreater(result.total_found, 0)
        self.assertGreater(result.leads_imported, 0)

    def test_unsafe_lead_url_is_rejected_before_import(self):
        batch = DiscoveryEngine.process_raw_leads([
            {"title": "CSV dashboard", "description": "Clean sales CSV and generate Excel report", "url": "javascript:alert(1)"}
        ], "Test", auto_commit=True)
        self.assertEqual(batch.leads_imported, 0)
        self.assertEqual(batch.unsuitable_skipped, 1)


    def test_legacy_bundled_sample_paths_remain_allowed_for_existing_db_sources(self):
        from core.config import PROJECT_ROOT
        legacy_rss = PROJECT_ROOT / "data" / "sample_feeds" / "sample_rss.xml"
        legacy_json = PROJECT_ROOT / "data" / "sample_feeds" / "sample_leads.json"
        self.assertTrue(safe_dropzone_path(legacy_rss).is_file())
        self.assertTrue(safe_dropzone_path(legacy_json).is_file())
        with self.assertRaises(ValueError):
            safe_dropzone_path(legacy_rss.parent, expect_directory=True)

    def test_xml_dtd_rejected(self):
        payload = b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel></channel></rss>'
        with self.assertRaisesRegex(ValueError, "DTD and entity"):
            parse_feed_xml(payload)

    def test_oversized_json_rejected(self):
        with self.assertRaisesRegex(ValueError, "maximum allowed size"):
            parse_json_leads(b" " * (2 * 1024 * 1024 + 1))

    def test_proposal_does_not_invent_unknown_budget_or_deadline(self):
        opp = {
            "title": "Clean retail CSV data",
            "description": "Clean a retail sales CSV and generate an Excel report.",
            "amount_inr": 0,
            "estimated_hours": 0,
        }
        draft = generate_proposal_template(opp)
        self.assertIn("budget. No price is assumed", draft)
        self.assertIn("Delivery timing will be confirmed only", draft)
        self.assertNotIn("₹5,000", draft)
        self.assertNotIn("within 24 hours", draft)

    def test_proposal_does_not_offer_fixed_quote_for_listed_budget(self):
        opp = {
            "title": "Retail sales Excel report",
            "description": "Prepare Excel sales analysis and summary charts.",
            "amount_inr": 7000,
            "estimated_hours": 3,
        }
        draft = generate_proposal_template(opp)
        self.assertIn("indicative budget of ₹7,000.00", draft)
        self.assertIn("not an accepted quote", draft)
        self.assertNotIn("fixed milestone", draft.lower())


if __name__ == "__main__":
    unittest.main()
