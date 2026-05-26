"""C33 / FAS 7a — manuell kvittouppladdning för företagskort.

Tester för:
- POST /api/messages/upload skapar ProcessedMessage med upload_source='manual_upload'
- Company-card-vägen kopplar direkt till bill_line när bill_line_id ges
- PDF-magic-byte-validering avvisar trasiga PDFer
- Storlek över 50 MB avvisas
- Privat kort (FAS 7b) returnerar 501 utan att försöka spara
"""

from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("GMAIL_CLIENT_ID", "")
os.environ.setdefault("GMAIL_CLIENT_SECRET", "")
os.environ.setdefault("GMAIL_REFRESH_TOKEN", "")
os.environ.setdefault("DRIVE_REFRESH_TOKEN", "")
os.environ.setdefault("BEZALA_USERNAME", "test@example.com")
os.environ.setdefault("BEZALA_PASSWORD", "secret")
os.environ.setdefault("SCAN_ENABLED", "false")
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nfake pdf body for test"
NOT_PDF_BYTES = b"<html><body>This isn't a PDF</body></html>"
# Magic bytes som lurar _ensure_pdf_for_bezala att tro att Drive-filen är
# JPG/PNG — i C35-testerna mockar vi image_to_pdf separat så det räcker
# med rätt prefix här (vi kör inte weasyprint i unit-testerna).
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"fake jpeg body"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16 + b"fake png body"


def _configure_memory_engine():
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


class ManualUploadEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_module = _configure_memory_engine()
        from app.db import Base
        from app import models  # noqa: F401
        from app import main as app_module
        from app.models import ProcessedMessage
        from fastapi.testclient import TestClient

        Base.metadata.create_all(bind=db_module.engine)

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
        try:
            from app.db import get_db as original_get_db
            app_module.app.dependency_overrides[original_get_db] = get_db
        except Exception:
            pass

        async def fake_require_auth():
            return None

        app_module.app.dependency_overrides[app_module.require_auth] = (
            fake_require_auth
        )
        cls.client = TestClient(app_module.app)
        cls.app_module = app_module
        cls.SessionLocal = SessionLocal
        cls.ProcessedMessage = ProcessedMessage

    @classmethod
    def tearDownClass(cls):
        cls.app_module.app.dependency_overrides.clear()

    def setUp(self):
        with self.SessionLocal() as db:
            db.query(self.ProcessedMessage).delete()
            db.commit()

    # ---------- Helpers ----------

    def _fake_drive(self):
        drive = MagicMock()
        upload = MagicMock()
        upload.file_id = "drv-manual-1"
        upload.web_view_link = "https://drive.google.com/file/d/drv-manual-1/view"
        upload.name = "manual.pdf"
        drive.upload_attachment.return_value = upload
        drive.upload_pdf.return_value = upload
        drive.download_pdf.return_value = PDF_BYTES
        return drive

    def _fake_bezala(self):
        bezala = MagicMock()
        attachment = MagicMock()
        attachment.attachment_id = "tx-501"
        bezala.attach_file.return_value = attachment
        bezala.list_missing_receipts.return_value = [
            {
                "id": 2200001,
                "description": "PORVOON AUTOPESU, PORVOO, FI 83.11 EUR",
                "amount": 83.11,
                "currency": "EUR",
                "date": "2026-05-12",
            },
        ]
        bezala.list_accounts.return_value = []
        bezala.list_cost_centers.return_value = []
        bezala.list_vat_rates.return_value = []
        return bezala

    def _post_upload(
        self,
        *,
        data: bytes = PDF_BYTES,
        filename: str = "biltvatt.pdf",
        content_type: str = "application/pdf",
        payment_method: str = "company_card",
        comment: str | None = None,
        bill_line_id: str | None = None,
        fake_drive=None,
        fake_bezala=None,
    ):
        files = {"file": (filename, data, content_type)}
        form = {"payment_method": payment_method}
        if comment is not None:
            form["comment"] = comment
        if bill_line_id is not None:
            form["bill_line_id"] = bill_line_id

        drive = fake_drive if fake_drive is not None else self._fake_drive()
        bezala = fake_bezala if fake_bezala is not None else self._fake_bezala()

        with patch.object(
            self.app_module, "DriveClient", return_value=drive
        ), patch.object(
            self.app_module, "BezalaClient", return_value=bezala
        ):
            resp = self.client.post(
                "/api/messages/upload", files=files, data=form,
            )
        return resp, drive, bezala

    # ---------- Tester ----------

    def test_upload_creates_message_with_manual_source(self):
        """Lyckad upload → ProcessedMessage med upload_source='manual_upload',
        payment_method='company_card' och manual_uploaded_at satt."""
        resp, drive, _ = self._post_upload(
            filename="autopesu.pdf", comment="Biltvätt 12 maj",
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIn("message", body)
        msg = body["message"]
        self.assertEqual(msg["upload_source"], "manual_upload")
        self.assertEqual(msg["payment_method"], "company_card")
        self.assertEqual(msg["manual_comment"], "Biltvätt 12 maj")
        self.assertIsNotNone(msg["manual_uploaded_at"])
        self.assertEqual(msg["status"], "saved")
        self.assertEqual(msg["drive_file_id"], "drv-manual-1")

        # Drive-upload kallades med korrekt mimetype.
        drive.upload_attachment.assert_called_once()
        args, _kwargs = drive.upload_attachment.call_args
        self.assertEqual(args[2], "application/pdf")

        # En rad finns i DB.
        with self.SessionLocal() as db:
            rows = db.query(self.ProcessedMessage).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].upload_source, "manual_upload")
            self.assertTrue(
                rows[0].message_id.startswith("manual:"),
                f"message_id should be synthesized, got {rows[0].message_id!r}",
            )

    def test_upload_company_card_couples_to_bill_line(self):
        """När bill_line_id ges kopplas raden direkt till Bezala via samma
        flöde som /match-to-bezala — coupling.ok=True och attach_file kallad."""
        resp, _drive, bezala = self._post_upload(
            filename="autopesu.pdf",
            bill_line_id="2200001",
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIsNotNone(body.get("coupling"))
        coupling = body["coupling"]
        self.assertTrue(coupling.get("ok"), f"coupling failed: {coupling}")
        self.assertEqual(coupling["bill_line_id"], "2200001")
        self.assertEqual(coupling["transaction_id"], "tx-501")

        # Bezala-anropet skedde.
        bezala.attach_file.assert_called_once()

        # Raden i DB har bezala_transaction_id satt.
        msg = body["message"]
        self.assertEqual(msg["bezala_transaction_id"], "tx-501")
        self.assertEqual(msg["bezala_upload_status"], "success")

    def test_upload_validates_pdf_content(self):
        """Filer märkta som PDF (filändelse + content-type) som saknar
        %PDF-magic-bytes ska avvisas med 422 — viktigt eftersom Drive ibland
        returnerar PNG-thumbnails trots application/pdf-header."""
        resp, _drive, _bezala = self._post_upload(
            data=NOT_PDF_BYTES,
            filename="trasig.pdf",
            content_type="application/pdf",
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertIn("%PDF", resp.json()["detail"])

        # Ingen ProcessedMessage-rad skapad.
        with self.SessionLocal() as db:
            self.assertEqual(db.query(self.ProcessedMessage).count(), 0)

    def test_upload_rejects_oversized_file(self):
        """Filer > 50 MB avvisas med 413 utan att försöka spara."""
        big = b"%PDF" + b"A" * (51 * 1024 * 1024)  # ~51 MB
        resp, _drive, _bezala = self._post_upload(
            data=big, filename="stor.pdf",
        )
        self.assertEqual(resp.status_code, 413, resp.text)
        self.assertIn("50", resp.json()["detail"])

        with self.SessionLocal() as db:
            self.assertEqual(db.query(self.ProcessedMessage).count(), 0)

    def test_upload_private_card_not_yet_routed(self):
        """FAS 7b ej byggd — private_card ska returnera 501 utan att krascha
        och utan att skapa en ProcessedMessage-rad."""
        resp, _drive, _bezala = self._post_upload(
            payment_method="private_card",
        )
        self.assertEqual(resp.status_code, 501, resp.text)
        self.assertIn("FAS 7b", resp.json()["detail"])

        with self.SessionLocal() as db:
            self.assertEqual(db.query(self.ProcessedMessage).count(), 0)

    def test_upload_rejects_unsupported_mime(self):
        """Endast PDF/JPG/PNG accepteras — t.ex. .docx ska avvisas 415."""
        resp, _drive, _bezala = self._post_upload(
            data=b"PK\x03\x04 fake docx",
            filename="kvitto.docx",
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )
        self.assertEqual(resp.status_code, 415, resp.text)

    def test_upload_unknown_payment_method_rejected(self):
        resp, _drive, _bezala = self._post_upload(payment_method="cash")
        self.assertEqual(resp.status_code, 400, resp.text)


class ManualUploadCouplingTest(unittest.TestCase):
    """C35 — match-to-bezala på en redan sparad manuell-upload-rad
    (FAS 7a-flödet där användaren först laddar upp och sedan klickar
    Couple på en bill_line). Detta är vägen som 502:ade i prod
    2026-05-26 (msg_id=642 Autopesu)."""

    @classmethod
    def setUpClass(cls):
        db_module = _configure_memory_engine()
        from app.db import Base
        from app import models  # noqa: F401
        from app import main as app_module
        from app.models import ProcessedMessage
        from fastapi.testclient import TestClient

        Base.metadata.create_all(bind=db_module.engine)

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
        try:
            from app.db import get_db as original_get_db
            app_module.app.dependency_overrides[original_get_db] = get_db
        except Exception:
            pass

        async def fake_require_auth():
            return None

        app_module.app.dependency_overrides[app_module.require_auth] = (
            fake_require_auth
        )
        cls.client = TestClient(app_module.app)
        cls.app_module = app_module
        cls.SessionLocal = SessionLocal
        cls.ProcessedMessage = ProcessedMessage

    @classmethod
    def tearDownClass(cls):
        cls.app_module.app.dependency_overrides.clear()

    def setUp(self):
        with self.SessionLocal() as db:
            db.query(self.ProcessedMessage).delete()
            db.commit()

    def _seed_manual_upload(self, **over):
        """Skapa ett ProcessedMessage som motsvarar en redan-uppladdad
        manuell rad (FAS 7a). Bara fält som upload-vägen sätter — Gmail-
        specifika fält (thread_id) är None som default."""
        from datetime import datetime
        defaults = dict(
            message_id="manual:abc123def456",
            thread_id=None,  # Gmail-specifik, None för manual_upload
            sender="manual-upload@bezala-bot",
            subject="autopesu.pdf",
            file_name="autopesu.pdf",
            drive_file_id="drv-manual-642",
            drive_link="https://drive/drv-manual-642",
            status="saved",
            vendor="Autopesu",
            amount=83.11,
            currency="EUR",
            receipt_date="2026-05-12",
            category=None,
            summary=None,
            ai_description_en="Car wash, Porvoo, 12 May 2026",
            ai_confidence=88,
            bezala_upload_status="pending",
            payment_method="company_card",
            upload_source="manual_upload",
            manual_uploaded_at=datetime.utcnow(),
            manual_comment="Biltvätt",
        )
        defaults.update(over)
        with self.SessionLocal() as db:
            row = self.ProcessedMessage(**defaults)
            db.add(row)
            db.flush()
            mid = row.id
            db.commit()
        return mid

    def _fake_bezala(self, bill_line_id: int = 2200001):
        bezala = MagicMock()
        attachment = MagicMock()
        attachment.attachment_id = "tx-501"
        bezala.attach_file.return_value = attachment
        bezala.list_missing_receipts.return_value = [
            {
                "id": bill_line_id,
                "description": (
                    "PORVOON AUTOPESU, PORVOO, FI 83.11 EUR"
                ),
                "amount": 83.11,
                "currency": "EUR",
                "date": "2026-05-12",
            },
        ]
        bezala.list_accounts.return_value = []
        bezala.list_cost_centers.return_value = []
        bezala.list_vat_rates.return_value = []
        return bezala

    def _post_match(self, mid: int, bill_line_id: int = 2200001,
                    drive_bytes: bytes = PDF_BYTES,
                    image_to_pdf_returns: bytes | None = None):
        fake_drive = MagicMock()
        fake_drive.download_pdf.return_value = drive_bytes
        fake_bezala = self._fake_bezala(bill_line_id=bill_line_id)

        drive_patch = patch.object(
            self.app_module, "DriveClient", return_value=fake_drive,
        )
        bezala_patch = patch.object(
            self.app_module, "BezalaClient", return_value=fake_bezala,
        )
        # Mocka image_to_pdf så testet inte beror på weasyprint-runtimen.
        if image_to_pdf_returns is not None:
            img_patch = patch.object(
                self.app_module, "image_to_pdf",
                return_value=image_to_pdf_returns,
            )
        else:
            img_patch = patch.object(
                self.app_module, "image_to_pdf",
                side_effect=AssertionError(
                    "image_to_pdf should not be called for PDF bytes",
                ),
            )

        with drive_patch, bezala_patch, img_patch:
            resp = self.client.post(
                f"/api/messages/{mid}/match-to-bezala",
                json={"missing_receipt_id": bill_line_id},
            )
        return resp, fake_drive, fake_bezala

    def test_match_to_bezala_with_manual_upload_pdf_message(self):
        """Ett upload_source='manual_upload'-meddelande med PDF-fil ska
        kunna couplas precis som ett Gmail-kvitto. Inget 502, ingen
        krasch — bezala_transaction_id sätts av attach_file."""
        mid = self._seed_manual_upload()
        resp, _drive, bezala = self._post_match(mid, drive_bytes=PDF_BYTES)

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["upload_source"], "manual_upload")
        self.assertEqual(body["bezala_upload_status"], "success")
        self.assertEqual(body["bezala_transaction_id"], "tx-501")
        # attach_file fick PDF-bytes och filename oförändrade
        bezala.attach_file.assert_called_once()
        args, _kwargs = bezala.attach_file.call_args
        self.assertEqual(args[2], PDF_BYTES)
        self.assertEqual(args[1], "autopesu.pdf")

    def test_match_to_bezala_manual_upload_jpg_converted_to_pdf(self):
        """C35 root cause: manuell upload kan vara JPG/PNG (fotat papperskvitto)
        — match-to-bezala måste konvertera till PDF innan attach_file istället
        för att 502:a på %PDF-magic-byte-checken. När bytes börjar med JPEG-
        magic ska image_to_pdf anropas och resultatet skickas till Bezala."""
        mid = self._seed_manual_upload(
            file_name="autopesu.jpg",
            subject="autopesu.jpg",
        )
        converted_pdf = b"%PDF-1.4\nconverted from jpg"
        resp, _drive, bezala = self._post_match(
            mid,
            drive_bytes=JPEG_BYTES,
            image_to_pdf_returns=converted_pdf,
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["bezala_upload_status"], "success")
        # attach_file ska ha fått den konverterade PDFen, inte JPG-bytena
        bezala.attach_file.assert_called_once()
        args, _kwargs = bezala.attach_file.call_args
        self.assertEqual(args[2], converted_pdf)
        # filnamnet ska sluta på .pdf för Bezala även om Drive-filen är .jpg
        self.assertTrue(
            args[1].endswith(".pdf"),
            f"attach_file filename ska sluta på .pdf, fick {args[1]!r}",
        )

    def test_match_to_bezala_manual_upload_png_converted_to_pdf(self):
        """PNG-uppladdningar (screenshots etc) ska gå samma väg som JPG."""
        mid = self._seed_manual_upload(
            file_name="kvitto.png",
            subject="kvitto.png",
        )
        converted_pdf = b"%PDF-1.4\nconverted from png"
        resp, _drive, bezala = self._post_match(
            mid,
            drive_bytes=PNG_BYTES,
            image_to_pdf_returns=converted_pdf,
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        bezala.attach_file.assert_called_once()
        args, _kwargs = bezala.attach_file.call_args
        self.assertEqual(args[2], converted_pdf)
        self.assertTrue(args[1].endswith(".pdf"))

    def test_match_to_bezala_manual_upload_missing_gmail_fields(self):
        """Gmail-specifika fält (thread_id, summary, category, sender utan
        domän) ska inte krascha coupling-vägen — matchningen bygger på
        drive_file_id + amount + receipt_date + vendor, inte Gmail-metadata."""
        mid = self._seed_manual_upload(
            thread_id=None,
            sender="manual-upload@bezala-bot",
            subject=None,
            category=None,
            summary=None,
            # Endast ai_description_en räknas — och även om den är None
            # ska build_description hitta något via file_name.
        )
        resp, _drive, bezala = self._post_match(mid)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["bezala_upload_status"], "success")
        # Description ska komma från ai_description_en (vår seed-default)
        # eller fallback via build_description — aldrig krascha.
        bezala.attach_file.assert_called_once()
        kwargs = bezala.attach_file.call_args.kwargs
        self.assertIn("description", kwargs)
        self.assertTrue(
            kwargs["description"],
            "description får inte vara tom även när Gmail-fält saknas",
        )

    def test_match_to_bezala_manual_upload_corrupt_drive_file_502(self):
        """Om Drive-filen varken är PDF eller JPG/PNG (korrupt fil) ska
        endpointen returnera 502 med tydligt felmeddelande — inte krascha
        i attach_file eller skicka skräp till Bezala."""
        mid = self._seed_manual_upload()
        garbage = b"<html>oops not a receipt</html>"
        resp, _drive, bezala = self._post_match(mid, drive_bytes=garbage)
        self.assertEqual(resp.status_code, 502, resp.text)
        # Bezala-anropet ska INTE ha skett
        bezala.attach_file.assert_not_called()

    def test_ensure_pdf_for_bezala_passes_pdf_through(self):
        """Unit-test för helpern: PDF-bytes ska gå rakt igenom utan
        konvertering eller filnamnsändring."""
        from app.main import _ensure_pdf_for_bezala
        out_bytes, out_name = _ensure_pdf_for_bezala(
            PDF_BYTES, drive_file_id="drv-1", file_name="kvitto.pdf",
        )
        self.assertEqual(out_bytes, PDF_BYTES)
        self.assertEqual(out_name, "kvitto.pdf")

    def test_ensure_pdf_for_bezala_converts_jpeg(self):
        """JPEG-magic-bytes triggar image_to_pdf och filnamnet får .pdf."""
        from app import main as app_module
        converted = b"%PDF-1.4\nstub"
        with patch.object(app_module, "image_to_pdf", return_value=converted):
            out_bytes, out_name = app_module._ensure_pdf_for_bezala(
                JPEG_BYTES, drive_file_id="drv-1", file_name="autopesu.jpg",
            )
        self.assertEqual(out_bytes, converted)
        self.assertEqual(out_name, "autopesu.pdf")

    def test_ensure_pdf_for_bezala_raises_502_on_garbage(self):
        """Bytes som varken är PDF eller bild → HTTPException 502."""
        from fastapi import HTTPException
        from app.main import _ensure_pdf_for_bezala
        with self.assertRaises(HTTPException) as ctx:
            _ensure_pdf_for_bezala(
                b"\x00\x01\x02junk", drive_file_id="drv-1",
                file_name="trasig.bin",
            )
        self.assertEqual(ctx.exception.status_code, 502)


if __name__ == "__main__":
    unittest.main()
