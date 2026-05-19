"""Backend-tester för Gate 0-fix: upload_receipt + /api/bezala/metadata +
pipeline auto-upload med fältmappning.

Täcker:
- BezalaClient.upload_receipt skickar file + description + date + vat_lines
  i samma multipart-request
- upload_receipt validerar obligatoriska fält (description, date, PDF-bytes)
- upload_receipt bubblar 422 med full response.text
- GET /api/bezala/metadata returnerar accounts/cost_centers/vat_rates
  med error-field när endpoints fallerar
- POST /api/messages/{id}/upload-to-bezala använder upload_receipt + mapper
- Pipeline _attempt_bezala_upload använder metadata
"""

from __future__ import annotations

import json
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
os.environ.setdefault("BEZALA_USERNAME", "test@example.com")
os.environ.setdefault("BEZALA_PASSWORD", "secret")
os.environ.setdefault("SCAN_ENABLED", "false")
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nfake"


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


def _make_client():
    from app.services.bezala_client import BezalaClient

    client = BezalaClient.__new__(BezalaClient)
    client._email = "test@example.com"
    client._password = "secret"
    client._base_url = "https://mock.bezala"
    client._client = MagicMock()
    client._token = "fake-token"
    client._token_expires_at = 9e18
    return client


class UploadReceiptTest(unittest.TestCase):
    """BezalaClient.upload_receipt — single-step multipart med metadata."""

    def test_two_step_flow_happy_path(self):
        """Nya flödet (draft-first): upload_receipt gör TVÅ anrop:
          1. POST /attachments (multipart file + draft=1) → får tx_id
          2. PUT /transactions/{tx_id} med metadata-JSON."""
        calls: list = []

        client = _make_client()

        draft_resp = MagicMock()
        draft_resp.status_code = 201
        draft_resp.headers = {"content-type": "application/json"}
        draft_resp.text = '{"id": 1091, "transaction_id": 2804}'
        draft_resp.json = MagicMock(
            return_value={"id": 1091, "transaction_id": 2804},
        )

        put_resp = MagicMock()
        put_resp.status_code = 200
        put_resp.headers = {"content-type": "application/json"}
        put_resp.text = '{"id": 2804}'
        put_resp.json = MagicMock(return_value={"id": 2804})

        def fake_request(method, url, **kwargs):
            calls.append({
                "method": method, "url": url,
                "json": kwargs.get("json"),
                "files": kwargs.get("files"),
                "data": kwargs.get("data"),
            })
            # POST /attachments → draft_resp
            # PUT /transactions/{id} → put_resp
            if method == "POST" and url.endswith("/attachments"):
                return draft_resp
            if method == "PUT" and "/transactions/" in url:
                return put_resp
            return draft_resp

        client._client.request = fake_request

        result = client.upload_receipt(
            filename="20260422 Finnair.pdf",
            pdf_bytes=PDF_BYTES,
            description="20260422 Finnair HEL-CPH",
            date="2026-04-22",
            credit_account_id=67100,
            vat_lines_attributes=[{
                "taxable": "503.00",
                "tax_percentage": "0.255",
                "currency": "EUR",
                "expense_account_id": 67100,
                "cost_center_ids": [927151],
                "vat_code_id": 1355,
            }],
        )

        self.assertEqual(len(calls), 2)

        # Steg 1: POST /attachments med file + draft=1
        step1 = calls[0]
        self.assertEqual(step1["method"], "POST")
        self.assertTrue(step1["url"].endswith("/attachments"))
        self.assertIsNone(step1["json"])
        self.assertIn("file", step1["files"])
        fname, bytes_, mime = step1["files"]["file"]
        self.assertEqual(fname, "20260422 Finnair.pdf")
        self.assertEqual(bytes_, PDF_BYTES)
        self.assertEqual(mime, "application/pdf")
        # FAS 5.28 (C21) — state="unapproved" skickas i CREATE-formen
        # så att Bezala aldrig defaultar nya drafter till "reviewing".
        self.assertEqual(step1["data"], {"draft": "1", "state": "unapproved"})

        # Steg 2: PUT /transactions/{tx_id} med metadata
        step2 = calls[1]
        self.assertEqual(step2["method"], "PUT")
        self.assertTrue(step2["url"].endswith("/transactions/2804"))
        self.assertIsNone(step2["files"])
        wrapped = step2["json"]
        self.assertIn("transaction", wrapped)
        body = wrapped["transaction"]
        self.assertEqual(body["description"], "20260422 Finnair HEL-CPH")
        self.assertEqual(body["date"], "2026-04-22")
        self.assertEqual(body["credit_account_id"], 67100)
        self.assertEqual(body["vat_lines_attributes"], [{
            "taxable": "503.00",
            "tax_percentage": "0.255",
            "currency": "EUR",
            "expense_account_id": 67100,
            "cost_center_ids": [927151],
            "vat_code_id": 1355,
        }])

        # Returnerat attachment_id = transaction_id (används för deep-link)
        self.assertEqual(result.attachment_id, "2804")

    def test_rejects_missing_description(self):
        from app.services.bezala_client import BezalaError

        client = _make_client()
        with self.assertRaises(BezalaError) as ctx:
            client.upload_receipt(
                filename="x.pdf",
                pdf_bytes=PDF_BYTES,
                description="",
                date="2026-04-22",
            )
        self.assertIn("description", str(ctx.exception).lower())

    def test_rejects_missing_date(self):
        from app.services.bezala_client import BezalaError

        client = _make_client()
        with self.assertRaises(BezalaError):
            client.upload_receipt(
                filename="x.pdf",
                pdf_bytes=PDF_BYTES,
                description="x",
                date="",
            )

    def test_rejects_invalid_pdf(self):
        from app.services.bezala_client import BezalaError

        client = _make_client()
        with self.assertRaises(BezalaError) as ctx:
            client.upload_receipt(
                filename="x.pdf",
                pdf_bytes=b"not a pdf",
                description="x",
                date="2026-04-22",
            )
        self.assertIn("pdf", str(ctx.exception).lower())

    def test_upload_file_as_draft_returns_attachment_and_transaction_ids(self):
        """POST /attachments med draft=1 → returnerar både attachment_id
        och transaction_id från Bezala-svaret."""
        captured = {}

        client = _make_client()
        resp = MagicMock()
        resp.status_code = 201
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"id": 1091, "transaction_id": 2804}'
        resp.json = MagicMock(
            return_value={"id": 1091, "transaction_id": 2804},
        )

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["files"] = kwargs.get("files")
            captured["data"] = kwargs.get("data")
            return resp

        client._client.request = fake_request

        attachment_id, transaction_id = client.upload_file_as_draft(
            PDF_BYTES, "kvitto.pdf",
        )
        self.assertEqual(attachment_id, "1091")
        self.assertEqual(transaction_id, "2804")
        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith("/attachments"))
        # FAS 5.28 (C21) — state="unapproved" i CREATE-formen.
        self.assertEqual(captured["data"], {"draft": "1", "state": "unapproved"})
        self.assertIn("file", captured["files"])
        fname, bytes_, mime = captured["files"]["file"]
        self.assertEqual(fname, "kvitto.pdf")
        self.assertEqual(bytes_, PDF_BYTES)
        self.assertEqual(mime, "application/pdf")

    def test_put_transaction_failure_logs_orphan(self):
        """Nya flödet: steg 1 (POST /attachments draft) lyckas men
        steg 2 (PUT /transactions/{id}) 500:ar → ORPHAN-log med tx_id
        så användaren kan städa manuellt."""
        from app.services.bezala_client import BezalaError

        client = _make_client()

        draft_resp = MagicMock()
        draft_resp.status_code = 201
        draft_resp.headers = {"content-type": "application/json"}
        draft_resp.text = '{"id": 1091, "transaction_id": "tx-orphan-1"}'
        draft_resp.json = MagicMock(return_value={
            "id": 1091, "transaction_id": "tx-orphan-1",
        })

        put_resp = MagicMock()
        put_resp.status_code = 500
        put_resp.headers = {"content-type": "application/json"}
        put_resp.text = '{"error": "internal"}'
        put_resp.json = MagicMock(return_value={"error": "internal"})

        def fake_request(method, url, **kwargs):
            if method == "POST" and url.endswith("/attachments"):
                return draft_resp
            return put_resp

        client._client.request = fake_request

        with self.assertLogs("app.services.bezala_client", level="ERROR") as cm:
            with self.assertRaises(BezalaError) as ctx:
                client.upload_receipt(
                    filename="x.pdf",
                    pdf_bytes=PDF_BYTES,
                    description="Test",
                    date="2026-04-22",
                    credit_account_id=67100,
                    vat_lines_attributes=[{
                        "taxable": "10.00", "tax_percentage": "0.255",
                        "currency": "EUR", "expense_account_id": 67100,
                        "vat_code_id": 1355,
                    }],
                )

        # Felet ska nämna tx_id i felmeddelandet
        self.assertIn("tx-orphan-1", str(ctx.exception))
        # ORPHAN-log ska ha skrivits
        orphan_logs = [line for line in cm.output if "ORPHAN" in line]
        self.assertTrue(orphan_logs, f"Saknar ORPHAN-log i: {cm.output}")
        self.assertIn("tx-orphan-1", orphan_logs[0])

    def test_transactions_422_bubbles_full_body(self):
        """Steg 1 (POST /transactions) 422 → BezalaError.body innehåller
        hela response.text + /attachments anropas aldrig."""
        from app.services.bezala_client import BezalaError

        client = _make_client()
        resp = MagicMock()
        resp.status_code = 422
        resp.headers = {"content-type": "application/json"}
        resp.text = (
            '{"errors":{"vat_lines":["är för kort (minst 1 tecken)"],'
            '"account_id":["kan inte vara tom"]}}'
        )
        resp.json = MagicMock(return_value={})

        call_count = {"n": 0}

        def fake_request(method, url, **kwargs):
            call_count["n"] += 1
            return resp

        client._client.request = fake_request

        with self.assertRaises(BezalaError) as ctx:
            client.upload_receipt(
                filename="x.pdf",
                pdf_bytes=PDF_BYTES,
                description="Test",
                date="2026-04-22",
                credit_account_id=67100,
                vat_lines_attributes=[{
                    "taxable": "10.00", "tax_percentage": "0.255",
                    "currency": "EUR", "expense_account_id": 67100,
                    "vat_code_id": 1355,
                }],
            )
        err = ctx.exception
        self.assertEqual(err.status_code, 422)
        self.assertIn("vat_lines", err.body)
        self.assertIn("account_id", err.body)
        # Bara steg 1 ska ha anropats — /attachments ska aldrig träffas
        self.assertEqual(call_count["n"], 1)


class DeleteTransactionTest(unittest.TestCase):
    """C22 — BezalaClient.delete_transaction (DELETE /transactions/{id})."""

    def test_delete_success_returns_deleted_true(self):
        client = _make_client()
        resp = MagicMock()
        resp.status_code = 204
        resp.headers = {"content-type": "application/json"}
        resp.text = ""

        captured = {}

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            return resp

        client._client.request = fake_request

        result = client.delete_transaction("2804")
        self.assertEqual(result, {"deleted": True})
        self.assertEqual(captured["method"], "DELETE")
        self.assertTrue(captured["url"].endswith("/transactions/2804"))

    def test_delete_404_is_idempotent(self):
        """Redan raderat utkast → already_gone=True så caller kan rensa
        lokal koppling utan att blockera på Bezala."""
        client = _make_client()
        resp = MagicMock()
        resp.status_code = 404
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"error": "not found"}'

        client._client.request = MagicMock(return_value=resp)

        result = client.delete_transaction(9999)
        self.assertEqual(result, {"deleted": True, "already_gone": True})

    def test_delete_500_raises_bezala_error(self):
        from app.services.bezala_client import BezalaError

        client = _make_client()
        resp = MagicMock()
        resp.status_code = 500
        resp.headers = {"content-type": "text/plain"}
        resp.text = "internal server error"

        # Bypass retries genom att returnera 500 från alla försök
        client._client.request = MagicMock(return_value=resp)

        with self.assertRaises(BezalaError) as ctx:
            client.delete_transaction("tx-bad")
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("internal server error", ctx.exception.body or "")

    def test_delete_422_raises_bezala_error(self):
        """Endast 404 är idempotent — andra 4xx (t.ex. 422 redan attesterad)
        ska bubbla upp som fel så att caller behåller lokal koppling."""
        from app.services.bezala_client import BezalaError

        client = _make_client()
        resp = MagicMock()
        resp.status_code = 422
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"errors": ["redan attesterad"]}'

        client._client.request = MagicMock(return_value=resp)

        with self.assertRaises(BezalaError) as ctx:
            client.delete_transaction("tx-locked")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_delete_rejects_empty_id(self):
        from app.services.bezala_client import BezalaError

        client = _make_client()
        with self.assertRaises(BezalaError):
            client.delete_transaction("")


class PipelineAutoUploadTest(unittest.TestCase):
    """_attempt_bezala_upload använder metadata + field-mapper."""

    def test_happy_path_calls_upload_receipt(self):
        from app.services.pipeline import _attempt_bezala_upload
        from app.services.receipt_analyzer import ReceiptAnalysis
        from app.services.gmail_client import GmailMessage

        analysis = ReceiptAnalysis(
            is_receipt=True,
            confidence=95,
            filename="20260422 Finnair HEL-CPH.pdf",
            vendor="Finnair",
            amount=503.0,
            currency="EUR",
            date="2026-04-22",
            category="Flyg",
            summary="Flyg HEL-CPH",
        )
        msg = GmailMessage(
            message_id="m1",
            thread_id="t1",
            sender="noreply@finnair.com",
            subject="Finnair kvitto",
            received_at=datetime(2026, 4, 22, tzinfo=timezone.utc),
            snippet="",
        )
        # Live-IDs från Bezala-metadata + default_vat_id-strategin
        metadata = {
            "accounts": [
                {"id": 67100, "name": "Matkaliput", "default_vat_id": 1355},
            ],
            "cost_centers": [{"id": 927151, "name": "VIS128 Visma HRM Sverige AB"}],
            "vat_rates": [],
        }

        fake_bezala = MagicMock()
        fake_receipt = MagicMock()
        fake_receipt.attachment_id = "receipt-99"
        fake_bezala.upload_receipt.return_value = fake_receipt

        status, txn_id, err = _attempt_bezala_upload(
            fake_bezala,
            analysis,
            msg,
            PDF_BYTES,
            "20260422 Finnair HEL-CPH.pdf",
            auto_upload=True,
            confidence_threshold=90,
            metadata=metadata,
        )

        self.assertEqual(status, "success")
        self.assertEqual(txn_id, "receipt-99")
        self.assertIsNone(err)
        fake_bezala.upload_receipt.assert_called_once()
        kwargs = fake_bezala.upload_receipt.call_args.kwargs
        # credit_account_id = betalningsmetod (kreditkort), default 82320
        # från BEZALA_CREDIT_ACCOUNT_ID env — inte kategori-kontot.
        self.assertEqual(kwargs["credit_account_id"], 82320)
        # expense_account_id i vat_lines_attributes är däremot kategorin
        # (67100 Matkaliput för Flyg).
        self.assertEqual(kwargs["vat_lines_attributes"], [{
            "taxable": "503.00",
            "tax_percentage": "0.255",
            "currency": "EUR",
            "expense_account_id": 67100,
            "cost_center_ids": [927151],
            "vat_code_id": 1355,
        }])
        self.assertEqual(kwargs["description"], "20260422 Finnair HEL-CPH")
        self.assertEqual(kwargs["date"], "2026-04-22")

    def test_missing_vat_rate_proceeds_with_empty_vat_lines(self):
        """Ny strategi: vat_lines=[] är OK — Bezala plockar kontots
        default_vat_id själv. Pipeline fortsätter med upload."""
        from app.services.pipeline import _attempt_bezala_upload
        from app.services.receipt_analyzer import ReceiptAnalysis
        from app.services.gmail_client import GmailMessage

        analysis = ReceiptAnalysis(
            is_receipt=True,
            confidence=95,
            filename="x.pdf",
            vendor="X",
            amount=10.0,
            currency="EUR",
            date="2026-04-22",
            category="Programvara",
            summary="s",
        )
        msg = GmailMessage(
            message_id="m1", thread_id="t", sender="a@b.com", subject="s",
            received_at=datetime(2026, 4, 22, tzinfo=timezone.utc), snippet="",
        )
        # Programvara → account 82612 (default_vat_id=null → vat_lines=[])
        metadata = {
            "accounts": [{"id": 82612, "name": "Atk-ohjelmistot", "default_vat_id": None}],
            "cost_centers": [{"id": 927151, "name": "VIS128"}],
            "vat_rates": [],
        }

        fake_bezala = MagicMock()
        fake_receipt = MagicMock()
        fake_receipt.attachment_id = "r-1"
        fake_bezala.upload_receipt.return_value = fake_receipt

        status, txn_id, err = _attempt_bezala_upload(
            fake_bezala, analysis, msg, PDF_BYTES, "x.pdf",
            auto_upload=True, confidence_threshold=90, metadata=metadata,
        )
        self.assertEqual(status, "success")
        self.assertEqual(txn_id, "r-1")
        self.assertIsNone(err)
        fake_bezala.upload_receipt.assert_called_once()
        # vat_lines_attributes ska vara tom när default_vat_id=None
        kwargs = fake_bezala.upload_receipt.call_args.kwargs
        self.assertEqual(kwargs["vat_lines_attributes"], [])
        # credit_account_id = hårdkodad default 82320 (betalningsmetod)
        self.assertEqual(kwargs["credit_account_id"], 82320)

    def test_missing_amount_or_date_returns_pending(self):
        from app.services.pipeline import _attempt_bezala_upload
        from app.services.receipt_analyzer import ReceiptAnalysis
        from app.services.gmail_client import GmailMessage

        analysis = ReceiptAnalysis(
            is_receipt=True,
            confidence=95,
            filename="x.pdf",
            vendor="X",
            amount=None,  # ← saknas
            currency="EUR",
            date="2026-04-22",
            category="Flyg",
            summary="s",
        )
        msg = GmailMessage(
            message_id="m1", thread_id="t", sender="a@b.com", subject="s",
            received_at=datetime(2026, 4, 22, tzinfo=timezone.utc), snippet="",
        )

        fake_bezala = MagicMock()
        status, _, err = _attempt_bezala_upload(
            fake_bezala, analysis, msg, PDF_BYTES, "x.pdf",
            auto_upload=True, confidence_threshold=90,
            metadata={"accounts": [], "cost_centers": [], "vat_rates": []},
        )
        self.assertEqual(status, "pending")
        self.assertIn("amount", err.lower())
        fake_bezala.upload_receipt.assert_not_called()

    def test_bezala_422_bubbles_body_in_error(self):
        from app.services.pipeline import _attempt_bezala_upload
        from app.services.bezala_client import BezalaError
        from app.services.receipt_analyzer import ReceiptAnalysis
        from app.services.gmail_client import GmailMessage

        analysis = ReceiptAnalysis(
            is_receipt=True, confidence=95, filename="x.pdf", vendor="X",
            amount=10.0, currency="EUR", date="2026-04-22",
            category="Flyg", summary="s",
        )
        msg = GmailMessage(
            message_id="m1", thread_id="t", sender="noreply@finnair.com",
            subject="s", received_at=datetime(2026, 4, 22, tzinfo=timezone.utc),
            snippet="",
        )
        metadata = {
            "accounts": [{"id": 1, "name": "Matkaliput"}],
            "cost_centers": [{"id": 2, "name": "Default"}],
            "vat_rates": [{"id": 3, "name": "Finland Transport 13.5%"}],
        }

        fake_bezala = MagicMock()
        fake_bezala.upload_receipt.side_effect = BezalaError(
            "Bezala upload_receipt: 422",
            status_code=422,
            body='{"errors":{"account_id":["kan inte vara tom"]}}',
        )

        status, _, err = _attempt_bezala_upload(
            fake_bezala, analysis, msg, PDF_BYTES, "x.pdf",
            auto_upload=True, confidence_threshold=90, metadata=metadata,
        )
        self.assertEqual(status, "failed")
        self.assertIn("account_id", err)  # body bevarad


class FetchBezalaMetadataTest(unittest.TestCase):
    def test_metadata_swallows_per_endpoint_errors(self):
        from app.services.bezala_client import BezalaError
        from app.services.pipeline import fetch_bezala_metadata

        fake = MagicMock()
        fake.list_accounts.return_value = [{"id": 1, "name": "A"}]
        fake.list_cost_centers.side_effect = BezalaError(
            "500", status_code=500, body="server error",
        )
        fake.list_vat_rates.return_value = [{"id": 2, "name": "FI 25.5%"}]

        metadata = fetch_bezala_metadata(fake)
        self.assertEqual(len(metadata["accounts"]), 1)
        self.assertEqual(metadata["cost_centers"], [])  # swallowed
        self.assertEqual(len(metadata["vat_rates"]), 1)


# ============================================================
# /api/bezala/metadata endpoint
# ============================================================


class BezalaMetadataEndpointTest(unittest.TestCase):
    """Verifiera att GET /api/bezala/metadata returnerar rätt shape även
    när enskilda Bezala-endpoints fallerar."""

    @classmethod
    def setUpClass(cls):
        db_module = _configure_memory_engine()
        from app.db import Base
        from app import models  # noqa: F401
        from app import main as app_module
        from fastapi.testclient import TestClient

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

        app_module.app.dependency_overrides[app_module.require_auth] = fake_require_auth
        cls.client = TestClient(app_module.app)
        cls.app_module = app_module

    @classmethod
    def tearDownClass(cls):
        cls.app_module.app.dependency_overrides.clear()

    def test_metadata_returns_rows_from_all_three(self):
        fake_bezala = MagicMock()
        fake_bezala.list_accounts.return_value = [{"id": 1, "name": "Matkaliput"}]
        fake_bezala.list_cost_centers.return_value = [{"id": 2, "name": "Default"}]
        fake_bezala.list_vat_rates.return_value = [
            {"id": 3, "name": "Finland Transport 13.5%"},
        ]

        with patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.get("/api/bezala/metadata")

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["accounts"]["count"], 1)
        self.assertEqual(body["accounts"]["rows"][0]["name"], "Matkaliput")
        self.assertIsNone(body["accounts"]["error"])
        self.assertEqual(body["cost_centers"]["count"], 1)
        self.assertEqual(body["vat_rates"]["count"], 1)

    def test_metadata_shows_per_endpoint_errors(self):
        """Om /vat_rates kastar BezalaError → error-fältet innehåller
        status + body istället för att hela requesten 500:ar."""
        from app.services.bezala_client import BezalaError

        fake_bezala = MagicMock()
        fake_bezala.list_accounts.return_value = [{"id": 1}]
        fake_bezala.list_cost_centers.return_value = []
        fake_bezala.list_vat_rates.side_effect = BezalaError(
            "500", status_code=500, body="internal error",
        )

        with patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.get("/api/bezala/metadata")

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["accounts"]["count"], 1)
        self.assertIsNotNone(body["vat_rates"]["error"])
        self.assertIn("500", body["vat_rates"]["error"])


# ============================================================
# /api/messages/{id}/upload-to-bezala integration
# ============================================================


class UploadToBezalaEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_module = _configure_memory_engine()
        from app.db import Base
        from app import models  # noqa: F401
        from app import main as app_module
        from app.models import ProcessedMessage
        from fastapi.testclient import TestClient

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

        app_module.app.dependency_overrides[app_module.require_auth] = fake_require_auth
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

    def _seed(self, **overrides) -> int:
        defaults = dict(
            message_id="gm-1",
            sender="noreply@finnair.com",
            subject="Kvitto",
            status="saved",
            file_name="20260422 Finnair HEL-CPH.pdf",
            drive_file_id="drv-1",
            drive_link="https://drive/drv-1",
            vendor="Finnair",
            amount=503.0,
            currency="EUR",
            receipt_date="2026-04-22",
            category="Flyg",
            summary="Flyg HEL-CPH",
            # FAS 5.9 — engelsk Bezala-beskrivning (vendor + plats + datum)
            ai_description_en=(
                "Flight Helsinki-Copenhagen round trip, 22 April 2026"
            ),
            ai_confidence=95,
            bezala_upload_status="pending",
        )
        defaults.update(overrides)
        with self.SessionLocal() as db:
            row = self.ProcessedMessage(**defaults)
            db.add(row)
            db.flush()
            mid = row.id
            db.commit()
        return mid

    def test_happy_path_calls_upload_receipt_with_mapped_fields(self):
        mid = self._seed()

        fake_drive = MagicMock()
        fake_drive.download_pdf.return_value = PDF_BYTES

        fake_bezala = MagicMock()
        # Live-verifierade IDs från produktionens Bezala-metadata
        fake_bezala.list_accounts.return_value = [
            {"id": 67100, "name": "Matkaliput", "default_vat_id": 1355},
        ]
        fake_bezala.list_cost_centers.return_value = [
            {"id": 927151, "name": "VIS128 Visma HRM Sverige AB"},
        ]
        fake_bezala.list_vat_rates.return_value = []
        fake_receipt = MagicMock()
        fake_receipt.attachment_id = "receipt-99"
        fake_bezala.upload_receipt.return_value = fake_receipt

        with patch.object(self.app_module, "DriveClient", return_value=fake_drive), \
             patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.post(f"/api/messages/{mid}/upload-to-bezala")

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["bezala_upload_status"], "success")
        self.assertEqual(body["bezala_transaction_id"], "receipt-99")

        fake_bezala.upload_receipt.assert_called_once()
        kwargs = fake_bezala.upload_receipt.call_args.kwargs
        # credit_account_id = betalningsmetod (82320 default)
        self.assertEqual(kwargs["credit_account_id"], 82320)
        # FAS 5.9 — manuell upload prioriterar row.ai_description_en
        # (engelsk AI-beskrivning) framför filnamns-fallbacken.
        self.assertEqual(
            kwargs["description"],
            "Flight Helsinki-Copenhagen round trip, 22 April 2026",
        )
        self.assertEqual(kwargs["date"], "2026-04-22")
        self.assertEqual(kwargs["vat_lines_attributes"], [{
            "taxable": "503.00",
            "tax_percentage": "0.255",
            "currency": "EUR",
            "expense_account_id": 67100,
            "cost_center_ids": [927151],
            "vat_code_id": 1355,
        }])

    def test_missing_date_returns_400(self):
        mid = self._seed(receipt_date=None)
        resp = self.client.post(f"/api/messages/{mid}/upload-to-bezala")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("datum", resp.json()["detail"].lower())

    def test_missing_amount_returns_400(self):
        mid = self._seed(amount=None)
        resp = self.client.post(f"/api/messages/{mid}/upload-to-bezala")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("belopp", resp.json()["detail"].lower())

    def test_empty_metadata_still_attempts_upload(self):
        """Även utan accounts/cost_centers/vat_rates försöker vi upload —
        Bezala får 422:a om det behövs och vi loggar fel, men vi
        blockerar INTE på förhand."""
        mid = self._seed()

        fake_drive = MagicMock()
        fake_drive.download_pdf.return_value = PDF_BYTES

        fake_bezala = MagicMock()
        fake_bezala.list_accounts.return_value = []
        fake_bezala.list_cost_centers.return_value = []
        fake_bezala.list_vat_rates.return_value = []
        fake_receipt = MagicMock()
        fake_receipt.attachment_id = "r-empty"
        fake_bezala.upload_receipt.return_value = fake_receipt

        with patch.object(self.app_module, "DriveClient", return_value=fake_drive), \
             patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.post(f"/api/messages/{mid}/upload-to-bezala")

        self.assertEqual(resp.status_code, 200, resp.text)
        fake_bezala.upload_receipt.assert_called_once()
        kwargs = fake_bezala.upload_receipt.call_args.kwargs
        # credit_account_id = betalningsmetod (env-default 82320) — oberoende
        # av Bezala-metadata eftersom det är kreditkortet, inte kategorin.
        self.assertEqual(kwargs.get("credit_account_id"), 82320)
        # vat_lines_attributes är tom när vi saknar account-metadata
        self.assertEqual(kwargs.get("vat_lines_attributes", []), [])

    def test_empty_drive_download_returns_502(self):
        """Om DriveClient.download_pdf levererar tom bytes (eller ogiltig
        PDF) ska vi INTE skicka tomma fil-bytes till Bezala. Istället →
        502 med tydligt meddelande och raden markeras failed."""
        mid = self._seed()

        fake_drive = MagicMock()
        fake_drive.download_pdf.return_value = b""  # tom!

        fake_bezala = MagicMock()

        with patch.object(self.app_module, "DriveClient", return_value=fake_drive), \
             patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.post(f"/api/messages/{mid}/upload-to-bezala")

        self.assertEqual(resp.status_code, 502)
        self.assertIn("PDF", resp.json()["detail"])
        # Bezala SKA INTE ha anropats
        fake_bezala.upload_receipt.assert_not_called()
        # Raden markeras failed
        with self.SessionLocal() as db:
            row = db.query(self.ProcessedMessage).filter_by(id=mid).first()
            self.assertEqual(row.bezala_upload_status, "failed")

    def test_non_pdf_drive_bytes_returns_502(self):
        """Om Drive-innehållet inte börjar med %PDF (t.ex. HTML-felsida)
        ska vi stoppa innan Bezala-anropet."""
        mid = self._seed()

        fake_drive = MagicMock()
        fake_drive.download_pdf.return_value = b"<html>Drive error</html>"

        fake_bezala = MagicMock()

        with patch.object(self.app_module, "DriveClient", return_value=fake_drive), \
             patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.post(f"/api/messages/{mid}/upload-to-bezala")

        self.assertEqual(resp.status_code, 502)
        fake_bezala.upload_receipt.assert_not_called()

    def test_missing_amount_returns_friendly_400(self):
        """Bug 1a: 400-text ska peka användaren till Granska-vyn."""
        mid = self._seed(amount=None)
        resp = self.client.post(f"/api/messages/{mid}/upload-to-bezala")
        self.assertEqual(resp.status_code, 400)
        detail = resp.json()["detail"]
        self.assertIn("belopp", detail.lower())
        self.assertIn("Granska", detail)

    def test_missing_date_returns_friendly_400(self):
        mid = self._seed(receipt_date=None)
        resp = self.client.post(f"/api/messages/{mid}/upload-to-bezala")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("datum", resp.json()["detail"].lower())
        self.assertIn("Granska", resp.json()["detail"])

    def test_amount_override_via_payload(self):
        """Bug 1b: UI kan skicka {amount: 123.45} → DB uppdateras +
        uploaden fortsätter med nya värdet även om DB tidigare var None."""
        mid = self._seed(amount=None)

        fake_drive = MagicMock()
        fake_drive.download_pdf.return_value = PDF_BYTES
        fake_bezala = MagicMock()
        fake_bezala.list_accounts.return_value = [{"id": 67100, "name": "Matkaliput", "default_vat_id": 1355}]
        fake_bezala.list_cost_centers.return_value = []
        fake_bezala.list_vat_rates.return_value = []
        fake_receipt = MagicMock()
        fake_receipt.attachment_id = "r-99"
        fake_bezala.upload_receipt.return_value = fake_receipt

        with patch.object(self.app_module, "DriveClient", return_value=fake_drive), \
             patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.post(
                f"/api/messages/{mid}/upload-to-bezala",
                json={"amount": 320.0, "receipt_date": "2026-04-21"},
            )

        self.assertEqual(resp.status_code, 200, resp.text)
        fake_bezala.upload_receipt.assert_called_once()
        kwargs = fake_bezala.upload_receipt.call_args.kwargs
        self.assertEqual(kwargs["date"], "2026-04-21")
        # amount överfört via vat_lines_attributes.taxable (string)
        self.assertEqual(kwargs["vat_lines_attributes"][0]["taxable"], "320.00")
        # DB ska ha uppdaterats med överstyrda värden
        with self.SessionLocal() as db:
            row = db.query(self.ProcessedMessage).filter_by(id=mid).first()
            self.assertEqual(row.amount, 320.0)
            self.assertEqual(row.receipt_date, "2026-04-21")

    def test_amount_override_zero_still_blocks(self):
        """Användaren skickar 0 → samma 400, lika bra som None."""
        mid = self._seed(amount=None)
        resp = self.client.post(
            f"/api/messages/{mid}/upload-to-bezala",
            json={"amount": 0},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("belopp", resp.json()["detail"].lower())

    def test_bezala_422_preserves_body_in_error(self):
        """Bezala kastar 422 → raden sparas med body i error_message."""
        from app.services.bezala_client import BezalaError

        mid = self._seed()

        fake_drive = MagicMock()
        fake_drive.download_pdf.return_value = PDF_BYTES

        fake_bezala = MagicMock()
        fake_bezala.list_accounts.return_value = [{"id": 1, "name": "Matkaliput"}]
        fake_bezala.list_cost_centers.return_value = [{"id": 2, "name": "Default"}]
        fake_bezala.list_vat_rates.return_value = [{"id": 3, "name": "Finland Transport 13.5%"}]
        fake_bezala.upload_receipt.side_effect = BezalaError(
            "Bezala upload_receipt: 422",
            status_code=422,
            body='{"errors":{"account_id":["kan inte vara tom"]}}',
        )

        with patch.object(self.app_module, "DriveClient", return_value=fake_drive), \
             patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.post(f"/api/messages/{mid}/upload-to-bezala")

        self.assertEqual(resp.status_code, 502)
        with self.SessionLocal() as db:
            row = db.query(self.ProcessedMessage).filter_by(id=mid).first()
            self.assertEqual(row.bezala_upload_status, "failed")
            self.assertIn("account_id", row.bezala_error_message)


if __name__ == "__main__":
    unittest.main()
