"""Backend-tester för link-fetch-flöde + AI confidence-tröskel.

Täcker:
- extract_receipt_link keyword-match + fallback
- fetch_pdf_from_link SSRF-skydd (localhost)
- POST /api/messages/{id}/fetch-pdf lyckad/HTML/timeout
- Pipeline ignorerar bilagor för link-fetch-avsändare
- AI confidence under tröskel → sparas INTE

Körs med:
    python -m unittest tests.backend.test_link_flow
"""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("GMAIL_CLIENT_ID", "")
os.environ.setdefault("GMAIL_CLIENT_SECRET", "")
os.environ.setdefault("GMAIL_REFRESH_TOKEN", "")
os.environ.setdefault("DRIVE_REFRESH_TOKEN", "")
os.environ.setdefault("BEZALA_USERNAME", "")
os.environ.setdefault("BEZALA_PASSWORD", "")
os.environ.setdefault("SCAN_ENABLED", "false")
os.environ["DATABASE_URL"] = "sqlite:///:memory:"


def _configure_memory_engine():
    """SQLAlchemy memory-DB delas inte mellan connections per default.
    Vi byter engine till en StaticPool så alla sessions ser samma DB."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app import db as db_module

    db_module.engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db_module.SessionLocal = sessionmaker(
        bind=db_module.engine, autoflush=False, autocommit=False
    )
    return db_module


# ============================================================
# Rena unit-tester — ingen app/DB behövs
# ============================================================


class ExtractReceiptLinkTest(unittest.TestCase):
    def test_finds_url_with_keyword(self):
        from app.services.link_extractor import extract_receipt_link

        body_text = (
            "Tack för din resa! Hämta ditt kvitto här: "
            "https://arlandaexpress.se/receipt/abc123?token=xyz\n"
        )
        url = extract_receipt_link(body_text, "")
        self.assertIsNotNone(url)
        self.assertIn("receipt", url)
        self.assertIn("arlandaexpress.se", url)

    def test_returns_none_when_no_url(self):
        from app.services.link_extractor import extract_receipt_link

        url = extract_receipt_link("Inget här.", "<p>Inga länkar</p>")
        self.assertIsNone(url)

    def test_falls_back_to_first_long_url(self):
        """Om inget nyckelord matchar men det finns en URL som överlever
        exclude-filtret (≥40 tecken, ej tracking-pattern) → returnera den."""
        from app.services.link_extractor import extract_receipt_link

        # Lång URL utan kvitto-keyword men > 40 tecken och inte en bild
        body = "Besök https://arlandaexpress.se/journey/1234567890abcdef för detaljer."
        url = extract_receipt_link(body, "")
        self.assertEqual(url, "https://arlandaexpress.se/journey/1234567890abcdef")

    def test_excludes_short_tracking_pixel_url(self):
        """Korta URL:er (< 40 tecken) räknas som tracking och returneras inte."""
        from app.services.link_extractor import extract_receipt_link

        body = "Klicka https://t.co/abc"
        url = extract_receipt_link(body, "")
        self.assertIsNone(url)

    def test_excludes_image_urls(self):
        """PNG/JPG-URLs ska aldrig returneras (tracking-pixlar / logos)."""
        from app.services.link_extractor import extract_receipt_link

        html = (
            '<img src="https://cdn.example.com/very-long-tracking-pixel.png">'
            '<img src="https://cdn.example.com/long-logo-image.gif">'
            '<a href="https://app.example.com/receipts/abcdefghijklmnop">Kvitto</a>'
        )
        url = extract_receipt_link("", html)
        self.assertEqual(url, "https://app.example.com/receipts/abcdefghijklmnop")

    def test_excludes_dimension_url(self):
        """URLs med dimensions-mönster (33x20, 1x1) räknas som bilder."""
        from app.services.link_extractor import extract_receipt_link

        body = (
            "https://tracker.example.com/p/33x20/long-token-string-here-yes "
            "https://app.example.com/order/long-token-1234567890abcdef"
        )
        url = extract_receipt_link(body, "")
        # Bara den utan dimensioner ska returneras
        self.assertEqual(url, "https://app.example.com/order/long-token-1234567890abcdef")

    def test_anchor_text_kvitto_outranks_other_url(self):
        """Anchor-text 'Ladda ner kvitto (PDF)' rankar URL:en högst
        även om en annan URL kommer tidigare i HTML:en."""
        from app.services.link_extractor import extract_receipt_link

        html = (
            '<a href="https://app.example.com/booking/long-token-abcdef1234">Boka igen</a>'
            '<a href="https://app.example.com/r/long-token-xyz4567890123">Ladda ner kvitto (PDF)</a>'
        )
        url = extract_receipt_link("", html)
        self.assertEqual(url, "https://app.example.com/r/long-token-xyz4567890123")

    def test_excludes_tracking_tokens_in_url(self):
        from app.services.link_extractor import extract_receipt_link

        html = (
            '<a href="https://example.com/track/abcdef-very-long-token">trk</a>'
            '<a href="https://example.com/order/abcdef-very-long-token">order</a>'
        )
        url = extract_receipt_link("", html)
        self.assertEqual(url, "https://example.com/order/abcdef-very-long-token")


class FetchPdfFromLinkTest(unittest.TestCase):
    def test_rejects_localhost_ssrf(self):
        from app.services.link_fetcher import fetch_pdf_from_link, LinkFetchError

        with self.assertRaises(LinkFetchError) as ctx:
            fetch_pdf_from_link("http://127.0.0.1/evil.pdf")
        self.assertIn("blockerad", ctx.exception.message.lower())

    def test_rejects_private_ip(self):
        from app.services.link_fetcher import fetch_pdf_from_link, LinkFetchError

        with self.assertRaises(LinkFetchError):
            fetch_pdf_from_link("http://10.0.0.1/x.pdf")

    def test_rejects_non_http_scheme(self):
        from app.services.link_fetcher import fetch_pdf_from_link, LinkFetchError

        with self.assertRaises(LinkFetchError):
            fetch_pdf_from_link("file:///etc/passwd")

    def test_rejects_non_pdf_response(self):
        """Mocka httpx-klient som svarar med HTML → LinkFetchError."""
        from app.services.link_fetcher import fetch_pdf_from_link, LinkFetchError

        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html; charset=utf-8"}
        mock_resp.content = b"<html><body>Inte en PDF</body></html>"
        mock_client.get.return_value = mock_resp

        with self.assertRaises(LinkFetchError) as ctx:
            fetch_pdf_from_link(
                "https://example.com/receipt.pdf", client=mock_client
            )
        self.assertIn("pdf", ctx.exception.message.lower())

    def test_accepts_valid_pdf(self):
        from app.services.link_fetcher import fetch_pdf_from_link

        pdf_bytes = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nfake pdf content"
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/pdf"}
        mock_resp.content = pdf_bytes
        mock_client.get.return_value = mock_resp

        result = fetch_pdf_from_link(
            "https://example.com/ok.pdf", client=mock_client
        )
        self.assertEqual(result, pdf_bytes)


# ============================================================
# Integrations-tester — riktig FastAPI + SQLite
# ============================================================


class FetchPdfEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_module = _configure_memory_engine()

        from app import main as app_module
        from app.models import ProcessedMessage
        from fastapi.testclient import TestClient
        from app.db import Base
        from app import models  # noqa: F401

        Base.metadata.create_all(bind=db_module.engine)

        from app.services import (
            trash_scheduler as ts_module,
            settings_service as ss_module,
        )
        cls._patch_sessions(db_module, app_module, ts_module, ss_module)

        cls.app_module = app_module
        cls.SessionLocal = db_module.SessionLocal
        cls.ProcessedMessage = ProcessedMessage

        async def fake_require_auth():
            return None

        app_module.app.dependency_overrides[app_module.require_auth] = fake_require_auth
        cls.client = TestClient(app_module.app)

    @staticmethod
    def _patch_sessions(db_module, app_module, ts_module, ss_module):
        SessionLocal = db_module.SessionLocal
        from contextlib import contextmanager

        @contextmanager
        def session_scope():
            s = SessionLocal()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        def get_db():
            s = SessionLocal()
            try:
                yield s
            finally:
                s.close()

        db_module.session_scope = session_scope
        db_module.get_db = get_db
        app_module.get_db = get_db
        app_module.session_scope = session_scope
        ts_module.session_scope = session_scope

        try:
            from app.db import get_db as original_get_db
            app_module.app.dependency_overrides[original_get_db] = get_db
        except Exception:
            pass

    @classmethod
    def tearDownClass(cls):
        cls.app_module.app.dependency_overrides.clear()

    def setUp(self):
        with self.SessionLocal() as db:
            db.query(self.ProcessedMessage).delete()
            db.commit()

    def _seed_pending(self, link="https://example.com/ticket/xyz"):
        with self.SessionLocal() as db:
            row = self.ProcessedMessage(
                message_id="gm-pending-1",
                sender="noreply@arlandaexpress.se",
                subject="Din biljett",
                status="needs_manual_download",
                pending_link=link,
            )
            db.add(row)
            db.flush()
            mid = row.id
            db.commit()
        return mid

    def test_fetch_pdf_success_marks_row_saved(self):
        """Monkeypatchar _fetch_pdf_helper + DriveClient + GmailClient.
        Resultat: status='saved' + drive_file_id."""
        mid = self._seed_pending()
        pdf_bytes = b"%PDF-1.4\nfejk"

        fake_drive = MagicMock()
        fake_upload = MagicMock()
        fake_upload.file_id = "drive-id-123"
        fake_upload.web_view_link = "https://drive.google.com/file/d/drive-id-123"
        fake_drive.upload_pdf.return_value = fake_upload

        fake_gmail = MagicMock()

        fake_analyzer = MagicMock()
        fake_analyzer.enabled = False

        with patch.object(self.app_module, "_fetch_pdf_helper", return_value=pdf_bytes), \
             patch.object(self.app_module, "DriveClient", return_value=fake_drive), \
             patch.object(self.app_module, "GmailClient", return_value=fake_gmail), \
             patch.object(self.app_module, "ReceiptAnalyzer", return_value=fake_analyzer):
            resp = self.client.post(f"/api/messages/{mid}/fetch-pdf")

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "saved")
        self.assertEqual(body["drive_file_id"], "drive-id-123")
        self.assertIsNone(body["pending_link"])
        fake_drive.upload_pdf.assert_called_once()
        fake_gmail.mark_done.assert_called_once()

    def test_fetch_pdf_html_response_keeps_pending_link(self):
        """LinkFetchError → 502, raden behåller status + pending_link."""
        from app.services.link_fetcher import LinkFetchError

        mid = self._seed_pending()

        def raise_html(_url):
            raise LinkFetchError("Länken gav text/html istället för PDF")

        with patch.object(self.app_module, "_fetch_pdf_helper", side_effect=raise_html):
            resp = self.client.post(f"/api/messages/{mid}/fetch-pdf")

        self.assertEqual(resp.status_code, 502)
        self.assertIn("html", resp.json()["detail"].lower())

        # Raden ska vara orörd
        with self.SessionLocal() as db:
            row = db.query(self.ProcessedMessage).filter_by(id=mid).first()
            self.assertEqual(row.status, "needs_manual_download")
            self.assertEqual(row.pending_link, "https://example.com/ticket/xyz")

    def test_fetch_pdf_timeout_keeps_pending_link(self):
        from app.services.link_fetcher import LinkFetchError

        mid = self._seed_pending()

        def raise_timeout(_url):
            raise LinkFetchError("Timeout efter 15.0s")

        with patch.object(self.app_module, "_fetch_pdf_helper", side_effect=raise_timeout):
            resp = self.client.post(f"/api/messages/{mid}/fetch-pdf")

        self.assertEqual(resp.status_code, 502)
        self.assertIn("timeout", resp.json()["detail"].lower())

        with self.SessionLocal() as db:
            row = db.query(self.ProcessedMessage).filter_by(id=mid).first()
            self.assertEqual(row.status, "needs_manual_download")
            self.assertIsNotNone(row.pending_link)

    def test_fetch_pdf_rejects_row_without_pending_link(self):
        """Rad utan pending_link → 400."""
        with self.SessionLocal() as db:
            row = self.ProcessedMessage(
                message_id="gm-saved-1",
                sender="a@b.com",
                subject="Klar",
                status="saved",
            )
            db.add(row)
            db.flush()
            mid = row.id
            db.commit()

        resp = self.client.post(f"/api/messages/{mid}/fetch-pdf")
        self.assertEqual(resp.status_code, 400)


class PipelineLinkFetchTest(unittest.TestCase):
    """Verifiera att pipeline ignorerar bilagor för link-fetch-avsändare
    och sparar raden som needs_manual_download."""

    @classmethod
    def setUpClass(cls):
        db_module = _configure_memory_engine()

        from app.db import Base
        from app import models  # noqa: F401
        from app.services import pipeline as pipeline_module

        Base.metadata.create_all(bind=db_module.engine)

        from contextlib import contextmanager
        SessionLocal = db_module.SessionLocal

        @contextmanager
        def session_scope():
            s = SessionLocal()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        db_module.session_scope = session_scope
        pipeline_module.session_scope = session_scope

        cls.SessionLocal = SessionLocal
        cls.ProcessedMessage = models.ProcessedMessage
        cls.pipeline = pipeline_module

    def setUp(self):
        with self.SessionLocal() as db:
            db.query(self.ProcessedMessage).delete()
            db.commit()

    def test_arlanda_with_pdf_and_link_uses_link_pdf_not_attachment(self):
        """C37: Arlanda Express skickar BÅDE bilaga (biljett) OCH länk till
        kvittot. Tidigare beteende (PR med C36): bilagan användes — fel,
        biljetten saknar belopp/moms. Nytt beteende: vendor-handler hämtar
        kvittot via länken och skriver över bilagan."""
        from app.services.gmail_client import GmailMessage, Attachment
        from app.services.pipeline import _process_one_message, ScanResult
        from app.services.drive_client import DriveUploadResult

        ticket_bytes = b"%PDF-1.4\nfake-ticket"
        receipt_bytes = b"%PDF-1.4\nfake-receipt-with-vat"

        msg = GmailMessage(
            message_id="link-1",
            thread_id="t-1",
            sender="noreply@arlandaexpress.se",
            subject="Din resa",
            received_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
            snippet="",
            attachments=[
                Attachment(
                    filename="biljett.pdf",
                    mime_type="application/pdf",
                    data=ticket_bytes,
                )
            ],
            body_text="",
            body_html=(
                '<a href="https://arlandaexpress.se/kvitto/7410356-token-1234567890">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )

        fake_gmail = MagicMock()
        fake_gmail.fetch_message.return_value = msg
        fake_drive = MagicMock()
        fake_drive.upload_pdf.return_value = DriveUploadResult(
            file_id="drv-1", web_view_link="https://drive/drv-1", name="kvitto.pdf",
        )
        fake_drive.filename_exists.return_value = False
        fake_namer = MagicMock()
        fake_namer.name_for.return_value = "20260421 Arlanda Express kvitto.pdf"
        fake_analyzer = MagicMock()
        fake_analyzer.enabled = False

        result = ScanResult()
        with patch(
            "app.services.vendor_handlers.fetch_pdf_from_link",
            return_value=receipt_bytes,
        ):
            _process_one_message(
                "link-1",
                fake_gmail,
                fake_drive,
                fake_namer,
                fake_analyzer,
                None,
                result,
                link_fetch_senders=["noreply@arlandaexpress.se"],
            )

        # Drive SKA få länk-PDFen, inte bilagan
        fake_drive.upload_pdf.assert_called_once()
        uploaded_bytes = fake_drive.upload_pdf.call_args[0][1]
        self.assertEqual(uploaded_bytes, receipt_bytes)
        self.assertNotEqual(uploaded_bytes, ticket_bytes)

        with self.SessionLocal() as db:
            row = (
                db.query(self.ProcessedMessage)
                .filter_by(message_id="link-1")
                .first()
            )
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "saved")
        fake_gmail.mark_done.assert_called_once()

    def test_arlanda_fallback_to_attachment_if_link_fetch_fails(self):
        """C37: Om länk-hämtning failar (timeout, fel domän, content-type)
        ska pipelinen falla tillbaka på bilagan — bättre att spara biljetten
        än ingenting alls. Mikko kan reprocessa manuellt när länken funkar."""
        from app.services.gmail_client import GmailMessage, Attachment
        from app.services.pipeline import _process_one_message, ScanResult
        from app.services.drive_client import DriveUploadResult
        from app.services.link_fetcher import LinkFetchError

        ticket_bytes = b"%PDF-1.4\nfake-ticket-fallback"
        msg = GmailMessage(
            message_id="link-fb",
            thread_id="t-fb",
            sender="noreply@arlandaexpress.se",
            subject="Din resa",
            received_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
            snippet="",
            attachments=[
                Attachment(
                    filename="biljett.pdf",
                    mime_type="application/pdf",
                    data=ticket_bytes,
                )
            ],
            body_text="",
            body_html=(
                '<a href="https://arlandaexpress.se/kvitto/7410356-token-1234567890">'
                'Ladda ner kvitto (PDF)</a>'
            ),
        )

        fake_gmail = MagicMock()
        fake_gmail.fetch_message.return_value = msg
        fake_drive = MagicMock()
        fake_drive.upload_pdf.return_value = DriveUploadResult(
            file_id="drv-fb", web_view_link="https://drive/drv-fb", name="b.pdf",
        )
        fake_drive.filename_exists.return_value = False
        fake_namer = MagicMock()
        fake_namer.name_for.return_value = "20260421 Arlanda Express.pdf"
        fake_analyzer = MagicMock()
        fake_analyzer.enabled = False

        def raise_timeout(_url):
            raise LinkFetchError("Timeout efter 15.0s")

        result = ScanResult()
        with patch(
            "app.services.vendor_handlers.fetch_pdf_from_link",
            side_effect=raise_timeout,
        ):
            _process_one_message(
                "link-fb",
                fake_gmail,
                fake_drive,
                fake_namer,
                fake_analyzer,
                None,
                result,
                link_fetch_senders=["noreply@arlandaexpress.se"],
            )

        # Bilagan (biljetten) används som fallback
        fake_drive.upload_pdf.assert_called_once()
        uploaded_bytes = fake_drive.upload_pdf.call_args[0][1]
        self.assertEqual(uploaded_bytes, ticket_bytes)

        with self.SessionLocal() as db:
            row = (
                db.query(self.ProcessedMessage)
                .filter_by(message_id="link-fb")
                .first()
            )
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "saved")

    def test_link_fetch_sender_without_pdf_uses_link(self):
        """Mail från link_fetch_senders UTAN PDF-bilaga (Mail B):
        bilagan ignoreras (det finns ingen), raden sparas som
        needs_manual_download."""
        from app.services.gmail_client import GmailMessage
        from app.services.pipeline import _process_one_message, ScanResult

        msg = GmailMessage(
            message_id="link-1",
            thread_id="t-1",
            sender="noreply@arlandaexpress.se",
            subject="Kvitto för ditt köp",
            received_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
            snippet="",
            attachments=[],
            body_text="Hämta kvitto: https://arlandaexpress.se/receipt/abcdefg-token-1234567",
            body_html="",
        )

        fake_gmail = MagicMock()
        fake_gmail.fetch_message.return_value = msg
        fake_drive = MagicMock()
        fake_namer = MagicMock()
        fake_analyzer = MagicMock()
        fake_analyzer.enabled = False

        result = ScanResult()
        _process_one_message(
            "link-1",
            fake_gmail,
            fake_drive,
            fake_namer,
            fake_analyzer,
            None,
            result,
            link_fetch_senders=["noreply@arlandaexpress.se"],
        )

        # Drive SKA INTE röras — ingen PDF
        fake_drive.upload_pdf.assert_not_called()
        # Gmail SKA INTE markeras klar — användaren behöver trigga fetch-pdf
        fake_gmail.mark_done.assert_not_called()
        # Raden ska finnas i DB som needs_manual_download
        with self.SessionLocal() as db:
            row = (
                db.query(self.ProcessedMessage)
                .filter_by(message_id="link-1")
                .first()
            )
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "needs_manual_download")
            self.assertIn("receipt/abc", row.pending_link)
        self.assertEqual(result.processed, 1)

    def test_link_fetch_sender_without_link_marks_done(self):
        """Mail från link_fetch_senders men ingen URL → mark_done + skipped."""
        from app.services.gmail_client import GmailMessage
        from app.services.pipeline import _process_one_message, ScanResult

        msg = GmailMessage(
            message_id="link-2",
            thread_id="t-2",
            sender="noreply@arlandaexpress.se",
            subject="Tack",
            received_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
            snippet="",
            attachments=[],
            body_text="Tack för att du reste med oss.",
            body_html="",
        )

        fake_gmail = MagicMock()
        fake_gmail.fetch_message.return_value = msg
        fake_drive = MagicMock()
        fake_namer = MagicMock()
        fake_analyzer = MagicMock()
        fake_analyzer.enabled = False

        result = ScanResult()
        _process_one_message(
            "link-2",
            fake_gmail,
            fake_drive,
            fake_namer,
            fake_analyzer,
            None,
            result,
            link_fetch_senders=["noreply@arlandaexpress.se"],
        )

        fake_drive.upload_pdf.assert_not_called()
        fake_gmail.mark_done.assert_called_once_with("link-2")
        self.assertEqual(result.skipped, 1)


class AiConfidenceThresholdTest(unittest.TestCase):
    """Verifiera att AI confidence < tröskel INTE sparar till DB."""

    @classmethod
    def setUpClass(cls):
        db_module = _configure_memory_engine()

        from app.db import Base
        from app import models  # noqa: F401
        from app.services import pipeline as pipeline_module

        Base.metadata.create_all(bind=db_module.engine)

        from contextlib import contextmanager
        SessionLocal = db_module.SessionLocal

        @contextmanager
        def session_scope():
            s = SessionLocal()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        db_module.session_scope = session_scope
        pipeline_module.session_scope = session_scope

        cls.SessionLocal = SessionLocal
        cls.ProcessedMessage = models.ProcessedMessage

    def setUp(self):
        with self.SessionLocal() as db:
            db.query(self.ProcessedMessage).delete()
            db.commit()

    def _build_msg(self, mid="low-1"):
        from app.services.gmail_client import GmailMessage, Attachment

        return GmailMessage(
            message_id=mid,
            thread_id="t",
            sender="sender@example.com",
            subject="Kvitto",
            received_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
            snippet="",
            attachments=[
                Attachment(
                    filename="r.pdf",
                    mime_type="application/pdf",
                    data=b"%PDF-1.4\nfake",
                )
            ],
        )

    def test_confidence_below_threshold_not_saved(self):
        from app.services.pipeline import _process_one_message, ScanResult
        from app.services.receipt_analyzer import ReceiptAnalysis

        msg = self._build_msg("low-1")
        analysis = ReceiptAnalysis(
            is_receipt=True,
            confidence=35,
            filename="Kvitto.pdf",
            vendor="X",
            amount=10.0,
            currency="EUR",
            date="2026-04-21",
            category="Annat",
            summary="test",
        )

        fake_gmail = MagicMock()
        fake_gmail.fetch_message.return_value = msg
        fake_drive = MagicMock()
        fake_namer = MagicMock()
        fake_analyzer = MagicMock()
        fake_analyzer.enabled = True
        fake_analyzer.analyze.return_value = analysis

        result = ScanResult()
        _process_one_message(
            "low-1",
            fake_gmail,
            fake_drive,
            fake_namer,
            fake_analyzer,
            None,
            result,
            use_ai=True,
            ai_min_confidence=40,
        )

        # Ingen Drive-upload (sparas INTE)
        fake_drive.upload_pdf.assert_not_called()
        # Gmail ska markeras klar (så mailet inte scannas igen)
        fake_gmail.mark_done.assert_called_once_with("low-1")
        # Inget rad i DB
        with self.SessionLocal() as db:
            row = (
                db.query(self.ProcessedMessage)
                .filter_by(message_id="low-1")
                .first()
            )
            self.assertIsNone(row)
        # Men loggad i result.notes
        self.assertTrue(
            any("låg confidence" in n for n in result.notes),
            f"Förväntade not-log, fick: {result.notes}",
        )

    def test_confidence_at_threshold_is_saved(self):
        """Confidence = tröskel → sparas (strikt <)."""
        from app.services.pipeline import _process_one_message, ScanResult
        from app.services.receipt_analyzer import ReceiptAnalysis

        msg = self._build_msg("ok-1")
        analysis = ReceiptAnalysis(
            is_receipt=True,
            confidence=45,
            filename="OK Kvitto.pdf",
            vendor="Y",
            amount=50.0,
            currency="EUR",
            date="2026-04-21",
            category="Annat",
            summary="test",
        )

        fake_gmail = MagicMock()
        fake_gmail.fetch_message.return_value = msg
        fake_drive = MagicMock()
        fake_upload = MagicMock()
        fake_upload.file_id = "drv-1"
        fake_upload.web_view_link = "https://drive/drv-1"
        fake_drive.upload_pdf.return_value = fake_upload
        fake_drive.filename_exists.return_value = False
        fake_namer = MagicMock()
        fake_analyzer = MagicMock()
        fake_analyzer.enabled = True
        fake_analyzer.analyze.return_value = analysis

        result = ScanResult()
        _process_one_message(
            "ok-1",
            fake_gmail,
            fake_drive,
            fake_namer,
            fake_analyzer,
            None,
            result,
            use_ai=True,
            ai_min_confidence=40,
        )

        fake_drive.upload_pdf.assert_called_once()
        with self.SessionLocal() as db:
            row = (
                db.query(self.ProcessedMessage)
                .filter_by(message_id="ok-1")
                .first()
            )
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "saved")
            self.assertEqual(row.ai_confidence, 45)


class PreliminaryFieldsExtractionTest(unittest.TestCase):
    """_extract_preliminary_fields kör HTML→PDF + Claude på mail-bodyn
    för link_fetch-rader så Dashboard visar vendor/date/amount innan
    användaren hämtar själva PDFen."""

    def _build_msg(self, html="<p>Kvitto</p>", text="Kvitto"):
        from datetime import datetime, timezone
        from app.services.gmail_client import GmailMessage
        return GmailMessage(
            message_id="lf-1",
            thread_id="t",
            sender="noreply@arlandaexpress.se",
            subject="Kvitto för ditt köp",
            received_at=datetime(2026, 4, 24, tzinfo=timezone.utc),
            snippet="Arlanda Express",
            attachments=[],
            body_html=html,
            body_text=text,
        )

    def test_returns_empty_when_ai_disabled(self):
        from app.services.pipeline import _extract_preliminary_fields
        analyzer = MagicMock()
        analyzer.enabled = False
        out = _extract_preliminary_fields(
            self._build_msg(), analyzer=analyzer,
            use_ai=True, html_to_pdf_enabled=True,
        )
        self.assertEqual(out, {
            "vendor": None, "amount": None, "currency": None,
            "receipt_date": None, "category": None,
        })
        analyzer.analyze.assert_not_called()

    def test_returns_empty_when_html_to_pdf_disabled(self):
        from app.services.pipeline import _extract_preliminary_fields
        analyzer = MagicMock()
        analyzer.enabled = True
        out = _extract_preliminary_fields(
            self._build_msg(), analyzer=analyzer,
            use_ai=True, html_to_pdf_enabled=False,
        )
        self.assertEqual(out["vendor"], None)
        analyzer.analyze.assert_not_called()

    def test_returns_analysis_fields_on_success(self):
        from unittest.mock import patch as _patch
        from app.services.pipeline import _extract_preliminary_fields
        from app.services.receipt_analyzer import ReceiptAnalysis

        analyzer = MagicMock()
        analyzer.enabled = True
        analyzer.analyze.return_value = ReceiptAnalysis(
            is_receipt=True, confidence=70, filename="x.pdf",
            vendor="Arlanda Express", amount=320.0, currency="SEK",
            date="2026-04-24", category="Taxi", summary=None,
        )
        with _patch(
            "app.services.pipeline.html_to_pdf",
            return_value=b"%PDF-1.4\nstub",
        ):
            out = _extract_preliminary_fields(
                self._build_msg(), analyzer=analyzer,
                use_ai=True, html_to_pdf_enabled=True,
            )
        self.assertEqual(out, {
            "vendor": "Arlanda Express", "amount": 320.0, "currency": "SEK",
            "receipt_date": "2026-04-24", "category": "Taxi",
        })
        analyzer.analyze.assert_called_once()

    def test_returns_empty_on_html_to_pdf_failure(self):
        from unittest.mock import patch as _patch
        from app.services.pipeline import _extract_preliminary_fields
        from app.services.html_pdf_converter import HtmlToPdfError

        analyzer = MagicMock()
        analyzer.enabled = True
        with _patch(
            "app.services.pipeline.html_to_pdf",
            side_effect=HtmlToPdfError("simulated"),
        ):
            out = _extract_preliminary_fields(
                self._build_msg(), analyzer=analyzer,
                use_ai=True, html_to_pdf_enabled=True,
            )
        self.assertEqual(out["vendor"], None)
        analyzer.analyze.assert_not_called()

    def test_returns_empty_on_analyzer_failure(self):
        from unittest.mock import patch as _patch
        from app.services.pipeline import _extract_preliminary_fields
        from app.services.receipt_analyzer import AnalyzerError

        analyzer = MagicMock()
        analyzer.enabled = True
        analyzer.analyze.side_effect = AnalyzerError("Claude API-fel")
        with _patch(
            "app.services.pipeline.html_to_pdf",
            return_value=b"%PDF-1.4\nstub",
        ):
            out = _extract_preliminary_fields(
                self._build_msg(), analyzer=analyzer,
                use_ai=True, html_to_pdf_enabled=True,
            )
        self.assertEqual(out["vendor"], None)


if __name__ == "__main__":
    unittest.main()
