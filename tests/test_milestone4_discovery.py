from __future__ import annotations

import json
import unittest
from pathlib import Path

from core.capability_matcher import CapabilityMatcher
from core.config import ensure_directories
from core.db import (
    add_discovery_source,
    add_opportunity,
    connect,
    delete_discovery_source,
    get_discovery_source,
    get_opportunity,
    get_proposal,
    init_db,
    list_discovery_sources,
    list_proposals,
    update_discovery_source_polled,
    update_proposal_status,
)
from core.discovery_engine import (
    DiscoveryEngine,
    compute_content_hash,
    extract_budget_inr,
    parse_feed_xml,
    parse_json_leads,
    validate_source_url,
)
from core.proposal_generator import authorize_proposal, draft_tailored_proposal, generate_proposal_template
from core.risk_analyzer import RiskAnalyzer


SAMPLE_RSS_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0">
<channel>
  <title>Analytics Jobs</title>
  <link>https://example.com</link>
  <item>
    <title>E-commerce Sales CSV Cleanup</title>
    <link>https://example.com/job/101</link>
    <description>Clean monthly sales CSV data, deduplicate customer entries, and create an Excel dashboard. Budget: ₹6,000.</description>
  </item>
  <item>
    <title>Telegram Video Creator</title>
    <link>https://example.com/job/102</link>
    <description>Make TikTok videos. Contact on Telegram @scamchannel. Pay in crypto.</description>
  </item>
</channel>
</rss>
""".encode("utf-8")

SAMPLE_ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Freelance Atom Feed</title>
  <entry>
    <title>Quarterly Financial Excel Reporting</title>
    <link href="https://example.com/job/201" />
    <summary>Analyze financial records, summarize column distributions, and format workbook. Budget: $120.</summary>
  </entry>
</feed>
""".encode("utf-8")


class Milestone4DiscoveryTests(unittest.TestCase):
    def setUp(self):
        ensure_directories()
        init_db()
        with connect() as conn:
            conn.execute("DELETE FROM invoices")
            conn.execute("DELETE FROM proposals")
            conn.execute("DELETE FROM revenue")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM opportunities")
            conn.execute("DELETE FROM discovery_sources")
            conn.execute("DELETE FROM audit_log")

    def test_capability_matcher_identifies_data_analytics(self):
        title = "Clean CSV and build multi-sheet Excel sales workbook"
        desc = "Deduplicate 5,000 customer rows, profile missing cells, and plot distribution charts."
        res = CapabilityMatcher.match(title, desc)
        self.assertTrue(res.is_suitable)
        self.assertGreaterEqual(res.skill_match, 0.7)
        self.assertIn("csv_data_cleaning", res.matched_capabilities)
        self.assertIn("excel_compilation", res.matched_capabilities)
        self.assertEqual(len(res.unsupported_requirements), 0)

    def test_capability_matcher_rejects_unsupported_domains(self):
        title = "Develop iOS and Android mobile app in Flutter"
        desc = "Build fullstack mobile app with WordPress backend, video editing, and 3D Blender models."
        res = CapabilityMatcher.match(title, desc)
        self.assertFalse(res.is_suitable)
        self.assertLessEqual(res.skill_match, 0.2)
        self.assertGreater(len(res.unsupported_requirements), 0)
        self.assertIn("mobile app", res.unsupported_requirements)

    def test_risk_analyzer_flags_telegram_and_crypto(self):
        title = "Fast Cash - Contact Telegram @fastcrypto"
        desc = "Work from home. Pay $500 in Bitcoin or USDT after registration fee deposit. Contact via WhatsApp."
        res = RiskAnalyzer.analyze(title, desc, amount_inr=50000, estimated_hours=1, source_permitted=True)
        self.assertTrue(res.is_suspicious)
        self.assertGreaterEqual(res.risk_level, 6.0)
        self.assertLessEqual(res.payment_confidence, 0.5)
        self.assertTrue(any("Telegram" in f for f in res.risk_flags))
        self.assertTrue(any("cryptocurrency" in f.lower() for f in res.risk_flags))

    def test_risk_analyzer_flags_extreme_unrealistic_hourly_rates(self):
        # 1. Absurdly high compensation (₹100,000 for 1 hr of data entry)
        res_high = RiskAnalyzer.analyze("Simple CSV Copy", "Copy numbers", amount_inr=100000, estimated_hours=1)
        self.assertTrue(any("Unusually high" in f for f in res_high.risk_flags))
        self.assertGreater(res_high.risk_level, 3.5)

        # 2. Extreme exploitation (₹50 for 10 hours = ₹5/hr)
        res_low = RiskAnalyzer.analyze("Massive CSV Entry", "Type 10,000 rows", amount_inr=50, estimated_hours=10)
        self.assertTrue(any("Extremely low" in f for f in res_low.risk_flags))

    def test_risk_analyzer_validates_safe_lead(self):
        title = "Retail Sales Data Analysis & Excel Format"
        desc = "Client referral for analyzing quarterly store revenue. Fixed milestone."
        res = RiskAnalyzer.analyze(title, desc, amount_inr=6000, estimated_hours=3, source_permitted=True)
        self.assertFalse(res.is_suspicious)
        self.assertLessEqual(res.risk_level, 3.0)
        self.assertGreaterEqual(res.payment_confidence, 0.8)

    def test_parse_rss_feed_xml(self):
        items = parse_feed_xml(SAMPLE_RSS_XML, source_name="Test RSS")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "E-commerce Sales CSV Cleanup")
        self.assertEqual(items[0]["url"], "https://example.com/job/101")
        self.assertIn("Excel dashboard", items[0]["description"])
        self.assertEqual(items[1]["title"], "Telegram Video Creator")

    def test_parse_atom_feed_xml(self):
        items = parse_feed_xml(SAMPLE_ATOM_XML, source_name="Test Atom")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Quarterly Financial Excel Reporting")
        self.assertEqual(items[0]["url"], "https://example.com/job/201")
        self.assertIn("Analyze financial records", items[0]["description"])

    def test_parse_json_leads_api(self):
        json_payload = json.dumps({
            "items": [
                {
                    "title": "Hospitality Booking CSV Audit",
                    "description": "Clean hotel reservations CSV and generate charts.",
                    "url": "https://example.com/lead/1",
                    "budget": 7500,
                    "hours": 3.5,
                }
            ]
        })
        items = parse_json_leads(json_payload, source_name="Test API")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Hospitality Booking CSV Audit")
        self.assertEqual(items[0]["amount_inr"], 7500)
        self.assertEqual(items[0]["estimated_hours"], 3.5)

    def test_ssrf_protection_blocks_localhost_and_private_ips(self):
        # 1. Localhost blocked
        valid, err = validate_source_url("http://localhost:8080/feed.xml")
        self.assertFalse(valid)
        self.assertIn("localhost", err.lower())

        valid, err = validate_source_url("http://127.0.0.1:11434/api/tags")
        self.assertFalse(valid)
        self.assertIn("localhost", err.lower())

        # 2. Private LAN IPs blocked
        valid, err = validate_source_url("http://192.168.1.50/leads.json")
        self.assertFalse(valid)
        self.assertIn("private or reserved", err.lower())

        valid, err = validate_source_url("http://10.0.0.1/rss")
        self.assertFalse(valid)
        self.assertIn("private or reserved", err.lower())

        # 3. Disallowed scheme
        valid, err = validate_source_url("ftp://files.example.com/feed.xml")
        self.assertFalse(valid)
        self.assertIn("invalid scheme", err.lower())

        # 4. Valid external HTTPS URL allowed
        valid, err = validate_source_url("https://example.com/jobs/rss.xml")
        self.assertTrue(valid)

    def test_deduplication_via_url_and_content_fingerprint(self):
        # Add existing opportunity to SQLite
        opp_id = add_opportunity({
            "title": "Existing Analytics Project",
            "source": "Partner Feed",
            "url": "https://example.com/unique-lead-url",
            "description": "Comprehensive CSV cleanup.",
            "amount_inr": 5000,
            "estimated_hours": 2,
            "skill_match": 1.0,
            "payment_confidence": 1.0,
            "risk_level": 1,
            "source_permitted": True,
            "content_hash": compute_content_hash("Existing Analytics Project", "Comprehensive CSV cleanup."),
        }, score=1000)
        self.assertGreater(opp_id, 0)

        # Batch contains: 1. same URL, 2. same title & description (content hash), 3. novel lead
        raw_items = [
            {"title": "Different Title", "description": "Different desc", "url": "https://example.com/unique-lead-url"},
            {"title": "Existing Analytics Project", "description": "Comprehensive CSV cleanup.", "url": "https://example.com/other-url"},
            {"title": "Brand New Inventory Analysis", "description": "Clean stock CSV data.", "url": "https://example.com/brand-new-job"},
        ]

        batch = DiscoveryEngine.process_raw_leads(raw_items, source_name="Test Source", source_permitted=True, auto_commit=True)
        self.assertEqual(batch.total_found, 3)
        self.assertEqual(batch.duplicates_skipped, 2)
        self.assertEqual(batch.leads_imported, 1)
        self.assertEqual(batch.leads[2].title, "Brand New Inventory Analysis")

    def test_discovery_batch_processing_and_scoring(self):
        raw_items = [
            {
                "title": "Shopify Sales CSV KPI Dashboard",
                "description": "Analyze retail sales CSV, deduplicate, calculate profit margins, and compile Excel report. Budget: ₹8,000 for 4 hours.",
                "url": "https://example.com/job/batch-kpi-1",
            }
        ]
        batch = DiscoveryEngine.process_raw_leads(raw_items, source_name="Verified Inbound", source_permitted=True, auto_commit=True)
        self.assertEqual(batch.leads_imported, 1)
        lead = batch.leads[0]
        self.assertTrue(lead.is_suitable)
        self.assertEqual(lead.amount_inr, 8000.0)
        self.assertGreater(lead.score, 500.0)

    def test_proposal_generator_deterministic_template(self):
        opp_id = add_opportunity({
            "title": "Hospitality Booking Trends",
            "source": "Direct",
            "url": "https://example.com/lead/hotel",
            "description": "Need data analyst to clean reservation CSV and prepare multi-tab Excel workbook.",
            "amount_inr": 7000,
            "estimated_hours": 3,
            "skill_match": 0.9,
            "payment_confidence": 0.9,
            "risk_level": 2,
            "source_permitted": True,
        }, score=1200)

        opp = get_opportunity(opp_id)
        template = generate_proposal_template(opp)

        self.assertIn("Hospitality Booking Trends", template)
        self.assertIn("business_analysis.xlsx", template)
        self.assertIn("cleaned_data.csv", template)
        self.assertIn("report.html", template)
        self.assertIn("6-point Quality Assurance", template)
        self.assertIn("₹7,000.00", template)

    def test_proposal_approval_gate_workflow(self):
        opp_id = add_opportunity({
            "title": "Financial Trend Synthesis",
            "source": "Direct Referral",
            "amount_inr": 8500,
            "estimated_hours": 4,
            "skill_match": 1.0,
            "payment_confidence": 1.0,
            "risk_level": 1,
            "source_permitted": True,
        }, score=1400)

        # 1. Draft generated — must start in 'draft' status
        prop_id, text, model = draft_tailored_proposal(opp_id)
        self.assertGreater(prop_id, 0)
        prop = get_proposal(opp_id)
        self.assertIsNotNone(prop)
        self.assertEqual(prop["status"], "draft")
        self.assertEqual(prop["model_used"], "deterministic_template")

        # 2. Explicit human authorization gate
        authorize_proposal(prop_id)
        updated_prop = get_proposal(opp_id)
        self.assertEqual(updated_prop["status"], "approved_by_owner")

    def test_discovery_source_database_crud(self):
        source_id = add_discovery_source(
            name="Partner Analytics RSS",
            source_type="rss",
            url_or_path="https://example.com/partner_rss.xml",
            is_permitted=True,
            is_active=True,
            poll_interval_minutes=30,
        )
        self.assertGreater(source_id, 0)

        sources = list_discovery_sources(active_only=True)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["name"], "Partner Analytics RSS")

        update_discovery_source_polled(source_id)
        src = get_discovery_source(source_id)
        self.assertIsNotNone(src["last_polled_at"])

        delete_discovery_source(source_id)
        self.assertEqual(len(list_discovery_sources()), 0)

    def test_unpermitted_source_polling_is_blocked(self):
        source_id = add_discovery_source(
            name="Unauthorized Feed",
            source_type="rss",
            url_or_path="https://example.com/unauthorized.xml",
            is_permitted=False,
            is_active=True,
        )
        src = get_discovery_source(source_id)
        res = DiscoveryEngine.poll_source(src, auto_commit=True)
        self.assertEqual(res.leads_imported, 0)
        self.assertTrue(any("not marked as permitted" in err for err in res.errors))


if __name__ == "__main__":
    unittest.main()
