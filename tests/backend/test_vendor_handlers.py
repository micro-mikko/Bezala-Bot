"""Tester för vendor-specifika kvitto-hämtare (C37).

Täcker:
- Arlanda Express: hämtar kvitto från länk i mail-bodyn
- Fallback: returnerar None om hämtning failar (pipelinen faller på bilaga)
- Säkerhet: länk måste vara HTTPS + matcha tillåten domän
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


class ArlandaExpressLinkReceiptTest(unittest.TestCase):
    def test_fetches_pdf_when_sender_matches_and_link_present(self):
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
            return fake_pdf

        result = fetch_link_receipt_for_message(msg, _fetcher=fake_fetcher)
        self.assertEqual(result, fake_pdf)
        self.assertIn("arlandaexpress.se", called_with["url"])
        self.assertIn("kvitto", called_with["url"])

    def test_returns_none_when_sender_does_not_match(self):
        """Avsändare som inte finns i handler-registry → None.
        Pipelinen ska då använda bilagan (default-vägen)."""
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(
            sender="info@some-other-vendor.com",
            body_html='<a href="https://arlandaexpress.se/kvitto/abc">Kvitto</a>',
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

    def test_rejects_link_to_other_domain(self):
        """Säkerhet: kvitto-länk måste peka mot arlandaexpress.se.
        En manipulerad länk till t.ex. evil.com ska ignoreras."""
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(
            body_html=(
                '<a href="https://evil.com/kvitto/long-token-abcdef1234567890">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )
        fetcher = MagicMock()
        result = fetch_link_receipt_for_message(msg, _fetcher=fetcher)
        self.assertIsNone(result)
        fetcher.assert_not_called()

    def test_rejects_http_link_only_https_allowed(self):
        """Säkerhet: kvitton går alltid över TLS. http:// → ignoreras."""
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
        """Subdomäner av arlandaexpress.se (t.ex. tickets.arlandaexpress.se,
        receipts.arlandaexpress.se) ska accepteras — A-Train AB kan rotera
        sub-tjänster utan att vi behöver patcha."""
        from app.services.vendor_handlers import fetch_link_receipt_for_message

        msg = _make_msg(
            body_html=(
                '<a href="https://receipts.arlandaexpress.se/r/long-token-abcdef1234567890">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )
        fake_pdf = b"%PDF-1.4\nok"
        result = fetch_link_receipt_for_message(
            msg, _fetcher=lambda _url: fake_pdf,
        )
        self.assertEqual(result, fake_pdf)

    def test_returns_none_when_fetcher_raises_link_fetch_error(self):
        """Timeout/HTTP-fel från fetch_pdf_from_link → None (fallback)."""
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
        result = fetch_link_receipt_for_message(
            msg, _fetcher=lambda _url: fake_pdf,
        )
        self.assertEqual(result, fake_pdf)


if __name__ == "__main__":
    unittest.main()
