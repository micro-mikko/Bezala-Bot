"""Tester för vendor-specifika kvitto-hämtare (C37 + C37b).

Täcker:
- Arlanda Express: hämtar kvitto från länk i mail-bodyn
- SendGrid-wrappade entry-URLer accepteras; final URL valideras post-fetch
- Fallback: returnerar None om hämtning failar (pipelinen faller på bilaga)
- Säkerhet: HTTPS på entry OCH final URL; final URL måste matcha vendor-domän
- Avsändare utan handler returnerar None
"""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("SESSION_SECRET", "test-secret")


def _make_msg(
    *,
    sender: str = "noreply@arlandaexpress.se",
    body_html: str = "",
    body_text: str = "",
    message_id: str = "vh-1",
):
    from app.services.gmail_client import GmailMessage

    return GmailMessage(
        message_id=message_id,
        thread_id="t",
        sender=sender,
        subject="Din resa",
        received_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        snippet="",
        attachments=[],
        body_text=body_text,
        body_html=body_html,
    )


def _make_fetcher(*, pdf: bytes = b"%PDF-1.4\nok", final_url: str):
    """Fake-fetcher som returnerar (pdf_bytes, final_url) — matchar nya
    signaturen för fetch_pdf_from_link efter C37b."""
    def _f(_url):
        return pdf, final_url
    return _f


class ArlandaExpressLinkReceiptTest(unittest.TestCase):
    def test_fetches_pdf_when_sender_matches_and_final_url_on_vendor_domain(self):
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(
            body_html=(
                '<a href="https://arlandaexpress.se/kvitto/7410356-token-1234567890">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )
        fake_pdf = b"%PDF-1.4\nfake-receipt"
        called_with = {}

        def fake_fetcher(url):
            called_with["url"] = url
            return fake_pdf, "https://www.arlandaexpress.se/kvitto/7410356.pdf"

        result = fetch_link_receipt_for_message(msg, _fetcher=fake_fetcher)
        self.assertEqual(result, fake_pdf)
        self.assertIn("arlandaexpress.se", called_with["url"])

    def test_sendgrid_wrapped_entry_url_accepted_when_final_url_matches_vendor(self):
        """C37b: Arlandas mail går genom SendGrid click-tracker. Entry-URLen
        är u10665393.ct.sendgrid.net/... — får INTE rejectas. Det är FINAL
        URL efter följda redirects som måste matcha arlandaexpress.se."""
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(
            body_html=(
                '<a href="https://u10665393.ct.sendgrid.net/ls/click?abc-very-long-token-xyz">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )
        fake_pdf = b"%PDF-1.4\nreal-receipt"
        fetcher = _make_fetcher(
            pdf=fake_pdf,
            final_url="https://www.arlandaexpress.se/kvitto/7410356.pdf",
        )
        result = fetch_link_receipt_for_message(msg, _fetcher=fetcher)
        self.assertEqual(result, fake_pdf)

    def test_sendgrid_wrapped_entry_url_rejected_when_final_url_is_other_domain(self):
        """C37b säkerhet: om någon hijackar SendGrid-kontot och pekar
        redirect mot evil.com → final URL ≠ arlandaexpress.se → rejecta."""
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(
            body_html=(
                '<a href="https://u10665393.ct.sendgrid.net/ls/click?abc-very-long-token-xyz">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )
        fetcher = _make_fetcher(final_url="https://evil.com/fake-receipt.pdf")
        result = fetch_link_receipt_for_message(msg, _fetcher=fetcher)
        self.assertIsNone(result)

    def test_rejects_when_final_url_downgrades_to_http(self):
        """C37b säkerhet: om redirect-kedjan landar på http:// → rejecta
        (downgrade-attack-skydd)."""
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(
            body_html=(
                '<a href="https://arlandaexpress.se/kvitto/7410356-token-1234567890">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )
        fetcher = _make_fetcher(
            final_url="http://arlandaexpress.se/kvitto/7410356.pdf",
        )
        result = fetch_link_receipt_for_message(msg, _fetcher=fetcher)
        self.assertIsNone(result)

    def test_direct_vendor_url_still_works_without_redirect(self):
        """Bakåt-kompat: direkt arlandaexpress.se URL utan redirect (final
        URL == entry URL) ska fortfarande accepteras."""
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(
            body_html=(
                '<a href="https://arlandaexpress.se/kvitto/7410356-token-1234567890">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )
        fake_pdf = b"%PDF-1.4\nok"
        fetcher = _make_fetcher(
            pdf=fake_pdf,
            final_url="https://arlandaexpress.se/kvitto/7410356-token-1234567890",
        )
        result = fetch_link_receipt_for_message(msg, _fetcher=fetcher)
        self.assertEqual(result, fake_pdf)

    def test_returns_none_when_sender_does_not_match(self):
        """Avsändare som inte finns i handler-registry → None."""
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(
            sender="info@some-other-vendor.com",
            body_html='<a href="https://arlandaexpress.se/kvitto/abc-long-token">Kvitto</a>',
        )
        fetcher = MagicMock()
        result = fetch_link_receipt_for_message(msg, _fetcher=fetcher)
        self.assertIsNone(result)
        fetcher.assert_not_called()

    def test_returns_none_when_no_link_in_body(self):
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(body_text="Tack för att du reste med oss.")
        fetcher = MagicMock()
        result = fetch_link_receipt_for_message(msg, _fetcher=fetcher)
        self.assertIsNone(result)
        fetcher.assert_not_called()

    def test_rejects_http_entry_url(self):
        """Entry måste vara HTTPS — http:// ignoreras innan fetch."""
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(
            body_html=(
                '<a href="http://arlandaexpress.se/kvitto/long-token-abcdef1234567890">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )
        fetcher = MagicMock()
        result = fetch_link_receipt_for_message(msg, _fetcher=fetcher)
        self.assertIsNone(result)
        fetcher.assert_not_called()

    def test_accepts_subdomain_of_allowed_domain(self):
        """Subdomäner av arlandaexpress.se accepteras som final URL."""
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(
            body_html=(
                '<a href="https://receipts.arlandaexpress.se/r/long-token-abcdef1234567890">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )
        fake_pdf = b"%PDF-1.4\nok"
        fetcher = _make_fetcher(
            pdf=fake_pdf,
            final_url="https://receipts.arlandaexpress.se/r/x.pdf",
        )
        result = fetch_link_receipt_for_message(msg, _fetcher=fetcher)
        self.assertEqual(result, fake_pdf)

    def test_returns_none_when_fetcher_raises_link_fetch_error(self):
        """Timeout/HTTP-fel/PDF-magic-fail från fetch_pdf_from_link → None."""
        from app.services.vendor_handlers import fetch_link_receipt_for_message
        from app.services.link_fetcher import LinkFetchError

        msg = _make_msg(
            body_html=(
                '<a href="https://arlandaexpress.se/kvitto/7410356-token-1234567890">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )

        def raise_timeout(_url):
            raise LinkFetchError("Timeout efter 15.0s")

        result = fetch_link_receipt_for_message(msg, _fetcher=raise_timeout)
        self.assertIsNone(result)

    def test_returns_none_when_fetcher_raises_unexpected_exception(self):
        """Oväntade fel ska sväljas — pipelinen får inte krascha."""
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(
            body_html=(
                '<a href="https://arlandaexpress.se/kvitto/7410356-token-1234567890">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )

        def boom(_url):
            raise RuntimeError("nät dog plötsligt")

        result = fetch_link_receipt_for_message(msg, _fetcher=boom)
        self.assertIsNone(result)

    def test_empty_sender_returns_none(self):
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(sender="", body_html='<a href="https://arlandaexpress.se/kvitto/x-long-token-abcdef">Kvitto</a>')
        result = fetch_link_receipt_for_message(msg, _fetcher=MagicMock())
        self.assertIsNone(result)

    def test_sender_match_is_case_insensitive(self):
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(
            sender="NoReply@ArlandaExpress.SE",
            body_html=(
                '<a href="https://arlandaexpress.se/kvitto/7410356-token-1234567890">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )
        fake_pdf = b"%PDF-1.4\nok"
        fetcher = _make_fetcher(
            pdf=fake_pdf,
            final_url="https://arlandaexpress.se/kvitto/7410356.pdf",
        )
        result = fetch_link_receipt_for_message(msg, _fetcher=fetcher)
        self.assertEqual(result, fake_pdf)


if __name__ == "__main__":
    unittest.main()
