import os
import unittest
from core.outreach_dispatcher import (
    SMTPOutreachProvider,
    WebhookOutreachProvider,
    dispatch_client_outreach,
    format_compliant_email_body,
    get_outreach_rate_limit_status,
)


class OutreachDispatcherTests(unittest.TestCase):
    def test_compliance_footer_appended(self):
        body = "Hello founder, we can help with your reconciliation."
        formatted = format_compliant_email_body(body, sender_name="Bhavesh", business_name="SOLOEARN")
        self.assertIn("UNSUBSCRIBE", formatted)
        self.assertIn("Bhavesh", formatted)
        self.assertIn("SOLOEARN", formatted)

        # Idempotent if already present
        double_formatted = format_compliant_email_body(formatted)
        self.assertEqual(double_formatted.count("UNSUBSCRIBE"), 1)

    def test_rate_limit_status(self):
        can_send, count, remaining = get_outreach_rate_limit_status(daily_limit=10, is_sandbox=True)
        self.assertIsInstance(can_send, bool)
        self.assertGreaterEqual(count, 0)
        self.assertGreaterEqual(remaining, 0)

    def test_sandbox_dispatch_simulation(self):
        res = dispatch_client_outreach(
            client_name="Test D2C Brand",
            channel="email",
            pitch_text="Sample value pitch text for reconciliation.",
            recipient="founder@testbrand.com",
            is_sandbox=True,
        )
        self.assertEqual(res.status, "simulated")
        self.assertEqual(res.channel, "email")
        self.assertTrue(res.message_id.startswith("SIM-"))
        self.assertTrue(len(res.verification_hash) > 0)

    def test_unconfigured_smtp_handled_gracefully(self):
        # Empty provider
        provider = SMTPOutreachProvider(smtp_user="", smtp_pass="")
        self.assertFalse(provider.is_configured())
        res = provider.send_pitch("target@example.com", "Subject", "Body")
        self.assertEqual(res.status, "failed")
        self.assertIn("not configured", res.error_message)

    def test_unconfigured_webhook_handled_gracefully(self):
        provider = WebhookOutreachProvider(webhook_url="")
        self.assertFalse(provider.is_configured())
        res = provider.send_pitch("target@example.com", "Subject", "Body")
        self.assertEqual(res.status, "failed")
        self.assertIn("not configured", res.error_message)


if __name__ == "__main__":
    unittest.main()
