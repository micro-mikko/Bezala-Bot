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


if __name__ == "__main__":
    unittest.main()
