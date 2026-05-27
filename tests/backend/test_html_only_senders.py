"""Tester för HTML-only senders:
- service (seed, add, remove, toggle)
- build_gmail_query_html_only
- endpoints (GET/POST/DELETE/PATCH)
- (pipeline-integrationen testas indirekt via build_gmail_query_html_only)
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock

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
        bind=db_module.engine, autoflush=False, autocommit=False,
    )
    return db_module


class HtmlOnlySendersServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_module = _configure_memory_engine()
        from app.db import Base
        from app import models  # noqa: F401
        Base.metadata.create_all(bind=db_module.engine)
        cls.SessionLocal = db_module.SessionLocal
        from app.models import HtmlOnlySender, MaintenanceTask
        cls.HtmlOnlySender = HtmlOnlySender
        cls.MaintenanceTask = MaintenanceTask

    def setUp(self):
        with self.SessionLocal() as db:
            db.query(self.HtmlOnlySender).delete()
            db.query(self.MaintenanceTask).filter(
                self.MaintenanceTask.name == "seed_html_only_senders_v1",
            ).delete()
            db.commit()

    # ----- 1: seed kör idempotent (kör två gånger ger samma DB-state) -----
    def test_seed_idempotent(self):
        from app.services.html_only_senders import (
            seed_default_html_only_senders, DEFAULT_HTML_ONLY_SENDERS,
        )
        with self.SessionLocal() as db:
            added_1 = seed_default_html_only_senders(db)
            self.assertEqual(added_1, len(DEFAULT_HTML_ONLY_SENDERS))
            added_2 = seed_default_html_only_senders(db)
            self.assertEqual(added_2, 0)
            count = db.query(self.HtmlOnlySender).count()
            self.assertEqual(count, len(DEFAULT_HTML_ONLY_SENDERS))

    # ----- 2: default-seed läggs in första gången -----
    def test_default_seed_first_run(self):
        from app.services.html_only_senders import (
            seed_default_html_only_senders,
        )
        with self.SessionLocal() as db:
            seed_default_html_only_senders(db)
            patterns = {
                r.sender_pattern for r in db.query(self.HtmlOnlySender).all()
            }
        self.assertIn("skanetrafiken", patterns)
        self.assertIn("noreply@moovy.fi", patterns)
        self.assertIn("cursor", patterns)

    # ----- 3: GET listar -----
    def test_list_returns_active_and_inactive(self):
        from app.services.html_only_senders import (
            add_sender, list_html_only_senders, set_active,
        )
        with self.SessionLocal() as db:
            r1, _ = add_sender(db, "skanetrafiken", "tåg")
            r2, _ = add_sender(db, "moovy", "parkering")
            set_active(db, r2.id, False)
            rows = list_html_only_senders(db)
            self.assertEqual(len(rows), 2)
            # Aktiv-filter:
            only_active = list_html_only_senders(db, only_active=True)
            self.assertEqual(len(only_active), 1)
            self.assertEqual(only_active[0].id, r1.id)

    # ----- 4: add idempotent -----
    def test_add_is_idempotent(self):
        from app.services.html_only_senders import add_sender
        with self.SessionLocal() as db:
            r1, already_1 = add_sender(db, "Skanetrafiken", "x")
            self.assertFalse(already_1)
            r2, already_2 = add_sender(db, "skanetrafiken", "y")
            self.assertTrue(already_2)
            self.assertEqual(r1.id, r2.id)

    # ----- 5: remove fungerar -----
    def test_remove_works(self):
        from app.services.html_only_senders import add_sender, remove_sender
        with self.SessionLocal() as db:
            row, _ = add_sender(db, "skanetrafiken", "x")
            sid = row.id
            self.assertTrue(remove_sender(db, sid))
            self.assertEqual(
                db.query(self.HtmlOnlySender).filter_by(id=sid).count(), 0,
            )
            # Andra gången → False (finns inte längre)
            self.assertFalse(remove_sender(db, sid))

    # ----- 6: set_active togglar -----
    def test_set_active_toggles(self):
        from app.services.html_only_senders import add_sender, set_active
        with self.SessionLocal() as db:
            row, _ = add_sender(db, "skanetrafiken", "x")
            updated = set_active(db, row.id, False)
            self.assertIsNotNone(updated)
            self.assertFalse(updated.is_active)
            updated2 = set_active(db, row.id, True)
            self.assertTrue(updated2.is_active)
            # Okänt id → None
            self.assertIsNone(set_active(db, 999999, True))

    # ----- 7: list_active_patterns lowercased + skippar inaktiva -----
    def test_list_active_patterns_lowercased(self):
        from app.services.html_only_senders import (
            add_sender, list_active_patterns, set_active,
        )
        with self.SessionLocal() as db:
            add_sender(db, "Skanetrafiken", "x")
            r2, _ = add_sender(db, "Moovy", "y")
            set_active(db, r2.id, False)
            patterns = list_active_patterns(db)
        self.assertEqual(patterns, ["skanetrafiken"])

    # ----- 8: is_html_only_sender case-insensitive substring -----
    def test_is_html_only_sender_case_insensitive(self):
        from app.services.html_only_senders import is_html_only_sender
        patterns = ["skanetrafiken", "noreply@moovy.fi"]
        self.assertTrue(is_html_only_sender(
            "biljetter@SKANETRAFIKEN.SE", patterns,
        ))
        self.assertTrue(is_html_only_sender(
            "Bekräftelse <Noreply@moovy.fi>", patterns,
        ))
        self.assertFalse(is_html_only_sender(
            "info@vr.fi", patterns,
        ))
        self.assertFalse(is_html_only_sender(None, patterns))
        self.assertFalse(is_html_only_sender("any", []))


class GmailQueryBuilderTest(unittest.TestCase):
    """Testar build_gmail_query_html_only — pure function, ingen DB."""

    def _settings(self):
        # Minimal fake AppSettings — bara attribut som build_gmail_query_html_only
        # läser av.
        s = MagicMock()
        s.exclude_promotions = True
        s.exclude_social = True
        s.exclude_calendar = True
        s.exclude_senders = []
        s.exclude_subjects = []
        return s

    # ----- 9: html-only-query genereras med patterns -----
    def test_builds_query_with_patterns(self):
        from app.services.settings_service import build_gmail_query_html_only
        q = build_gmail_query_html_only(
            self._settings(),
            ["skanetrafiken", "noreply@moovy.fi"],
            done_label="Bezala-Klar",
        )
        self.assertIsNotNone(q)
        self.assertIn("from:skanetrafiken", q)
        self.assertIn("from:noreply@moovy.fi", q)
        self.assertIn("OR", q)
        # Får INTE innehålla has:attachment — det är poängen
        self.assertNotIn("has:attachment", q)
        # Bibehåller base-filter
        self.assertIn("-in:spam", q)
        self.assertIn("-in:trash", q)
        self.assertIn('-label:"Bezala-Klar"', q)

    # ----- 10: tom patterns-lista → None (ingen extra-skanning) -----
    def test_empty_patterns_returns_none(self):
        from app.services.settings_service import build_gmail_query_html_only
        self.assertIsNone(build_gmail_query_html_only(
            self._settings(), [], done_label="Bezala-Klar",
        ))
        self.assertIsNone(build_gmail_query_html_only(
            self._settings(), None, done_label="Bezala-Klar",
        ))

    # ----- 11: standard build_gmail_query har KVAR has:attachment
    #          (regression — vi får INTE ha brutit befintligt flöde) -----
    def test_standard_query_still_uses_attachment_filter(self):
        from app.services.settings_service import build_gmail_query
        s = MagicMock()
        s.require_attachments = True
        s.exclude_promotions = True
        s.exclude_social = True
        s.exclude_calendar = True
        s.exclude_senders = []
        s.exclude_subjects = []
        s.include_senders = []
        q = build_gmail_query(s, done_label="Bezala-Klar")
        self.assertIn("has:attachment", q)

    # ----- 12: standard build_gmail_query utan html_only_patterns ger
    #          samma resultat som tidigare (regression — kwarg-default) -----
    def test_standard_query_no_html_only_patterns_unchanged(self):
        from app.services.settings_service import build_gmail_query
        s = MagicMock()
        s.require_attachments = True
        s.exclude_promotions = True
        s.exclude_social = True
        s.exclude_calendar = True
        s.exclude_senders = []
        s.exclude_subjects = []
        s.include_senders = []
        q_default = build_gmail_query(s, done_label="Bezala-Klar")
        q_empty = build_gmail_query(
            s, done_label="Bezala-Klar", html_only_patterns=[],
        )
        q_none = build_gmail_query(
            s, done_label="Bezala-Klar", html_only_patterns=None,
        )
        self.assertEqual(q_default, q_empty)
        self.assertEqual(q_default, q_none)

    # ----- 13: standard build_gmail_query MED html_only_patterns lägger
    #          till "-from:<pattern>" för var och en så standard-passet
    #          inte hämtar dem (bug-fix för PR #20 dedup-problemet) -----
    def test_standard_query_excludes_html_only_senders(self):
        from app.services.settings_service import build_gmail_query
        s = MagicMock()
        s.require_attachments = True
        s.exclude_promotions = True
        s.exclude_social = True
        s.exclude_calendar = True
        s.exclude_senders = []
        s.exclude_subjects = []
        s.include_senders = []
        q = build_gmail_query(
            s, done_label="Bezala-Klar",
            html_only_patterns=[
                "skanetrafiken", "noreply@moovy.fi", "cursor", "airport",
            ],
        )
        self.assertIn("-from:skanetrafiken", q)
        self.assertIn("-from:noreply@moovy.fi", q)
        self.assertIn("-from:cursor", q)
        self.assertIn("-from:airport", q)
        # has:attachment ska FORTFARANDE finnas — vi blockerar bara
        # html_only-senders, inte hela attachment-filtret.
        self.assertIn("has:attachment", q)

    # ----- 14: dedup-fix — html-only-mail ska INTE bli matchad av
    #          standard-queryn längre. Verifiera att samma pattern
    #          finns både i `-from:` (exkluderar standard) och i
    #          OR-clause:n av html-only-queryn (inkluderar html-only). -----
    def test_dedup_separation_html_only_vs_standard(self):
        from app.services.settings_service import (
            build_gmail_query, build_gmail_query_html_only,
        )
        s = MagicMock()
        s.require_attachments = True
        s.exclude_promotions = True
        s.exclude_social = True
        s.exclude_calendar = True
        s.exclude_senders = []
        s.exclude_subjects = []
        s.include_senders = []
        patterns = ["skanetrafiken", "cursor"]

        std = build_gmail_query(
            s, done_label="Bezala-Klar", html_only_patterns=patterns,
        )
        html_only = build_gmail_query_html_only(
            s, patterns, done_label="Bezala-Klar",
        )
        # Standard-queryn EXKLUDERAR
        self.assertIn("-from:skanetrafiken", std)
        self.assertIn("-from:cursor", std)
        # html-only-queryn INKLUDERAR (i OR-clause:n)
        self.assertIn("from:skanetrafiken", html_only)
        self.assertIn("from:cursor", html_only)
        # html-only-queryn har INGEN has:attachment (poängen)
        self.assertNotIn("has:attachment", html_only)


# ---------- C32b: Stripe-invoice-filter ----------


class StripeInvoiceFilterTest(unittest.TestCase):
    """C32b: BUILTIN_INCLUDES måste täcka Stripe-genererade fakturor.

    Stripe skickar `invoice+statements+acct_<id>@stripe.com` för Bolt
    m.fl. Gmail-filtret allowlistade tidigare bara Anthropics
    `invoice+statements@mail.anthropic.com` exakt — Stripe-varianterna
    föll därför aldrig in i scan-queryns OR-clause och kvittona
    processades aldrig.

    Verifierat i prod 2026-05-26: `from:invoice+statements@stripe.com`
    med fullt domän-suffix matchade 0 mail i Gmail (Gmail gör INTE
    prefix-match på `+`-alias för icke-gmail-domäner). Bar
    `from:invoice+statements` matchade 25 — det är denna bredare
    substring-syntax vi använder.
    """

    def _settings(self):
        s = MagicMock()
        s.require_attachments = True
        s.exclude_promotions = True
        s.exclude_social = True
        s.exclude_calendar = True
        s.exclude_senders = []
        s.exclude_subjects = []
        s.include_senders = []
        return s

    def test_invoice_statements_prefix_in_builtins(self):
        """C32b: prefix `invoice+statements` måste finnas i BUILTIN_INCLUDES."""
        from app.services.settings_service import BUILTIN_INCLUDES
        self.assertIn("invoice+statements", BUILTIN_INCLUDES)

    def test_gmail_query_includes_invoice_statements_clause(self):
        """C32b: build_gmail_query måste lägga in `from:"invoice+statements"`
        i OR-clause:n. Citationstecken krävs eftersom Gmail bryter OR-parsning
        när `+`-tecknet förekommer i en oquoted `from:`-värde (verifierat i
        prod 2026-05-26: `(from:x OR from:invoice+statements)` → 0 träffar;
        `(from:x OR from:"invoice+statements")` → korrekt antal)."""
        from app.services.settings_service import build_gmail_query
        q = build_gmail_query(self._settings(), done_label="Bezala-Klar")
        # Måste vara quoted för att överleva OR-clause:n.
        self.assertIn('from:"invoice+statements"', q)
        # UN-quoted version får INTE finnas direkt — Gmail bryter OR då.
        # Sök efter exakt `from:invoice+statements` (utan citat) som inte
        # följs av citattecken. Splittar på " och kollar att ingen del
        # innehåller bar `from:invoice+statements`.
        # Enklare: kontrollera att querysträngen INTE har varianten
        # `OR from:invoice+statements` eller `(from:invoice+statements`
        # utan citat. (Quoten ligger som `from:"invoice+statements"`.)
        self.assertNotIn(" from:invoice+statements ", " " + q + " ")
        self.assertNotIn("(from:invoice+statements ", q)
        self.assertNotIn("(from:invoice+statements)", q)

    def test_gmail_query_keeps_promo_exclude(self):
        """C32b/säkerhet: -category:promotions är kvar så marknadsmail
        inte fångas via det breddade filtret."""
        from app.services.settings_service import build_gmail_query
        q = build_gmail_query(self._settings(), done_label="Bezala-Klar")
        self.assertIn("-category:promotions", q)

    def test_gmail_query_does_not_broaden_to_all_stripe(self):
        """C32b/säkerhet: filtret breddar INTE till `from:stripe.com` — vi
        vill inte fånga all Stripe-trafik (kundnotiser, dispute-mail osv),
        bara faktura-aviseringar med `invoice+statements`-prefix."""
        from app.services.settings_service import build_gmail_query
        q = build_gmail_query(self._settings(), done_label="Bezala-Klar")
        # En bar `from:stripe.com` (utan local-part-prefix) får inte finnas.
        self.assertNotIn(" from:stripe.com", q)
        self.assertNotIn("(from:stripe.com", q)

    def test_plus_sender_gets_quoted_in_or_clause(self):
        """C32b regression: alla `from:`-värden som innehåller `+`-tecken
        måste citeras i OR-clause:n. Annars bryts Gmails OR-parsing och
        ANDRA villkor i samma OR-clause matchar 0 (root-cause för att
        Arlanda Express-mail föll bort efter C32b first-pass).
        """
        from app.services.settings_service import build_gmail_query
        s = self._settings()
        # Lägg till en user-include med `+` så vi täcker bägge banorna
        # (BUILTIN och user-includes).
        s.include_senders = ["receipts+sub@example.com"]
        q = build_gmail_query(s, done_label="Bezala-Klar")
        self.assertIn('from:"receipts+sub@example.com"', q)
        self.assertIn('from:"invoice+statements"', q)
        # Avsändare UTAN `+` ska INTE quotas (för att inte bryta befintliga
        # query-mönster i onödan).
        self.assertIn("from:noreply@arlandaexpress.se", q)
        self.assertNotIn('from:"noreply@arlandaexpress.se"', q)

    def test_no_redundant_invoice_statements_entries(self):
        """C32b: vi har konsoliderat till en enda bred entry. Tidigare
        fanns `invoice+statements@mail.anthropic.com` separat — den entryn
        är överflödig nu när bredare prefix-pattern täcker samma adresser
        plus Stripe/Lovable. Test fångar regression om någon lägger
        tillbaka den redundanta entryn."""
        from app.services.settings_service import BUILTIN_INCLUDES
        self.assertNotIn(
            "invoice+statements@mail.anthropic.com", BUILTIN_INCLUDES,
        )
        # Verifiera att bredare prefix däremot finns:
        self.assertIn("invoice+statements", BUILTIN_INCLUDES)


# ---------- Endpoint-integration ----------


class HtmlOnlySendersEndpointTest(unittest.TestCase):
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

        app_module.app.dependency_overrides[app_module.require_auth] = (
            fake_require_auth
        )
        cls.client = TestClient(app_module.app)
        cls.app_module = app_module
        cls.SessionLocal = SessionLocal
        from app.models import HtmlOnlySender, MaintenanceTask
        cls.HtmlOnlySender = HtmlOnlySender
        cls.MaintenanceTask = MaintenanceTask

    @classmethod
    def tearDownClass(cls):
        cls.app_module.app.dependency_overrides.clear()

    def setUp(self):
        with self.SessionLocal() as db:
            db.query(self.HtmlOnlySender).delete()
            db.query(self.MaintenanceTask).filter(
                self.MaintenanceTask.name == "seed_html_only_senders_v1",
            ).delete()
            db.commit()

    # ----- 12: GET tom -----
    def test_get_empty(self):
        r = self.client.get("/api/settings/html-only-senders")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"senders": []})

    # ----- 13: POST + GET hämtar tillagd rad -----
    def test_post_then_get(self):
        r = self.client.post(
            "/api/settings/html-only-senders",
            json={"sender_pattern": "skanetrafiken", "description": "tåg"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["sender_pattern"], "skanetrafiken")
        self.assertEqual(body["description"], "tåg")
        self.assertTrue(body["is_active"])
        self.assertFalse(body["already_exists"])

        r2 = self.client.get("/api/settings/html-only-senders")
        self.assertEqual(len(r2.json()["senders"]), 1)

    # ----- 14: POST utan pattern → 400 -----
    def test_post_empty_pattern_400(self):
        r = self.client.post(
            "/api/settings/html-only-senders",
            json={"sender_pattern": "", "description": "x"},
        )
        self.assertEqual(r.status_code, 400)

    # ----- 15: PATCH togglar -----
    def test_patch_toggles_active(self):
        r = self.client.post(
            "/api/settings/html-only-senders",
            json={"sender_pattern": "skanetrafiken"},
        )
        sid = r.json()["id"]
        r2 = self.client.patch(
            f"/api/settings/html-only-senders/{sid}",
            json={"is_active": False},
        )
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.json()["is_active"])
        r3 = self.client.patch(
            f"/api/settings/html-only-senders/{sid}",
            json={"is_active": True},
        )
        self.assertTrue(r3.json()["is_active"])

    # ----- 16: PATCH okänt id → 404 -----
    def test_patch_unknown_id_404(self):
        r = self.client.patch(
            "/api/settings/html-only-senders/999999",
            json={"is_active": False},
        )
        self.assertEqual(r.status_code, 404)

    # ----- 17: DELETE -----
    def test_delete_removes_row(self):
        r = self.client.post(
            "/api/settings/html-only-senders",
            json={"sender_pattern": "skanetrafiken"},
        )
        sid = r.json()["id"]
        r2 = self.client.delete(f"/api/settings/html-only-senders/{sid}")
        self.assertEqual(r2.status_code, 200)
        # Andra gången → 404
        r3 = self.client.delete(f"/api/settings/html-only-senders/{sid}")
        self.assertEqual(r3.status_code, 404)


if __name__ == "__main__":
    unittest.main()
