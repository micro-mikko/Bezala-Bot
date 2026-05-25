"""FAS 5.4 — kortmatchning. Tester för:
- receipt_matcher pure functions (score_match, find_matches, vendor)
- BezalaClient.list_missing_receipts + attach_file
- GET /api/bezala/missing-receipts endpoint
- GET /api/bezala/match-suggestions endpoint
- POST /api/messages/{id}/match-to-bezala endpoint
"""

from __future__ import annotations

import os
import unittest
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
    client._email = "u"
    client._password = "p"
    client._base_url = "https://mock.bezala/api"
    client._client = MagicMock()
    client._token = "fake"
    client._token_expires_at = 9e18
    return client


# ---------- Pure matcher-tester ----------


class ParseAmountFromDescriptionTest(unittest.TestCase):
    """Bezala bill_lines har amount=null — vi plockar från description
    ('... 28.54 EUR' i slutet)."""

    def _parse(self, desc):
        from app.main import _parse_amount_from_description
        return _parse_amount_from_description(desc)

    def test_standard_format(self):
        self.assertEqual(
            self._parse("MIKKO KEINONEN: SKANETRAFIKEN APP, KRISTIANSTAD, SE 28.54 EUR"),
            (28.54, "EUR"),
        )

    def test_swedish_comma_decimal(self):
        self.assertEqual(
            self._parse("MIKKO: MOOVY, HELSINKI, FI 10,69 EUR"),
            (10.69, "EUR"),
        )

    def test_ignores_country_code_before_amount(self):
        self.assertEqual(self._parse("SE 60.58 EUR"), (60.58, "EUR"))

    def test_other_currency_code(self):
        self.assertEqual(
            self._parse("NAMN: VENDOR, STOCKHOLM, SE 300.00 SEK"),
            (300.00, "SEK"),
        )

    def test_empty_string(self):
        self.assertEqual(self._parse(""), (None, None))

    def test_none_input(self):
        self.assertEqual(self._parse(None), (None, None))

    def test_no_amount_in_text(self):
        self.assertEqual(self._parse("text utan belopp eller valuta"), (None, None))

    def test_one_decimal_dot(self):
        """Lovable rapporterar 100.0 EUR (1 decimal). Måste matcha."""
        self.assertEqual(
            self._parse("MIKKO KEINONEN: LOVABLE, DOVER, US 100.0 EUR"),
            (100.0, "EUR"),
        )

    def test_one_decimal_finnair(self):
        """Finnair rapporterar 494.5 EUR (1 decimal). Måste matcha."""
        self.assertEqual(
            self._parse("MIKKO KEINONEN: FINNAIR O87UJ3J, VANTAA, FI 494.5 EUR"),
            (494.5, "EUR"),
        )

    def test_integer_no_decimals(self):
        """Heltal utan decimaler ('100 EUR') matchar — vissa vendors
        rapporterar runda belopp utan decimaler."""
        self.assertEqual(self._parse("VENDOR, NYC, US 100 EUR"), (100.0, "EUR"))

    def test_swedish_comma_one_decimal(self):
        """Svensk komma med 1 decimal."""
        self.assertEqual(self._parse("VENDOR, STOCKHOLM, SE 100,5 EUR"), (100.5, "EUR"))


class ScoreMatchTest(unittest.TestCase):
    def _missing(self, **over):
        base = {
            "amount": 112.95,
            "currency": "EUR",
            "date": "2026-04-14",
            "description": "CLAUDE.AI SUBSCRIPTION",
        }
        base.update(over)
        return base

    def _candidate(self, **over):
        base = {
            "amount": 112.95,
            "currency": "EUR",
            "receipt_date": "2026-04-14",
            "vendor": "Anthropic",
        }
        base.update(over)
        return base

    def test_perfect_match_high_score(self):
        from app.services.receipt_matcher import score_match
        s = score_match(self._missing(), self._candidate())
        # exact amount (50) + exact date (30) + vendor override claude→anthropic
        self.assertGreaterEqual(s["total"], 80)
        self.assertEqual(s["breakdown"]["amount"], 50)
        self.assertEqual(s["breakdown"]["date"], 30)
        self.assertGreater(s["breakdown"]["vendor"], 20)

    def test_amount_within_5pct(self):
        """C30b — graderad amount: 3.6% diff faller i 2-5%-bucketen → 25p
        (tidigare binärt 50p). Säkrar att near-matches inte längre kan
        slå exakta belopp i scoringen."""
        from app.services.receipt_matcher import score_match
        # 112.95 vs 117.00 → 3.6% diff → 2-5%-bucket → 25p
        s = score_match(self._missing(), self._candidate(amount=117.00))
        self.assertEqual(s["breakdown"]["amount"], 25)

    def test_amount_outside_5pct_no_bonus(self):
        from app.services.receipt_matcher import score_match
        s = score_match(self._missing(), self._candidate(amount=200.00))
        self.assertEqual(s["breakdown"]["amount"], 0)

    def test_date_distance_decay(self):
        from app.services.receipt_matcher import score_match
        # C26 buckets (max_days, score):
        # (0, 30), (1, 28), (3, 26), (7, 25), (14, 15), (30, 10), (60, 5)
        # Same date → 30 (exakt match — full pott)
        self.assertEqual(
            score_match(self._missing(), self._candidate())["breakdown"]["date"], 30,
        )
        # ±1 day → 28 (C26: tydligt lägre än exakt match)
        self.assertEqual(
            score_match(self._missing(), self._candidate(receipt_date="2026-04-15"))["breakdown"]["date"], 28,
        )
        # ±3 days → 26 (C26: 2-3-dagars-bucketen)
        self.assertEqual(
            score_match(self._missing(), self._candidate(receipt_date="2026-04-17"))["breakdown"]["date"], 26,
        )
        # 4-7 days → 25
        self.assertEqual(
            score_match(self._missing(), self._candidate(receipt_date="2026-04-20"))["breakdown"]["date"], 25,
        )
        # 8-14 days → 15
        self.assertEqual(
            score_match(self._missing(), self._candidate(receipt_date="2026-04-25"))["breakdown"]["date"], 15,
        )
        # 15-30 days → 10
        self.assertEqual(
            score_match(self._missing(), self._candidate(receipt_date="2026-05-08"))["breakdown"]["date"], 10,
        )
        # 31-60 days → 5
        self.assertEqual(
            score_match(self._missing(), self._candidate(receipt_date="2026-06-05"))["breakdown"]["date"], 5,
        )
        # >60 days → 0
        self.assertEqual(
            score_match(self._missing(), self._candidate(receipt_date="2026-07-15"))["breakdown"]["date"], 0,
        )

    # ---------- FAS 8.5a fix — dual-date matching ----------

    def test_dual_date_uses_receipt_date_when_matches(self):
        from app.services.receipt_matcher import score_match
        s = score_match(
            self._missing(),
            self._candidate(
                receipt_date="2026-04-14",
                received_at="2026-04-10T10:00:00+00:00",
            ),
        )
        self.assertEqual(s["breakdown"]["date"], 30)
        self.assertEqual(s["breakdown"]["date_matched_field"], "receipt_date")
        self.assertEqual(s["breakdown"]["date_days_off"], 0)

    def test_dual_date_prefers_receipt_date_even_when_received_at_is_closer(self):
        """FAS 5.12: receipt_date är källa-för-sanning. Även när received_at
        ligger närmare kortransaktionen ska receipt_date väljas — annars
        rapporterar Match Health fel matched_field.

        Finnair-case: card_trans 24 april, receipt_date 30 april (resedag),
        received_at 24 april (bokning). Gamla beteendet plockade received_at
        (0 dagar). Nu väljs receipt_date (6 dagar → 4-7d-bucket → 25p)."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            self._missing(date="2026-04-24"),
            self._candidate(
                receipt_date="2026-04-30",
                received_at="2026-04-24T18:42:00+00:00",
            ),
        )
        self.assertEqual(s["breakdown"]["date_matched_field"], "receipt_date")
        self.assertEqual(s["breakdown"]["date_days_off"], 6)
        self.assertEqual(s["breakdown"]["date"], 25)

    def test_dual_date_uses_receipt_date_diff_not_smaller_received_at(self):
        """FAS 5.12 regression: dual-date plockar inte längre fältet med
        minst diff. receipt_date 11 dagar bort vinner över received_at
        8 dagar bort — diff används bara för score-bucket, inte för
        fältval."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            self._missing(date="2026-04-14"),
            self._candidate(
                receipt_date="2026-04-25",
                received_at="2026-04-22T10:00:00+00:00",
            ),
        )
        self.assertEqual(s["breakdown"]["date_matched_field"], "receipt_date")
        self.assertEqual(s["breakdown"]["date_days_off"], 11)
        self.assertEqual(s["breakdown"]["date"], 15)

    def test_dual_date_neither_matches(self):
        """Båda fälten >60 dagar från bankraden → 0p och matched_field=None.
        Match algorithm 3.0 utökade fönstret till 60d, så testet använder
        65/64-dagars-diff för att verifiera över-gränsen."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            self._missing(date="2026-04-14"),
            self._candidate(
                receipt_date="2026-06-18",
                received_at="2026-06-17T10:00:00+00:00",
            ),
        )
        self.assertEqual(s["breakdown"]["date"], 0)
        self.assertIsNone(s["breakdown"]["date_matched_field"])

    def test_dual_date_received_at_null_falls_back(self):
        """Defensivt: received_at saknas → använd bara receipt_date."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            self._missing(),
            self._candidate(receipt_date="2026-04-14", received_at=None),
        )
        self.assertEqual(s["breakdown"]["date"], 30)
        self.assertEqual(s["breakdown"]["date_matched_field"], "receipt_date")

    def test_dual_date_both_null_returns_zero(self):
        from app.services.receipt_matcher import score_match
        s = score_match(
            self._missing(),
            self._candidate(receipt_date=None, received_at=None),
        )
        self.assertEqual(s["breakdown"]["date"], 0)
        self.assertIsNone(s["breakdown"]["date_matched_field"])
        self.assertIsNone(s["breakdown"]["date_days_off"])

    def test_prefers_receipt_date_when_available(self):
        """FAS 5.12: receipt_date är alltid källa-för-sanning när den finns.
        Även när received_at är exakt lika med kortdatumet ska matched_field
        peka på receipt_date — Match Health rapporterar då rätt diff."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            self._missing(date="2026-04-14"),
            self._candidate(
                receipt_date="2026-04-16",
                received_at="2026-04-14T10:00:00+00:00",
            ),
        )
        self.assertEqual(s["breakdown"]["date_matched_field"], "receipt_date")
        self.assertEqual(s["breakdown"]["date_days_off"], 2)

    def test_falls_back_to_received_at_when_receipt_date_is_null(self):
        """FAS 5.12: när PDF-parsern inte hittat receipt_date används
        received_at som fallback. matched_field ska signalera detta."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            self._missing(date="2026-04-14"),
            self._candidate(
                receipt_date=None,
                received_at="2026-04-15T08:00:00+00:00",
            ),
        )
        self.assertEqual(s["breakdown"]["date_matched_field"], "received_at")
        self.assertEqual(s["breakdown"]["date_days_off"], 1)
        # C26: ±1 dag ger 28 (tidigare 30 — exakt match särskiljs nu).
        self.assertEqual(s["breakdown"]["date"], 28)

    def test_moovy_case_uses_receipt_date_not_received_at(self):
        """FAS 5.12 konkret Match Health-case: Moovy-kvitto har
        receipt_date 2026-04-16 (parkeringsdag) men received_at 2026-04-17
        (mejlet kom dagen efter). Kortdebitering 2026-04-15.

        Gamla logiken plockade received_at (2 dagars diff) över
        receipt_date (1 dags diff) eftersom båda hade lika små diffar
        och tie-break gick på receipt_date. Men: även om received_at
        gett mindre diff ska receipt_date vinna — det är kvittots
        sanna datum, inte mejlets ankomsttid."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            {
                "amount": 12.50, "currency": "EUR", "date": "2026-04-15",
                "description": "MIKKO KEINONEN: MOOVY, HELSINKI, FI 12.50 EUR",
            },
            {
                "amount": 12.50, "currency": "EUR",
                "receipt_date": "2026-04-16",
                "received_at": "2026-04-17T07:23:00+00:00",
                "vendor": "Moovy",
            },
        )
        self.assertEqual(s["breakdown"]["date_matched_field"], "receipt_date")
        self.assertEqual(s["breakdown"]["date_days_off"], 1)

    def test_dual_date_strips_time_component(self):
        """received_at är full ISO-timestamp — jämförelse görs på datum-nivå
        så timme/minut inte räknas in."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            self._missing(date="2026-04-24"),
            self._candidate(
                receipt_date=None,
                received_at="2026-04-24T23:59:59+00:00",
            ),
        )
        self.assertEqual(s["breakdown"]["date"], 30)
        self.assertEqual(s["breakdown"]["date_days_off"], 0)

    def test_vendor_override_claude_anthropic(self):
        from app.services.receipt_matcher import vendor_similarity
        # Direct override
        sim = vendor_similarity("CLAUDE.AI SUBSCRIPTION", "Anthropic")
        self.assertGreaterEqual(sim, 0.9)

    def test_airport_lrs_no_false_alias(self):
        """Match algorithm 3.1 Bug #1: AIRPORT LRS är inte Arlanda Express.
        Tidigare override + alias gjorde att de matchade 95-100% — falskt
        positivt. Efter fix: ingen alias, ingen override → låg fuzzy-sim."""
        from app.services.receipt_matcher import (
            alias_match, vendor_similarity, _vendor_canonical,
        )
        # Ingen override-mapping
        self.assertIsNone(_vendor_canonical("airport lrs"))
        # Ingen alias-match
        self.assertFalse(alias_match("AIRPORT LRS", "Arlanda Express"))
        # Fuzzy-similarity ska vara låg (< 0.5) — olika brands
        sim = vendor_similarity("AIRPORT LRS", "Arlanda Express")
        self.assertLess(sim, 0.5)
        # Arlanda Express ska fortfarande matcha sig själv
        sim_self = vendor_similarity("ARLANDA EXPRESS BILJETT", "Arlanda Express")
        self.assertGreaterEqual(sim_self, 0.9)

    # ---------- Match algorithm 3.0 — utökad datum-tolerans ----------

    def test_date_tolerance_15_days_gets_15_points(self):
        """Bug 1: 15-30d ska ge 10 poäng. 15 dagar ligger i den bucketen.
        Tidigare gav 8+ dagar 0 poäng vilket dödade fördröjda
        kortdebiteringar (parkering, transit etc)."""
        from app.services.receipt_matcher import score_match
        # 15 dagar diff (datum-bucket: 15-30 → 10p)
        s = score_match(
            self._missing(date="2026-05-09"),
            self._candidate(receipt_date="2026-04-24"),
        )
        self.assertEqual(s["breakdown"]["date_days_off"], 15)
        self.assertEqual(s["breakdown"]["date"], 10)

    def test_date_tolerance_30_days_gets_10_points(self):
        """Bug 1: 30 dagar ska fortfarande ligga i 15-30d-bucketen → 10p."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            self._missing(date="2026-05-14"),
            self._candidate(receipt_date="2026-04-14"),
        )
        self.assertEqual(s["breakdown"]["date_days_off"], 30)
        self.assertEqual(s["breakdown"]["date"], 10)

    def test_date_tolerance_45_days_gets_5_points(self):
        """Bug 1: 31-60d ska ge 5p (tidigare 0p)."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            self._missing(date="2026-05-29"),
            self._candidate(receipt_date="2026-04-14"),
        )
        self.assertEqual(s["breakdown"]["date_days_off"], 45)
        self.assertEqual(s["breakdown"]["date"], 5)

    def test_date_tolerance_61_days_gets_zero(self):
        """Bug 1: >60 dagar ska ge 0p (övre gränsen)."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            self._missing(date="2026-06-15"),
            self._candidate(receipt_date="2026-04-14"),
        )
        self.assertEqual(s["breakdown"]["date_days_off"], 62)
        self.assertEqual(s["breakdown"]["date"], 0)

    def test_moovy_15_day_delayed_debit_reaches_threshold(self):
        """Bug 1 verifiering: MOOVY 73,49 EUR bankrad 2026-05-09 → Moovy
        kvitto 73,49 EUR 2026-04-24 (15 dagars skillnad). Tidigare 78p
        (under tröskel), nu med utökat datum-fönster ska bli ≥80."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            {
                "amount": 73.49, "currency": "EUR", "date": "2026-05-09",
                "description": "MIKKO KEINONEN: MOOVY, HELSINKI, FI 73.49 EUR",
            },
            {
                "amount": 73.49, "currency": "EUR",
                "receipt_date": "2026-04-24",
                "vendor": "Moovy",
            },
        )
        # 50 (amount) + 10 (15d) + 30 (alias/substring/override) = 90
        self.assertGreaterEqual(s["total"], 80)

    # ---------- Match algorithm 3.0 — vendor-aliasing ----------

    def test_vendor_alias_moovy_matches_finavia(self):
        """Bug 2: bankrad 'MOOVY' ska matcha kvitto med vendor 'Finavia'
        via VENDOR_ALIASES-mappningen."""
        from app.services.receipt_matcher import alias_match, vendor_similarity
        self.assertTrue(alias_match(
            "MIKKO KEINONEN: MOOVY, HELSINKI, FI 37.49 EUR",
            "Finavia",
        ))
        sim = vendor_similarity(
            "MIKKO KEINONEN: MOOVY, HELSINKI, FI 37.49 EUR",
            "Finavia",
        )
        self.assertEqual(sim, 1.0)

    def test_vendor_alias_lovable_via_sender(self):
        """Bug 2: alias kan triggas via sender-fältet om vendor-fältet
        inte bär signalen (Lovable-mejl från no-reply@lovable.dev)."""
        from app.services.receipt_matcher import alias_match
        self.assertTrue(alias_match(
            "MIKKO KEINONEN: LOVABLE, DOVER, US 100.00 EUR",
            "no-reply",
            "no-reply@lovable.dev",
        ))

    def test_vendor_alias_apple_does_NOT_match_anthropic(self):
        """Bug 2 negativ test: APPLE.COM/BILL ska INTE matcha
        Anthropic-vendor — endast aliaserna i listan."""
        from app.services.receipt_matcher import alias_match
        self.assertFalse(alias_match(
            "MIKKO KEINONEN: APPLE.COM/BILL, CUPERTINO, US 9.99 EUR",
            "Anthropic",
        ))

    def test_vendor_alias_no_match_when_key_absent(self):
        """Bug 2: alias triggar inte om bankraden inte innehåller någon
        VENDOR_ALIASES-nyckel."""
        from app.services.receipt_matcher import alias_match
        self.assertFalse(alias_match(
            "MIKKO KEINONEN: RANDOM_VENDOR, NYC, US 50.00 EUR",
            "lovable.dev",
        ))

    def test_score_match_uses_alias_via_sender(self):
        """End-to-end: Lovable-bankrad + processed_message med
        sender=lovable.dev men vendor=null → vendor-bonus 30p via alias."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            {
                "amount": 100.0, "currency": "EUR", "date": "2026-04-25",
                "description": "MIKKO KEINONEN: LOVABLE, DOVER, US 100.00 EUR",
            },
            {
                "amount": 100.0, "currency": "EUR",
                "receipt_date": "2026-04-25",
                "vendor": None,
                "sender": "billing@lovable.dev",
            },
        )
        self.assertEqual(s["breakdown"]["vendor"], 30)

    # ---------- FAS 5.12 — regressionsskydd för alias-träffar ----------

    def test_regression_alias_match_total_110_unchanged(self):
        """FAS 5.12: ändrad date-field-prioritering ska inte påverka
        score-summan. Exact amount + exact date + alias = 50+30+30 = 110."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            {
                "amount": 100.0, "currency": "EUR", "date": "2026-04-25",
                "description": "MIKKO KEINONEN: LOVABLE, DOVER, US 100.00 EUR",
            },
            {
                "amount": 100.0, "currency": "EUR",
                "receipt_date": "2026-04-25",
                "received_at": "2026-04-26T09:00:00+00:00",
                "vendor": "Lovable",
                "sender": "billing@lovable.dev",
            },
        )
        self.assertEqual(s["total"], 110)
        self.assertEqual(s["breakdown"]["date_matched_field"], "receipt_date")

    def test_regression_alias_match_total_95_unchanged(self):
        """FAS 5.12: alias-träff + amount-bonus + 4-7d-bucket (25p) =
        50+25+30 = 105. Tidigare gav identiska siffror — testet säkrar
        att FAS 5.12 inte gör received_at-baserade nedgraderingar för
        de pairs som algo 3.1 redan accepterat."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            {
                "amount": 100.0, "currency": "EUR", "date": "2026-04-25",
                "description": "MIKKO KEINONEN: LOVABLE, DOVER, US 100.00 EUR",
            },
            {
                "amount": 100.0, "currency": "EUR",
                "receipt_date": "2026-04-30",
                "received_at": "2026-04-25T09:00:00+00:00",
                "vendor": "Lovable",
            },
        )
        # receipt_date vinner alltid (5 dagar) — received_at exakt 0 dagar
        # ignoreras enligt FAS 5.12.
        self.assertEqual(s["breakdown"]["date_matched_field"], "receipt_date")
        self.assertEqual(s["breakdown"]["date"], 25)
        self.assertGreaterEqual(s["total"], 95)

    def test_regression_moovy_alias_still_reaches_threshold(self):
        """FAS 5.12: Moovy bug 1-fixet (15 dagars fördröjd debitering)
        ska fortfarande nå tröskel. Bankrad 2026-05-09, kvitto receipt_date
        2026-04-24, ingen received_at. Total ≥80 (50 amount + 10 date + 30
        alias)."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            {
                "amount": 73.49, "currency": "EUR", "date": "2026-05-09",
                "description": "MIKKO KEINONEN: MOOVY, HELSINKI, FI 73.49 EUR",
            },
            {
                "amount": 73.49, "currency": "EUR",
                "receipt_date": "2026-04-24",
                "received_at": "2026-04-25T08:00:00+00:00",
                "vendor": "Finavia",
            },
        )
        self.assertEqual(s["breakdown"]["date_matched_field"], "receipt_date")
        self.assertGreaterEqual(s["total"], 80)

    # ---------- Match algorithm 3.0 — currency-check ----------

    def test_amount_blocked_when_currencies_differ(self):
        """Verifiering: LOVABLE 100 EUR vs Arlanda Express 100 SEK ska
        INTE få amount-bonus utan konvertering — currency-mismatch
        blockerar direkt match."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            {
                "amount": 100.0, "currency": "EUR", "date": "2026-04-25",
                "description": "MIKKO KEINONEN: LOVABLE, DOVER, US 100.00 EUR",
            },
            {
                "amount": 100.0, "currency": "SEK",
                "receipt_date": "2026-04-25",
                "vendor": "Arlanda Express",
            },
        )
        self.assertEqual(s["breakdown"]["amount"], 0)

    def test_amount_matches_when_currencies_match(self):
        """Regression: samma valuta + samma belopp → 50p (oförändrat)."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            self._missing(),
            self._candidate(),
        )
        self.assertEqual(s["breakdown"]["amount"], 50)

    def test_amount_matches_when_currency_missing_on_either_side(self):
        """Defensiv: om någon valuta saknas, behåll gamla beteendet
        (matcha på belopp). Backwards compat med rader som saknar valuta."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            {"amount": 100.0, "date": "2026-04-25", "description": "X"},
            {"amount": 100.0, "currency": "EUR", "receipt_date": "2026-04-25",
             "vendor": "X"},
        )
        self.assertEqual(s["breakdown"]["amount"], 50)

    # ---------- Match algorithm 3.1 — false-positive-skydd ----------

    def test_cross_currency_tight_tolerance(self):
        """Bug #2: cross-currency-konvertering ska kräva ±2% (inte ±10%).
        APPLE.COM/BILL 22.99 EUR vs Flytoget 268 NOK (~25 EUR efter konv,
        ~9% diff) → inga amount-poäng, total ska INTE nå 50."""
        from app.services.receipt_matcher import score_match

        def rate_provider(date_str, from_c, to_c):
            # NOK→EUR ≈ 0.093 → 268 NOK = 24.92 EUR (diff 8.4% från 22.99)
            if (from_c, to_c) == ("NOK", "EUR"):
                return 0.093
            return None

        s = score_match(
            {
                "amount": 22.99, "currency": "EUR", "date": "2026-04-28",
                "description": "MIKKO KEINONEN: APPLE.COM/BILL, CUPERTINO, US 22.99 EUR",
            },
            {
                "amount": 268.0, "currency": "NOK",
                "receipt_date": "2026-04-28",
                "vendor": "Flytoget",
            },
            rate_provider=rate_provider,
        )
        self.assertEqual(s["breakdown"]["amount"], 0)
        self.assertNotIn("conversion", s)

    def test_cross_currency_within_2pct_still_matches(self):
        """Bug #2 motsats: cross-currency MED <2% diff ska fortfarande
        ge 40p (regressionssäkring för true positives)."""
        from app.services.receipt_matcher import score_match

        def rate_provider(date_str, from_c, to_c):
            # SEK→EUR = 0.0951 → 300 SEK = 28.53 EUR, diff 0.04% från 28.54
            if (from_c, to_c) == ("SEK", "EUR"):
                return 0.0951
            return None

        s = score_match(
            {
                "amount": 28.54, "currency": "EUR", "date": "2026-04-22",
                "description": "SKANETRAFIKEN APP",
            },
            {
                "amount": 300.0, "currency": "SEK",
                "receipt_date": "2026-04-22",
                "vendor": "Skånetrafiken",
            },
            rate_provider=rate_provider,
        )
        self.assertEqual(s["breakdown"]["amount"], 40)

    def test_skanetrafiken_eur_sek_amount_mismatch_stays_low(self):
        """Bug #3: SKANETRAFIKEN 23.14 EUR vs Skånetrafiken 300 SEK är ett
        riktigt data-mismatch (300 SEK är ett prepaid-kortpåfyllning, inte
        själva köpet). Trots perfekt vendor-alias ska score stanna under
        tröskeln 80 när belopp skiljer >30% efter konvertering."""
        from app.services.receipt_matcher import score_match

        def rate_provider(date_str, from_c, to_c):
            # 300 SEK → ca 26.30 EUR. Diff från 23.14 ≈ 13.6%.
            if (from_c, to_c) == ("SEK", "EUR"):
                return 0.0876
            return None

        s = score_match(
            {
                "amount": 23.14, "currency": "EUR", "date": "2026-04-22",
                "description": "SKANETRAFIKEN APP",
            },
            {
                "amount": 300.0, "currency": "SEK",
                "receipt_date": "2026-04-22",
                "vendor": "Skånetrafiken",
            },
            rate_provider=rate_provider,
        )
        # vendor 30 (alias 1.0) + date 30 + amount 0 = 60. Under 80.
        self.assertEqual(s["breakdown"]["amount"], 0)
        self.assertLess(s["total"], 80)

    def test_old_receipt_rejected_over_365_days(self):
        """Bug #4: kvitto > 365d från korttransaktion ska få total=0
        oavsett andra signaler. FINNAIR 494,50 EUR 2026-04-28 vs Finnair
        489,76 EUR 2021-12-15 (1595d) ska inte vara en match."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            {
                "amount": 494.50, "currency": "EUR", "date": "2026-04-28",
                "description": "FINNAIR O87UJ3J",
            },
            {
                "amount": 489.76, "currency": "EUR",
                "receipt_date": "2021-12-15",
                "vendor": "Finnair",
            },
        )
        self.assertEqual(s["total"], 0)
        self.assertEqual(s.get("rejected_reason"), "date_too_far")

    def test_under_365_days_not_rejected(self):
        """Bug #4 regression: 364 dagar gammalt kvitto ska INTE killas."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            {
                "amount": 100.0, "currency": "EUR", "date": "2026-04-28",
                "description": "ANTHROPIC",
            },
            {
                "amount": 100.0, "currency": "EUR",
                "receipt_date": "2025-04-29",  # 364 dagar
                "vendor": "Anthropic",
            },
        )
        self.assertNotEqual(s["total"], 0)
        self.assertNotIn("rejected_reason", s)

    def test_vendor_floor_caps_score_at_49(self):
        """Bug #5: HERTZ SVERIGE 108.92 EUR vs Anthropic 112.95 EUR har
        liknande belopp + datum men noll vendor-signal (sim < 20%).
        Total ska kapas vid 49 (under display-tröskeln 50)."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            {
                "amount": 108.92, "currency": "EUR", "date": "2026-04-14",
                "description": (
                    "MIKKO KEINONEN: HERTZ SVERIGE, STOCKHOLM, "
                    "SE 108.92 EUR"
                ),
            },
            {
                "amount": 112.95, "currency": "EUR",
                "receipt_date": "2026-04-20",
                "vendor": "Anthropic",
            },
        )
        self.assertLessEqual(s["total"], 49)

    def test_vendor_floor_does_not_affect_alias_matches(self):
        """Bug #5 regression: MOOVY → Finavia via alias (sim=1.0) ska
        INTE kapas av vendor-floor."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            {
                "amount": 73.49, "currency": "EUR", "date": "2026-05-09",
                "description": "MIKKO KEINONEN: MOOVY, HELSINKI, FI 73.49 EUR",
            },
            {
                "amount": 73.49, "currency": "EUR",
                "receipt_date": "2026-04-24",
                "vendor": "Finavia",
            },
        )
        # 50 + 10 + 30 = 90 (alias triggar, ingen floor)
        self.assertGreaterEqual(s["total"], 80)

    def test_find_matches_filters_below_threshold(self):
        from app.services.receipt_matcher import find_matches, MIN_DISPLAY_SCORE
        candidates = [
            {  # Bra match
                "id": 1, "amount": 112.95, "receipt_date": "2026-04-14",
                "vendor": "Anthropic",
            },
            {  # Helt fel
                "id": 2, "amount": 9999, "receipt_date": "2025-01-01",
                "vendor": "Random Inc",
            },
        ]
        matches = find_matches(self._missing(), candidates)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["message"]["id"], 1)
        self.assertGreaterEqual(matches[0]["score"], MIN_DISPLAY_SCORE)


# ---------- C30b — graderad amount-bonus ----------


class GradedAmountScoreTest(unittest.TestCase):
    """C30b — _amount_score-trappan: exakt belopp slår nära-belopp.

    Bakgrund: bekräftat i prod 2026-05-22 visade samma 554,50-kvitto
    100% confidence mot BÅDE O7CCY63 (534,49 EUR) och O9VAZGJ (554,50)
    eftersom ±5%-toleransen gav binär 50p amount-bonus. Nu graderat
    enligt 50/40/25/0 så att 0% diff > 0.5–2% diff > 2–5% diff."""

    def test_amount_score_exact_match(self):
        from app.services.receipt_matcher import _amount_score
        self.assertEqual(_amount_score(100.0, 100.0), 50)

    def test_amount_score_rounding_band_below_half_pct(self):
        from app.services.receipt_matcher import _amount_score
        # 0.4% diff → exakt-bucket
        self.assertEqual(_amount_score(100.0, 100.4), 50)
        self.assertEqual(_amount_score(100.0, 99.6), 50)

    def test_amount_score_one_to_two_pct_band(self):
        from app.services.receipt_matcher import _amount_score
        # 1.5% diff → 40p
        self.assertEqual(_amount_score(100.0, 101.5), 40)
        self.assertEqual(_amount_score(100.0, 98.5), 40)
        # gränsen 2.0% själv → 40p
        self.assertEqual(_amount_score(100.0, 102.0), 40)

    def test_amount_score_two_to_five_pct_band(self):
        from app.services.receipt_matcher import _amount_score
        # 3.5% diff → 25p
        self.assertEqual(_amount_score(100.0, 103.5), 25)
        # Finnair-fallet: 534.49 vs 554.50 = 3.74% → 2-5%-bucket
        self.assertEqual(_amount_score(534.49, 554.50), 25)
        # gränsen 5.0% själv → 25p
        self.assertEqual(_amount_score(100.0, 105.0), 25)

    def test_amount_score_outside_5pct_zero(self):
        """Yttre gränsen oförändrad — >5% diff ger 0p (currency-fallback
        / cross-currency-väg får hantera dessa fall via konvertering)."""
        from app.services.receipt_matcher import _amount_score
        self.assertEqual(_amount_score(100.0, 106.0), 0)
        self.assertEqual(_amount_score(100.0, 200.0), 0)

    def test_amount_score_none_or_zero(self):
        from app.services.receipt_matcher import _amount_score
        self.assertEqual(_amount_score(None, 100.0), 0)
        self.assertEqual(_amount_score(100.0, None), 0)
        self.assertEqual(_amount_score(0, 100.0), 0)


class GradedAmountRankingTest(unittest.TestCase):
    """C30b — exakt-belopp ska ranka över near-match för samma vendor
    även när near-matchen har bättre datum (Finnair-fall i prod)."""

    def test_exact_beats_near_amount_finnair_flight_date_8_to_14d(self):
        """Realistiskt Finnair-fall: bankrad 2026-05-20 / 534,49 EUR.
        Kvitto A = 554,50 EUR (3,74% diff), receipt_date 2026-05-19 (1d).
        Kvitto B = 534,49 EUR (exakt), receipt_date 2026-05-30 (10d off,
        Finnair-eticket daterar avresedag). B SKA vinna trots sämre
        datum eftersom beloppet är exakt."""
        from app.services.receipt_matcher import find_matches
        missing = {
            "amount": 534.49, "currency": "EUR", "date": "2026-05-20",
            "description": "MIKKO KEINONEN: FINNAIR O7CCY63, VANTAA, FI 534.49 EUR",
        }
        near = {
            "id": 1, "amount": 554.50, "currency": "EUR",
            "receipt_date": "2026-05-19", "vendor": "Finnair",
        }
        exact = {
            "id": 2, "amount": 534.49, "currency": "EUR",
            "receipt_date": "2026-05-30", "vendor": "Finnair",
        }
        ranked = find_matches(missing, [near, exact])
        self.assertEqual(ranked[0]["message"]["id"], 2)
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])

    def test_exact_beats_near_amount_finnair_flight_date_15_to_30d(self):
        """Som ovan men receipt_date 25d bort (15-30d bucket → 10p).
        Exakt total 50+30+10 = 90, near total 25+30+28 = 83."""
        from app.services.receipt_matcher import find_matches
        missing = {
            "amount": 534.49, "currency": "EUR", "date": "2026-05-20",
            "description": "MIKKO KEINONEN: FINNAIR O7CCY63, VANTAA, FI 534.49 EUR",
        }
        near = {
            "id": 1, "amount": 554.50, "currency": "EUR",
            "receipt_date": "2026-05-19", "vendor": "Finnair",
        }
        exact = {
            "id": 2, "amount": 534.49, "currency": "EUR",
            "receipt_date": "2026-06-14", "vendor": "Finnair",
        }
        ranked = find_matches(missing, [near, exact])
        self.assertEqual(ranked[0]["message"]["id"], 2)

    def test_exact_amount_beats_near_amount_same_date_vendor(self):
        """Sanity: när datum + vendor är lika ska exakt belopp vinna
        med stor marginal (50 vs 25)."""
        from app.services.receipt_matcher import find_matches
        missing = {
            "amount": 554.50, "currency": "EUR", "date": "2026-05-13",
            "description": "FINNAIR",
        }
        exact = {
            "id": 1, "amount": 554.50, "currency": "EUR",
            "receipt_date": "2026-05-13", "vendor": "Finnair",
        }
        near = {
            "id": 2, "amount": 534.49, "currency": "EUR",
            "receipt_date": "2026-05-13", "vendor": "Finnair",
        }
        ranked = find_matches(missing, [near, exact])
        self.assertEqual(ranked[0]["message"]["id"], 1)
        self.assertEqual(ranked[0]["score"], 110)  # 50 + 30 + 30
        # 2-5%-bucket: 25 + 30 + 30 = 85
        self.assertEqual(ranked[1]["score"], 85)


class GradedAmountCrossCurrencyTest(unittest.TestCase):
    """C30b — cross-currency-vägen ska vara helt opåverkad av graderingen.

    Same-currency near-matches utanför 5% → 0p (oförändrat). Cross-currency
    går igenom _amount_matches_via_conversion (binär 40p/0p med ±2 % tight).
    """

    def test_skanetrafiken_sek_vs_eur_still_finds_match_via_conversion(self):
        """Regression: Skånetrafiken-flödet ska fortfarande matcha
        SEK-kvitto mot EUR-bankrad via ECB-konvertering, oförändrat."""
        from app.services.receipt_matcher import score_match

        def rate_provider(date_str, from_c, to_c):
            if (from_c, to_c) == ("SEK", "EUR"):
                return 0.0951
            return None

        s = score_match(
            {
                "amount": 28.54, "currency": "EUR", "date": "2026-04-22",
                "description": "SKANETRAFIKEN APP",
            },
            {
                "amount": 300.0, "currency": "SEK",
                "receipt_date": "2026-04-22",
                "vendor": "Skånetrafiken",
            },
            rate_provider=rate_provider,
        )
        self.assertEqual(s["breakdown"]["amount"], 40)
        self.assertIn("conversion", s)

    def test_cross_currency_zero_amount_but_vendor_date_still_score(self):
        """Cross-currency utan rate_provider → 0p på amount, men
        vendor+datum ska fortfarande ge en icke-noll total så kandidaten
        kan synas i UI:t (manuell match-väg)."""
        from app.services.receipt_matcher import score_match
        s = score_match(
            {
                "amount": 23.14, "currency": "EUR", "date": "2026-05-08",
                "description": "SKANETRAFIKEN APP",
            },
            {
                "amount": 245.0, "currency": "SEK",
                "receipt_date": "2026-05-08",
                "vendor": "Skånetrafiken",
            },
        )
        self.assertEqual(s["breakdown"]["amount"], 0)
        # vendor (alias 30) + date (exakt 30) = 60
        self.assertGreater(s["total"], 0)
        self.assertEqual(s["breakdown"]["vendor"], 30)
        self.assertEqual(s["breakdown"]["date"], 30)

    def test_finnair_large_date_gap_with_exact_amount_still_scores(self):
        """Finnair flygdatum 6d bort + exakt belopp ska fortfarande
        ranka högt (regression mot test_dual_date_prefers_receipt_date)."""
        from app.services.receipt_matcher import score_match, MIN_DISPLAY_SCORE
        s = score_match(
            {
                "amount": 366.32, "currency": "EUR", "date": "2026-04-24",
                "description": "FINNAIR O87UJ3J",
            },
            {
                "amount": 366.32, "currency": "EUR",
                "receipt_date": "2026-04-30",
                "vendor": "Finnair",
            },
        )
        # 50 (exakt) + 25 (4-7d) + 30 (alias) = 105
        self.assertEqual(s["breakdown"]["amount"], 50)
        self.assertGreaterEqual(s["total"], MIN_DISPLAY_SCORE)


class MaxTotalScoreConstantTest(unittest.TestCase):
    """C30b — MAX_TOTAL_SCORE måste hållas i synk med vikterna; frontend
    hårdkodar 110 i MatchCandidates.jsx + OtherReceiptsList.jsx."""

    def test_max_total_score_equals_sum_of_weights(self):
        from app.services.receipt_matcher import (
            AMOUNT_BONUS, MAX_TOTAL_SCORE, VENDOR_BONUS_MAX,
        )
        # Date max är 30p (DATE_BUCKETS[0])
        self.assertEqual(MAX_TOTAL_SCORE, AMOUNT_BONUS + 30 + VENDOR_BONUS_MAX)
        self.assertEqual(MAX_TOTAL_SCORE, 110)


# ---------- BezalaClient-tester ----------


class BezalaMissingReceiptsClientTest(unittest.TestCase):
    def test_list_missing_receipts_parses_array(self):
        client = _make_client()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.text = "[]"
        rows = [{"id": 1, "description": "X", "amount": 10, "date": "2026-04-14"}]
        resp.json = MagicMock(return_value=rows)

        def fake_request(method, url, **kwargs):
            return resp
        client._client.request = fake_request

        result = client.list_missing_receipts()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], 1)

    def test_attach_file_sends_bill_line_id_and_draft(self):
        """attach_file replikerar UI:s 'Koppla till existerande':
        POST /attachments med file + draft=1 + bill_line_id (+ optional
        description)."""
        client = _make_client()
        captured = {}
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"id": 999}'
        resp.json = MagicMock(return_value={"id": 999})

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["files"] = kwargs.get("files")
            captured["data"] = kwargs.get("data")
            return resp
        client._client.request = fake_request

        att = client.attach_file(2163467, "kvitto.pdf", PDF_BYTES)
        self.assertEqual(att.attachment_id, "999")
        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith("/attachments"))
        # FAS 5.28 (C21) — state="unapproved" skickas redan i CREATE
        # så draften aldrig hamnar i "Väntar på andras attestering"
        # som default när PUT:en till state-fältet returnerar 500.
        self.assertEqual(captured["data"], {
            "draft": "1",
            "state": "unapproved",
            "bill_line_id": "2163467",
        })
        self.assertIn("file", captured["files"])

    def test_attach_file_includes_description_when_given(self):
        """När description skickas med bifogas den i multipart-formen."""
        client = _make_client()
        captured = {}
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"id": 5}'
        resp.json = MagicMock(return_value={"id": 5})

        def fake_request(method, url, **kwargs):
            captured["data"] = kwargs.get("data")
            return resp
        client._client.request = fake_request

        client.attach_file(
            2163467, "kvitto.pdf", PDF_BYTES,
            description="20260414 Anthropic API",
        )
        self.assertEqual(captured["data"], {
            "draft": "1",
            "state": "unapproved",
            "bill_line_id": "2163467",
            "description": "20260414 Anthropic API",
        })

    def test_attach_file_rejects_invalid_pdf(self):
        from app.services.bezala_client import BezalaError
        client = _make_client()
        with self.assertRaises(BezalaError):
            client.attach_file(123, "x.pdf", b"not a pdf")

    def test_attach_file_rejects_missing_bill_line_id(self):
        from app.services.bezala_client import BezalaError
        client = _make_client()
        with self.assertRaises(BezalaError):
            client.attach_file("", "kvitto.pdf", PDF_BYTES)

    def test_attach_file_with_metadata_puts_transaction_update(self):
        """FAS 5.24 — när date + credit_account_id + vat_lines skickas in
        så följs POST /attachments upp av PUT /transactions/{tx_id}."""
        client = _make_client()
        calls: list[dict] = []

        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.headers = {"content-type": "application/json"}
        post_resp.text = '{"id": 111, "transaction_id": 222}'
        post_resp.json = MagicMock(return_value={"id": 111, "transaction_id": 222})

        put_resp = MagicMock()
        put_resp.status_code = 200
        put_resp.headers = {"content-type": "application/json"}
        put_resp.text = '{"id": 222}'
        put_resp.json = MagicMock(return_value={"id": 222})

        def fake_request(method, url, **kwargs):
            calls.append({
                "method": method, "url": url,
                "files": kwargs.get("files"),
                "data": kwargs.get("data"),
                "json": kwargs.get("json"),
            })
            if method == "POST":
                return post_resp
            return put_resp

        client._client.request = fake_request

        att = client.attach_file(
            42, "kvitto.pdf", PDF_BYTES,
            description="My description",
            date="2026-05-04",
            credit_account_id=67100,
            vat_lines_attributes=[{
                "taxable": "23.22",
                "tax_percentage": "0.24",
                "currency": "EUR",
                "expense_account_id": 67100,
                "vat_code_id": 864,
            }],
        )

        # Returnerar transaction_id (inte attachment_id) när vi fick en
        self.assertEqual(att.attachment_id, "222")

        # POST + PUT
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertTrue(calls[0]["url"].endswith("/attachments"))
        self.assertEqual(calls[0]["data"]["bill_line_id"], "42")
        self.assertEqual(calls[0]["data"]["draft"], "1")
        self.assertEqual(calls[0]["data"]["state"], "unapproved")
        self.assertEqual(calls[0]["data"]["description"], "My description")
        self.assertEqual(calls[1]["method"], "PUT")
        self.assertTrue(calls[1]["url"].endswith("/transactions/222"))
        body = calls[1]["json"]["transaction"]
        self.assertEqual(body["date"], "2026-05-04")
        self.assertEqual(body["credit_account_id"], 67100)
        self.assertEqual(body["description"], "My description")
        self.assertEqual(body["vat_lines_attributes"][0]["taxable"], "23.22")

    def test_attach_file_put_payload_includes_bill_id(self):
        """FAS 5.27 (C19 punkt C) — efter POST /attachments med bill_line_id
        i form-datan visade Bezalas pre_flight på live-tx 5005475 att
        transaktionen ändå hade `connected_to_bill_line: false` och
        `bill_id: null`. Form-datan räcker uppenbarligen inte för Bezala
        för att aktivera kortrad-kopplingen. attach_file PUT:ar därför
        bill_id explicit i transaction-payloaden så att kortrad-kopplingen
        verkligen blir aktiv (= 💳-ikonen blir grön i UI:t)."""
        client = _make_client()
        calls: list[dict] = []

        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.headers = {"content-type": "application/json"}
        post_resp.text = '{"id": 111, "transaction_id": 5005475}'
        post_resp.json = MagicMock(
            return_value={"id": 111, "transaction_id": 5005475},
        )

        put_resp = MagicMock()
        put_resp.status_code = 200
        put_resp.headers = {"content-type": "application/json"}
        put_resp.text = '{"id": 5005475}'
        put_resp.json = MagicMock(return_value={"id": 5005475})

        def fake_request(method, url, **kwargs):
            calls.append({"method": method, "url": url, "json": kwargs.get("json")})
            return post_resp if method == "POST" else put_resp
        client._client.request = fake_request

        client.attach_file(
            2163467, "kvitto.pdf", PDF_BYTES,
            description="Anthropic",
            date="2026-04-14",
            credit_account_id=67100,
            vat_lines_attributes=[{
                "taxable": "112.95", "tax_percentage": "0.00",
                "currency": "EUR", "expense_account_id": 67100,
            }],
        )

        put_calls = [c for c in calls if c["method"] == "PUT"]
        self.assertEqual(len(put_calls), 1)
        body = put_calls[0]["json"]["transaction"]
        # bill_id måste sättas till bill_line_id (i strängform efter
        # update_transaction:s passthrough) så Bezala får signal att
        # transaktionen ska kopplas till kortraden.
        self.assertIn("bill_id", body, "bill_id måste finnas i PUT-payloaden")
        self.assertEqual(str(body["bill_id"]), "2163467")

    def test_attach_file_extra_fields_cannot_override_bill_id(self):
        """Caller kan inte överstyra bill_id via extra_fields — vi
        injicerar alltid bill_line_id-input som bill_id sist."""
        client = _make_client()
        calls: list[dict] = []

        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.headers = {"content-type": "application/json"}
        post_resp.text = '{"id": 111, "transaction_id": 5005475}'
        post_resp.json = MagicMock(
            return_value={"id": 111, "transaction_id": 5005475},
        )

        put_resp = MagicMock()
        put_resp.status_code = 200
        put_resp.headers = {"content-type": "application/json"}
        put_resp.text = "{}"
        put_resp.json = MagicMock(return_value={})

        def fake_request(method, url, **kwargs):
            calls.append({"method": method, "url": url, "json": kwargs.get("json")})
            return post_resp if method == "POST" else put_resp
        client._client.request = fake_request

        client.attach_file(
            2163467, "kvitto.pdf", PDF_BYTES,
            description="x",
            date="2026-04-14",
            credit_account_id=67100,
            vat_lines_attributes=[{
                "taxable": "1.00", "tax_percentage": "0.0",
                "currency": "EUR", "expense_account_id": 67100,
            }],
            extra_fields={"bill_id": "999999"},  # försök överstyra
        )

        put_body = [c for c in calls if c["method"] == "PUT"][0]["json"]
        self.assertEqual(str(put_body["transaction"]["bill_id"]), "2163467")

    def test_match_to_bezala_payload_includes_state_unapproved(self):
        """FAS 5.27 — PUT /transactions måste innehålla state='unapproved'
        så att Bezala håller kvar drafter i 'Utkast' i stället för att
        promotade dem till 'Väntar på andras attestering'.

        C18-diagnostiken (GET /transactions/5005475) bekräftade att rätt
        fältnamn är `state` och rätt värde för Utkast är `"unapproved"` —
        INTE `draft`, INTE `status`, INTE `is_draft`. C16/C17:s fyra-fält-
        gissning fungerade aldrig; det fyrdubblade bara payloaden utan
        att träffa rätt fält."""
        client = _make_client()
        calls: list[dict] = []

        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.headers = {"content-type": "application/json"}
        post_resp.text = '{"id": 11, "transaction_id": 22}'
        post_resp.json = MagicMock(return_value={"id": 11, "transaction_id": 22})

        put_resp = MagicMock()
        put_resp.status_code = 200
        put_resp.headers = {"content-type": "application/json"}
        put_resp.text = '{"id": 22, "draft": true}'
        put_resp.json = MagicMock(return_value={"id": 22, "draft": True})

        def fake_request(method, url, **kwargs):
            calls.append({"method": method, "url": url, "json": kwargs.get("json")})
            return post_resp if method == "POST" else put_resp

        client._client.request = fake_request

        client.attach_file(
            42, "kvitto.pdf", PDF_BYTES,
            description="Skånetrafiken",
            date="2026-05-05",
            credit_account_id=67100,
            vat_lines_attributes=[{
                "taxable": "23.22",
                "tax_percentage": "0.24",
                "currency": "EUR",
                "expense_account_id": 67100,
            }],
        )

        put_calls = [c for c in calls if c["method"] == "PUT"]
        self.assertEqual(len(put_calls), 1, "förväntar exakt en PUT /transactions")
        body = put_calls[0]["json"]["transaction"]
        self.assertEqual(
            body.get("state"), "unapproved",
            "PUT-payloaden måste skicka state='unapproved' så draften "
            "ligger kvar i Utkast",
        )

    def test_match_to_bezala_payload_does_not_submit(self):
        """FAS 5.25 — defensivt: PUT-payloaden får INTE innehålla några
        fält som Bezala kan tolka som en submit-signal (status='submitted',
        submitted=True, state='submitted', action='submit', is_draft=False).
        Bezala tillåter inte återkallning — bara attestanten kan avvisa."""
        client = _make_client()
        calls: list[dict] = []

        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.headers = {"content-type": "application/json"}
        post_resp.text = '{"id": 11, "transaction_id": 22}'
        post_resp.json = MagicMock(return_value={"id": 11, "transaction_id": 22})

        put_resp = MagicMock()
        put_resp.status_code = 200
        put_resp.headers = {"content-type": "application/json"}
        put_resp.text = '{"id": 22}'
        put_resp.json = MagicMock(return_value={"id": 22})

        def fake_request(method, url, **kwargs):
            calls.append({"method": method, "url": url, "json": kwargs.get("json")})
            return post_resp if method == "POST" else put_resp

        client._client.request = fake_request

        client.attach_file(
            42, "kvitto.pdf", PDF_BYTES,
            description="Anthropic",
            date="2026-05-05",
            credit_account_id=67100,
            vat_lines_attributes=[{
                "taxable": "112.95", "tax_percentage": "0.00",
                "currency": "EUR", "expense_account_id": 67100,
            }],
        )

        put_calls = [c for c in calls if c["method"] == "PUT"]
        self.assertEqual(len(put_calls), 1)
        body = put_calls[0]["json"]["transaction"]
        # Submit-coded fält måste aldrig finnas med
        self.assertNotIn("submitted", body)
        self.assertNotIn("action", body)
        self.assertNotIn("send_for_approval", body)
        # FAS 5.27: state är `unapproved` (Utkast). Inga av C16/C17:s
        # fyra-fält-gissningar (draft/status/is_draft) skickas längre.
        self.assertEqual(body.get("state"), "unapproved")

    def test_update_transaction_extra_fields_cannot_override_state(self):
        """FAS 5.27 — defensivt skydd: även om en framtida caller råkar
        skicka state='submitted' via extra_fields ska update_transaction
        behålla state='unapproved' (guard-flaggan får aldrig regrediera
        till submit)."""
        client = _make_client()
        captured: dict = {}
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"id": 99}'
        resp.json = MagicMock(return_value={"id": 99})

        def fake_request(method, url, **kwargs):
            captured["json"] = kwargs.get("json")
            return resp
        client._client.request = fake_request

        client.update_transaction(
            "99",
            description="X",
            date="2026-05-05",
            extra_fields={"state": "submitted", "cost_center_id": 1},
        )
        body = captured["json"]["transaction"]
        self.assertEqual(body["state"], "unapproved")
        # extra_fields utan guard-namn ska gå igenom
        self.assertEqual(body["cost_center_id"], 1)

    # ---- FAS 5.28 (C21) — state="unapproved" i CREATE-formen ----

    def test_attach_file_post_form_includes_state_unapproved(self):
        """FAS 5.28 (C21) FIX B — POST /attachments måste skicka
        state='unapproved' redan i form-datan så att Bezala aldrig
        defaultar till 'reviewing' (Väntar på andras attestering).

        Test-sessionen 2026-05-18 körde 8 Couples där C19:s PUT-baserade
        draft-guard läckte 8/8 in i 'reviewing' — PUT:en returnerar 500
        i vissa flöden och hinner inte flytta tillbaka draften till
        Utkast. Fixen är att skicka state-flaggan i CREATE-steget."""
        client = _make_client()
        captured = {}
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"id": 1}'
        resp.json = MagicMock(return_value={"id": 1})

        def fake_request(method, url, **kwargs):
            captured.setdefault("calls", []).append({
                "method": method, "url": url,
                "data": kwargs.get("data"),
            })
            return resp

        client._client.request = fake_request
        client.attach_file(2163467, "kvitto.pdf", PDF_BYTES)

        post_calls = [c for c in captured["calls"] if c["method"] == "POST"]
        self.assertEqual(len(post_calls), 1)
        form = post_calls[0]["data"]
        self.assertEqual(
            form.get("state"), "unapproved",
            "POST /attachments form-datan måste innehålla "
            "state='unapproved' så Bezala aldrig skapar draften som "
            "'reviewing' från start",
        )

    def test_upload_file_as_draft_post_form_includes_state_unapproved(self):
        """FAS 5.28 (C21) — samma CREATE-state-guard för
        upload_file_as_draft, som används av upload_receipt-flödet
        (kvitton som inte matchas mot en bill_line)."""
        client = _make_client()
        captured = {}
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"id": 1, "transaction_id": 5005475}'
        resp.json = MagicMock(
            return_value={"id": 1, "transaction_id": 5005475},
        )

        def fake_request(method, url, **kwargs):
            captured["data"] = kwargs.get("data")
            return resp

        client._client.request = fake_request
        client.upload_file_as_draft(PDF_BYTES, "kvitto.pdf")

        self.assertEqual(captured["data"].get("state"), "unapproved")
        self.assertEqual(captured["data"].get("draft"), "1")

    def test_attach_file_cross_currency_put_includes_bill_line_id_and_flag(self):
        """FAS 5.28 (C21) FIX C — cross-currency Couples (SEK-kvitto +
        EUR-bankrad) gav 0/2 i test-sessionen 2026-05-18: bill_id satt
        men `connected_to_bill_line` förblev false, vilket innebär att
        Bezala-UI inte visar 💳-ikonen. PUT:en måste därför också skicka
        bill_line_id (Rails-konvention för fk-id) och
        connected_to_bill_line=True så att kopplingen aktiveras oavsett
        vilket fält Bezalas modell läser av vid cross-currency."""
        client = _make_client()
        calls: list[dict] = []

        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.headers = {"content-type": "application/json"}
        post_resp.text = '{"id": 111, "transaction_id": 5011389}'
        post_resp.json = MagicMock(
            return_value={"id": 111, "transaction_id": 5011389},
        )

        put_resp = MagicMock()
        put_resp.status_code = 200
        put_resp.headers = {"content-type": "application/json"}
        put_resp.text = '{"id": 5011389}'
        put_resp.json = MagicMock(return_value={"id": 5011389})

        def fake_request(method, url, **kwargs):
            calls.append({"method": method, "url": url, "json": kwargs.get("json")})
            return post_resp if method == "POST" else put_resp
        client._client.request = fake_request

        # Skånetrafiken-fallet: SEK-kvitto (245 SEK) länkad mot
        # EUR-bankrad (23.14 EUR). Vi skickar effective_amount/currency
        # från bankraden (EUR) precis som _do_match_to_bezala gör.
        client.attach_file(
            2163489, "skanetrafiken.pdf", PDF_BYTES,
            description="Skånetrafiken",
            date="2026-05-05",
            credit_account_id=67100,
            vat_lines_attributes=[{
                "taxable": "23.14",
                "tax_percentage": "0.0",
                "currency": "EUR",
                "expense_account_id": 67100,
            }],
        )

        put_calls = [c for c in calls if c["method"] == "PUT"]
        self.assertEqual(len(put_calls), 1)
        body = put_calls[0]["json"]["transaction"]
        self.assertEqual(
            str(body.get("bill_id")), "2163489",
            "bill_id måste finnas (C19 garanti)",
        )
        self.assertEqual(
            str(body.get("bill_line_id")), "2163489",
            "C21 — bill_line_id (Rails fk-id-konvention) måste också "
            "skickas så att cross-currency-kopplingen aktiveras",
        )
        self.assertIs(
            body.get("connected_to_bill_line"), True,
            "C21 — connected_to_bill_line=True måste skickas explicit; "
            "GET /transactions visade fältet som false trots korrekt "
            "bill_id för Skånetrafiken-fallet",
        )

    def test_attach_file_legacy_description_only_skips_put(self):
        """Bakåtkompatibilitet: description-only (utan date) → ingen PUT."""
        client = _make_client()
        calls: list[str] = []
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"id": 5}'
        resp.json = MagicMock(return_value={"id": 5})

        def fake_request(method, url, **kwargs):
            calls.append(method)
            return resp
        client._client.request = fake_request

        client.attach_file(
            42, "kvitto.pdf", PDF_BYTES,
            description="legacy",
        )
        self.assertEqual(calls, ["POST"])

    # ---- FAS 5.27 (C18) — hardcoded state=unapproved guard ----

    def _run_put_capture(self, env=None):
        """Helper: kör en update_transaction-call och returnera PUT-bodyn.

        FAS 5.27: `env`-parametern är kvar för bakåtkompatibilitet men
        påverkar inte längre payloaden — state='unapproved' är hårdkodat.
        Helper:n verifierar just det."""
        import os
        client = _make_client()
        captured: dict = {}

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"id": 99}'
        resp.json = MagicMock(return_value={"id": 99})

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return resp
        client._client.request = fake_request

        old = os.environ.get("BEZALA_DRAFT_GUARD_FIELDS_JSON")
        if env is not None:
            os.environ["BEZALA_DRAFT_GUARD_FIELDS_JSON"] = env
        elif "BEZALA_DRAFT_GUARD_FIELDS_JSON" in os.environ:
            del os.environ["BEZALA_DRAFT_GUARD_FIELDS_JSON"]
        try:
            client.update_transaction(
                "99",
                description="Diagnostik",
                date="2026-05-15",
                credit_account_id=67113,
                vat_lines_attributes=[{
                    "taxable": "100.00", "tax_percentage": "0.255",
                    "currency": "EUR", "expense_account_id": 67113,
                }],
            )
        finally:
            if old is None:
                os.environ.pop("BEZALA_DRAFT_GUARD_FIELDS_JSON", None)
            else:
                os.environ["BEZALA_DRAFT_GUARD_FIELDS_JSON"] = old
        return captured["json"]["transaction"]

    def test_default_draft_guard_is_state_unapproved_only(self):
        """FAS 5.27 — default skickar BARA state='unapproved'. C16/C17:s
        fyrdubbla guess (draft/state/status/is_draft) ersätts av den
        verifierade fältnamnet (state) och värdet (unapproved)."""
        body = self._run_put_capture()
        self.assertEqual(body.get("state"), "unapproved")
        # Gamla gissningarna ska INTE finnas med längre — de var fel och
        # ger bara mer brus i Bezalas request-logs.
        self.assertNotIn("draft", body)
        self.assertNotIn("status", body)
        self.assertNotIn("is_draft", body)
        # Submit-kodade fält måste fortfarande aldrig skickas
        for field in ("submitted", "submitted_at", "is_submitted",
                      "action", "approval_status", "submit",
                      "send_for_approval", "mark_as_submitted"):
            self.assertNotIn(field, body, f"{field} får inte finnas i PUT-body")

    def test_draft_guard_env_var_no_longer_overrides_state(self):
        """FAS 5.27 — state='unapproved' är hårdkodat. Tidigare kunde
        operatören sätta `BEZALA_DRAFT_GUARD_FIELDS_JSON` i Railway och
        styra payloaden; nu ignoreras env-vars helt så att rätt fält
        garanterat alltid skickas."""
        body = self._run_put_capture(
            env='{"state": "submitted", "draft": false}',
        )
        # Env-värdena tas INTE in
        self.assertEqual(body.get("state"), "unapproved")
        self.assertNotIn("draft", body)

    def test_draft_guard_env_invalid_no_effect(self):
        """FAS 5.27 — ogiltig JSON i env ändrar inget (env-läsning är
        borta). state='unapproved' är fortfarande hårdkodat."""
        body = self._run_put_capture(env="not json at all")
        self.assertEqual(body.get("state"), "unapproved")

    def test_extra_fields_cannot_smuggle_submit_codes(self):
        """Defensivt: extra_fields får inte injicera submit-fält som
        submitted=True, action='submit', send_for_approval=True etc."""
        client = _make_client()
        captured: dict = {}
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"id": 7}'
        resp.json = MagicMock(return_value={"id": 7})

        def fake_request(method, url, **kwargs):
            captured["json"] = kwargs.get("json")
            return resp
        client._client.request = fake_request

        client.update_transaction(
            "7",
            description="X",
            date="2026-05-15",
            extra_fields={
                "submitted": True,
                "action": "submit",
                "send_for_approval": True,
                "submit": True,
                "is_submitted": True,
                "approval_status": "pending",
                # En oskyldig att verifiera att vanliga fält fortfarande
                # passerar
                "cost_center_id": 9,
            },
        )
        body = captured["json"]["transaction"]
        for blocked in ("submitted", "action", "send_for_approval",
                        "submit", "is_submitted", "approval_status"):
            self.assertNotIn(blocked, body, f"{blocked} smög sig igenom")
        # Lovligt extra-field går fortfarande igenom
        self.assertEqual(body.get("cost_center_id"), 9)
        # FAS 5.27 — guard:en är state='unapproved' (hårdkodat)
        self.assertEqual(body.get("state"), "unapproved")

    def test_diagnostics_capture_records_request_and_response(self):
        """FAS 5.26 — record_diagnostics=True fångar in både request-
        och response-bodies så att /api/debug/test-bezala-draft-flow
        kan returnera dem."""
        client = _make_client()
        client.record_diagnostics = True
        client.call_log = []

        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.headers = {"content-type": "application/json"}
        post_resp.text = '{"id": 333, "transaction_id": 444}'
        post_resp.json = MagicMock(
            return_value={"id": 333, "transaction_id": 444},
        )

        put_resp = MagicMock()
        put_resp.status_code = 200
        put_resp.headers = {"content-type": "application/json"}
        put_resp.text = '{"id": 444, "state": "draft"}'
        put_resp.json = MagicMock(
            return_value={"id": 444, "state": "draft"},
        )

        def fake_request(method, url, **kwargs):
            return post_resp if method == "POST" else put_resp
        client._client.request = fake_request

        client.attach_file(
            42, "k.pdf", PDF_BYTES,
            description="diag",
            date="2026-05-15",
            credit_account_id=67100,
            vat_lines_attributes=[{
                "taxable": "10.00", "tax_percentage": "0.24",
                "currency": "EUR", "expense_account_id": 67100,
            }],
        )

        self.assertGreaterEqual(len(client.call_log), 2)
        methods = [c["method"] for c in client.call_log]
        self.assertIn("POST", methods)
        self.assertIn("PUT", methods)
        put_entry = [c for c in client.call_log if c["method"] == "PUT"][0]
        self.assertEqual(put_entry["response_status"], 200)
        self.assertIn("state", put_entry["response_body"])
        # PUT-payloaden ska innehålla state='unapproved' (FAS 5.27 guard)
        put_req = put_entry["request_body"]
        tx = put_req["transaction"]
        self.assertEqual(tx.get("state"), "unapproved")

    def test_diagnostics_disabled_by_default(self):
        """call_log fylls inte i utan opt-in."""
        client = _make_client()
        # __new__-byggd klient saknar attributen — record_diagnostics
        # ska behandlas som False via getattr-guard i _request.
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"id": 1}'
        resp.json = MagicMock(return_value={"id": 1})

        def fake_request(method, url, **kwargs):
            return resp
        client._client.request = fake_request

        client.update_transaction("1", description="X", date="2026-05-15")
        self.assertFalse(getattr(client, "call_log", None))

    def test_get_transaction_returns_dict(self):
        """get_transaction läser nuvarande state från Bezala för
        post-flight-verifiering."""
        client = _make_client()
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"id": 22, "state": "draft", "draft": true}'
        resp.json = MagicMock(
            return_value={"id": 22, "state": "draft", "draft": True},
        )

        captured: dict = {}

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            return resp
        client._client.request = fake_request

        state = client.get_transaction(22)
        self.assertEqual(captured["method"], "GET")
        self.assertTrue(captured["url"].endswith("/transactions/22"))
        self.assertEqual(state["state"], "draft")
        self.assertIs(state["draft"], True)

    def test_raw_put_transaction_sends_arbitrary_fields(self):
        """FAS 5.27 — raw_put_transaction skickar ALLA fält rakt
        igenom (ingen draft-guard, ingen BLOCKED_SUBMIT-filtrering),
        wrappar i {"transaction": ...}, och returnerar status + body
        utan att raisa på 4xx."""
        client = _make_client()
        resp = MagicMock()
        resp.status_code = 422
        resp.headers = {"content-type": "application/json"}
        resp.text = '{"errors":{"state":"invalid"}}'
        resp.json = MagicMock(
            return_value={"errors": {"state": "invalid"}},
        )

        captured: dict = {}

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return resp
        client._client.request = fake_request

        result = client.raw_put_transaction(
            "2200999",
            {
                "state": "draft",
                # submit-codes får INTE filtreras bort av raw_put
                "submit": False,
                "submitted": False,
                "approval_status": "draft",
            },
        )

        self.assertEqual(captured["method"], "PUT")
        self.assertTrue(captured["url"].endswith("/transactions/2200999"))
        body = captured["json"]["transaction"]
        # ALLA fält ska ha gått igenom — inget filtrerades
        self.assertEqual(body["state"], "draft")
        self.assertIs(body["submit"], False)
        self.assertIs(body["submitted"], False)
        self.assertEqual(body["approval_status"], "draft")
        # Returnerar status+body utan att raisa
        self.assertEqual(result["status_code"], 422)
        self.assertEqual(
            result["body"], {"errors": {"state": "invalid"}},
        )


# ---------- Endpoint-tester ----------


class CardMatchingEndpointsTest(unittest.TestCase):
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

    def _seed_processed(self, **over):
        defaults = dict(
            message_id="m-1",
            sender="invoice@anthropic.com",
            subject="Anthropic API",
            status="saved",
            file_name="20260414 Anthropic API.pdf",
            drive_file_id="drv-1",
            drive_link="https://drive/drv-1",
            vendor="Anthropic",
            amount=112.95,
            currency="EUR",
            receipt_date="2026-04-14",
            category="AI",
            ai_confidence=95,
            bezala_upload_status="pending",
        )
        defaults.update(over)
        with self.SessionLocal() as db:
            row = self.ProcessedMessage(**defaults)
            db.add(row)
            db.flush()
            mid = row.id
            db.commit()
        return mid

    def test_get_missing_receipts_returns_normalized_list(self):
        fake_bezala = MagicMock()
        fake_bezala.list_missing_receipts.return_value = [
            {
                "id": 12345,
                "description": "CLAUDE.AI SUBSCRIPTION",
                "amount": 112.95,
                "currency": "EUR",
                "date": "2026-04-14",
            },
        ]
        with patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.get("/api/bezala/missing-receipts")

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["id"], 12345)
        self.assertEqual(body[0]["description"], "CLAUDE.AI SUBSCRIPTION")

    def test_amount_parsed_from_description_when_field_missing(self):
        """Bezala bill_lines returnerar amount=null. Vi plockar från
        description-strängen så matchern har något att jämföra med."""
        fake_bezala = MagicMock()
        fake_bezala.list_missing_receipts.return_value = [
            {
                "id": 2176713,
                "description": "MIKKO KEINONEN: SKANETRAFIKEN APP, KRISTIANSTAD, SE 28.54 EUR",
                "amount": None,
                "currency": None,
                "date": "2026-04-22",
            },
        ]
        with patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.get("/api/bezala/missing-receipts")

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()[0]
        self.assertEqual(body["amount"], 28.54)
        self.assertEqual(body["currency"], "EUR")

    def test_amount_parsed_with_one_decimal(self):
        """Lovable/Finnair rapporterar 100.0 / 494.5 EUR (1 decimal).
        Tidigare regex \\d{2} missade dessa → amount=null → ingen scoring."""
        fake_bezala = MagicMock()
        fake_bezala.list_missing_receipts.return_value = [
            {
                "id": 9001,
                "description": "MIKKO KEINONEN: LOVABLE, DOVER, US 100.0 EUR",
                "amount": None,
                "currency": None,
                "date": "2026-04-25",
            },
            {
                "id": 9002,
                "description": "MIKKO KEINONEN: FINNAIR O87UJ3J, VANTAA, FI 494.5 EUR",
                "amount": None,
                "currency": None,
                "date": "2026-04-25",
            },
        ]
        with patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.get("/api/bezala/missing-receipts")

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body[0]["amount"], 100.0)
        self.assertEqual(body[0]["currency"], "EUR")
        self.assertEqual(body[1]["amount"], 494.5)
        self.assertEqual(body[1]["currency"], "EUR")

    def test_amount_field_takes_precedence_over_description(self):
        """När Bezala DÅ returnerar strukturerat amount (t.ex. Anthropic)
        används det; description-parsning är bara en fallback."""
        fake_bezala = MagicMock()
        fake_bezala.list_missing_receipts.return_value = [
            {
                "id": 1,
                "description": "nonsense 999.99 EUR",
                "amount": 112.95,
                "currency": "EUR",
                "date": "2026-04-14",
            },
        ]
        with patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.get("/api/bezala/missing-receipts")
        body = resp.json()[0]
        self.assertEqual(body["amount"], 112.95)  # från fältet, inte desc

    def test_match_suggestions_returns_top_candidates(self):
        # Skapa en ProcessedMessage som matchar perfekt
        self._seed_processed()

        fake_bezala = MagicMock()
        fake_bezala.list_missing_receipts.return_value = [
            {
                "id": 12345,
                "description": "CLAUDE.AI SUBSCRIPTION",
                "amount": 112.95,
                "currency": "EUR",
                "date": "2026-04-14",
            },
        ]
        with patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.get("/api/bezala/match-suggestions")

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(len(body), 1)
        entry = body[0]
        self.assertEqual(entry["missing_receipt"]["id"], 12345)
        self.assertEqual(len(entry["suggestions"]), 1)
        self.assertGreaterEqual(entry["suggestions"][0]["score"], 80)

    def test_match_suggestions_include_all_messages_returns_envelope(self):
        """FAS 8.5a — när include_all_messages=true returneras envelope
        {missing_receipts, all_messages} och alla saved-rader (inkl
        kopplade) listas i all_messages med coupled-flagga."""
        # En okopplad rad
        self._seed_processed()
        # En kopplad rad — bezala_upload_status='success'
        self._seed_processed(
            message_id="m-coupled",
            file_name="20260301 Hotel.pdf",
            drive_file_id="drv-2",
            vendor="Scandic",
            amount=500.0,
            currency="EUR",
            receipt_date="2026-03-01",
            bezala_upload_status="success",
            bezala_transaction_id="9999",
        )

        fake_bezala = MagicMock()
        fake_bezala.list_missing_receipts.return_value = [
            {
                "id": 12345,
                "description": "CLAUDE.AI SUBSCRIPTION",
                "amount": 112.95,
                "currency": "EUR",
                "date": "2026-04-14",
            },
        ]
        with patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.get(
                "/api/bezala/match-suggestions?include_all_messages=true",
            )

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        # Envelope-shape
        self.assertIn("missing_receipts", body)
        self.assertIn("all_messages", body)
        self.assertEqual(len(body["missing_receipts"]), 1)
        self.assertEqual(len(body["all_messages"]), 2)

        by_msgid = {m["message_id"]: m for m in body["all_messages"]}
        self.assertFalse(by_msgid["m-1"]["coupled"])
        self.assertIsNone(by_msgid["m-1"]["matched_bill_line_id"])
        self.assertTrue(by_msgid["m-coupled"]["coupled"])
        self.assertEqual(by_msgid["m-coupled"]["matched_bill_line_id"], "9999")

        # Suggestion-listan ska INTE innehålla den kopplade raden
        suggestions = body["missing_receipts"][0]["suggestions"]
        suggestion_msg_ids = {s["message"]["message_id"] for s in suggestions}
        self.assertNotIn("m-coupled", suggestion_msg_ids)

    def test_match_suggestions_default_shape_unchanged(self):
        """Bakåtkompatibilitet: utan flaggan returneras samma shape som tidigare."""
        self._seed_processed()
        fake_bezala = MagicMock()
        fake_bezala.list_missing_receipts.return_value = [
            {
                "id": 12345,
                "description": "CLAUDE.AI SUBSCRIPTION",
                "amount": 112.95,
                "currency": "EUR",
                "date": "2026-04-14",
            },
        ]
        with patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.get("/api/bezala/match-suggestions")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIsInstance(body, list)  # ren lista, inte envelope
        self.assertEqual(len(body), 1)
        self.assertIn("missing_receipt", body[0])
        self.assertIn("suggestions", body[0])

    def _run_match(self, mid: int, missing_receipt_id: int = 2163467):
        """Helper — mockar Drive + Bezala och kör match-to-bezala-endpointen.
        Returnerar (response, fake_bezala) så tester kan inspektera
        attach_file-anropet."""
        fake_drive = MagicMock()
        fake_drive.download_pdf.return_value = PDF_BYTES

        fake_bezala = MagicMock()
        fake_attachment = MagicMock()
        fake_attachment.attachment_id = "att-1"
        fake_bezala.attach_file.return_value = fake_attachment
        fake_bezala.list_missing_receipts.return_value = [
            {
                "id": 2163467,
                "description": "ANTHROPIC API",
                "amount": 112.95,
                "currency": "EUR",
                "date": "2026-04-14",
            },
        ]
        fake_bezala.list_accounts.return_value = []
        fake_bezala.list_cost_centers.return_value = []
        fake_bezala.list_vat_rates.return_value = []

        with patch.object(self.app_module, "DriveClient", return_value=fake_drive), \
             patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.post(
                f"/api/messages/{mid}/match-to-bezala",
                json={"missing_receipt_id": missing_receipt_id},
            )
        return resp, fake_bezala

    def test_match_to_bezala_links_via_bill_line_id(self):
        """FAS 5.24 — match-flödet anropar attach_file med bill_line_id +
        full metadata (date, credit_account_id, vat_lines) så att Bezala
        får rätt belopp/valuta/konto på drafterns PUT.

        FAS 5.27 — bezala_transaction_id sätts till det riktiga
        transaction_id som attach_file returnerar (5xxxxxx i prod), INTE
        bill_line_id (2xxxxxx). I testet är mock-värdet 'att-1' eftersom
        MagicMock-en bara har attachment_id satt; det viktiga är att det
        är attach-result-ID:t, inte bill_line_id:t (2163467)."""
        mid = self._seed_processed()
        resp, fake_bezala = self._run_match(mid)

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["bezala_upload_status"], "success")
        # FAS 5.27 — det riktiga transaction_id från attach_file, INTE
        # bill_line_id. Tidigare lagrade vi "2163467" (bill_line_id) här
        # vilket fick PUT /transactions/{id} att 403:a.
        self.assertEqual(body["bezala_transaction_id"], "att-1")
        self.assertNotEqual(
            body["bezala_transaction_id"], "2163467",
            "bezala_transaction_id får ALDRIG lagra bill_line_id "
            "(2xxxxxx) — det skapar 403:or på alla downstream PUT-anrop.",
        )

        # attach_file anropas med bill_line_id + filename + pdf + full
        # metadata (description, date, credit_account_id, vat_lines).
        fake_bezala.attach_file.assert_called_once()
        args, kwargs = fake_bezala.attach_file.call_args
        self.assertEqual(args[0], "2163467")
        self.assertEqual(args[1], "20260414 Anthropic API.pdf")
        self.assertEqual(args[2], PDF_BYTES)
        # date ska komma från bill_line (2026-04-14)
        self.assertEqual(kwargs["date"], "2026-04-14")
        self.assertIsNotNone(kwargs.get("credit_account_id"))
        # vat_lines (om vat_rates returneras tomt blir listan tom — det är
        # OK, Bezala använder kontots default_vat då).
        self.assertIn("vat_lines_attributes", kwargs)

    def test_match_flow_passes_ai_description_en(self):
        """FAS 5.17 — match-flödet skickar row.ai_description_en till
        Bezala när inget mapping-override finns. Detta är fix:en för
        bugg: Bezala-draft hade tomt description-fält efter Couple."""
        mid = self._seed_processed(
            vendor="Lovable",
            ai_description_en="Lovable Pro 3 subscription, May 5–Jun 5, 2026",
            file_name="20260505 Lovable.pdf",
        )
        resp, fake_bezala = self._run_match(mid)
        self.assertEqual(resp.status_code, 200, resp.text)
        fake_bezala.attach_file.assert_called_once()
        args, kwargs = fake_bezala.attach_file.call_args
        self.assertEqual(args[0], "2163467")
        self.assertEqual(args[1], "20260505 Lovable.pdf")
        self.assertEqual(args[2], PDF_BYTES)
        self.assertEqual(
            kwargs["description"],
            "Lovable Pro 3 subscription, May 5–Jun 5, 2026",
        )

    def test_match_flow_uses_mapping_override_when_present(self):
        """mapping.description_override har högsta prio — överstyr även
        ai_description_en."""
        from app.models import BezalaVendorMapping
        with self.SessionLocal() as db:
            db.add(BezalaVendorMapping(
                vendor_pattern="lovable",
                bezala_account_id=4000,
                vat_rate=0,
                description_override="Lovable AI subscription (mapped)",
            ))
            db.commit()
        mid = self._seed_processed(
            vendor="Lovable",
            ai_description_en="Lovable Pro 3 subscription, May 5–Jun 5, 2026",
        )
        resp, fake_bezala = self._run_match(mid)
        self.assertEqual(resp.status_code, 200, resp.text)
        kwargs = fake_bezala.attach_file.call_args.kwargs
        self.assertEqual(kwargs["description"], "Lovable AI subscription (mapped)")

    def test_match_flow_falls_back_to_summary_for_legacy_rows(self):
        """Legacy-rader saknar ai_description_en men har row.summary
        (svensk AI-sammanfattning från tidigare versioner)."""
        mid = self._seed_processed(
            vendor="Finnair",
            ai_description_en=None,
            summary="Flygbiljett HEL–ARN 14 april 2026",
            file_name="20260414 Finnair.pdf",
        )
        resp, fake_bezala = self._run_match(mid)
        self.assertEqual(resp.status_code, 200, resp.text)
        kwargs = fake_bezala.attach_file.call_args.kwargs
        self.assertEqual(kwargs["description"], "Flygbiljett HEL–ARN 14 april 2026")

    def test_match_flow_payload_includes_description_field(self):
        """Integrationssäkring: attach_file-anropet (mockad Bezala-API)
        innehåller faktiskt description-kwarg — inte bara file/bill_line_id.
        Förhindrar regression där description tappas på vägen."""
        mid = self._seed_processed(
            ai_description_en="Anthropic API usage — april 2026",
        )
        resp, fake_bezala = self._run_match(mid)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(fake_bezala.attach_file.call_count, 1)
        kwargs = fake_bezala.attach_file.call_args.kwargs
        self.assertIn("description", kwargs)
        self.assertEqual(kwargs["description"], "Anthropic API usage — april 2026")

    def test_match_to_bezala_404_for_missing_message(self):
        resp = self.client.post(
            "/api/messages/99999/match-to-bezala",
            json={"missing_receipt_id": 1},
        )
        self.assertEqual(resp.status_code, 404)

    # ---- C20 — amount-mismatch safety net ----

    def _run_match_with_missing(self, mid, missing_receipts, missing_receipt_id):
        """Variant av _run_match som låter testet styra bill_line-snapshot:en
        (för C20-amount-mismatch-fall där snap.amount skiljer sig från
        row.amount)."""
        fake_drive = MagicMock()
        fake_drive.download_pdf.return_value = PDF_BYTES

        fake_bezala = MagicMock()
        fake_attachment = MagicMock()
        fake_attachment.attachment_id = "att-1"
        fake_bezala.attach_file.return_value = fake_attachment
        fake_bezala.list_missing_receipts.return_value = missing_receipts
        fake_bezala.list_accounts.return_value = []
        fake_bezala.list_cost_centers.return_value = []
        fake_bezala.list_vat_rates.return_value = []

        with patch.object(self.app_module, "DriveClient", return_value=fake_drive), \
             patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.post(
                f"/api/messages/{mid}/match-to-bezala",
                json={"missing_receipt_id": missing_receipt_id},
            )
        return resp, fake_bezala

    def test_match_to_bezala_rejects_amount_mismatch_400(self):
        """C20 — Lovable→Finnair-race: frontend skickade fel bill_line_id
        (Finnair 554.50 EUR) för Lovable-kvittot (100 EUR). Backend måste
        avvisa kopplingen istället för att skapa ett Bezala-utkast med
        fel bank-rads belopp."""
        mid = self._seed_processed(amount=100.0, currency="EUR")
        missing = [{
            "id": 2210736,
            "description": "Finnair O9VAZGJ",
            "amount": 554.50,
            "currency": "EUR",
            "date": "2026-05-13",
        }]
        resp, fake_bezala = self._run_match_with_missing(mid, missing, 2210736)

        self.assertEqual(resp.status_code, 400, resp.text)
        detail = resp.json()["detail"]
        self.assertIn("100.00", detail)
        self.assertIn("554.50", detail)
        # Bezala-anropet får ALDRIG ske om vi har upptäckt mismatchen.
        fake_bezala.attach_file.assert_not_called()

    def test_match_to_bezala_allows_cross_currency_via_currency_mismatch(self):
        """C20 — legitim cross-currency: 245 SEK kvitto → 23.14 EUR bank-rad.
        amount-checken skippar när valutorna skiljer sig (bill_line äger
        valutan)."""
        mid = self._seed_processed(amount=245.0, currency="SEK")
        missing = [{
            "id": 2163467,
            "description": "EUR card-tx",
            "amount": 23.14,
            "currency": "EUR",
            "date": "2026-05-13",
        }]
        resp, fake_bezala = self._run_match_with_missing(mid, missing, 2163467)

        self.assertEqual(resp.status_code, 200, resp.text)
        fake_bezala.attach_file.assert_called_once()

    def test_match_to_bezala_allows_small_amount_diff_within_margin(self):
        """C20 — avrundnings-diff (öre/cent) tillåts. Bara kraftiga diffar
        (> 0.5) blockerar."""
        mid = self._seed_processed(amount=112.95, currency="EUR")
        missing = [{
            "id": 2163467,
            "description": "ANTHROPIC",
            "amount": 113.00,   # 0.05 EUR diff — inom marginalen
            "currency": "EUR",
            "date": "2026-04-14",
        }]
        resp, fake_bezala = self._run_match_with_missing(mid, missing, 2163467)

        self.assertEqual(resp.status_code, 200, resp.text)
        fake_bezala.attach_file.assert_called_once()

    def test_match_to_bezala_400_when_missing_drive_file(self):
        mid = self._seed_processed(drive_file_id=None, file_name=None)
        resp = self.client.post(
            f"/api/messages/{mid}/match-to-bezala",
            json={"missing_receipt_id": 1},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Drive-fil", resp.json()["detail"])

    # ---- C23 Del A — 409 race-condition-skydd ----

    def test_match_to_bezala_returns_409_when_bill_line_already_coupled(self):
        """C23 Del A — om en annan ProcessedMessage redan har samma
        bezala_transaction_id (= bill_line_id) ska anropet neka med 409
        så frontend kan visa felmeddelande och refresha listan."""
        coupled = self._seed_processed(
            message_id="m-coupled",
            file_name="20260414 ScandicHotel.pdf",
            drive_file_id="drv-coupled",
            vendor="Scandic",
            amount=499.0,
            currency="EUR",
            bezala_transaction_id="2163467",
            bezala_upload_status="success",
        )
        mid = self._seed_processed()

        resp, fake_bezala = self._run_match(mid)

        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(detail["error"], "bill_line_already_coupled")
        self.assertEqual(detail["existing_message_id"], coupled)
        self.assertEqual(detail["existing_vendor"], "Scandic")
        self.assertEqual(detail["bezala_transaction_id"], "2163467")
        fake_bezala.attach_file.assert_not_called()

    def test_match_to_bezala_allows_rematch_on_same_message_idempotent(self):
        """Re-Couple av samma msg till samma bill_line ska INTE blockas
        — bara en ANNAN msg som redan tagit den triggar 409."""
        mid = self._seed_processed(
            bezala_transaction_id="2163467",
            bezala_upload_status="success",
        )
        resp, fake_bezala = self._run_match(mid)

        self.assertEqual(resp.status_code, 200, resp.text)
        fake_bezala.attach_file.assert_called_once()

    def test_match_to_bezala_ignores_soft_deleted_existing_coupling(self):
        """En soft-deleted rad med samma bezala_transaction_id ska INTE
        blockera nya kopplingar — den är borta ur datan."""
        from datetime import datetime as _dt
        self._seed_processed(
            message_id="m-deleted",
            bezala_transaction_id="2163467",
            bezala_upload_status="success",
            deleted_at=_dt.utcnow(),
            delete_reason="manual",
        )
        mid = self._seed_processed()
        resp, fake_bezala = self._run_match(mid)

        self.assertEqual(resp.status_code, 200, resp.text)
        fake_bezala.attach_file.assert_called_once()

    # ---- FAS 5.26 (C17) — debug-endpoint för draft-flödet ----

    def test_debug_test_bezala_draft_flow_returns_call_log_and_post_flight(self):
        """POST /api/debug/test-bezala-draft-flow:
        - Returnerar call_log (request + response för POST + PUT)
        - Returnerar post_flight (GET /transactions/{tx_id})
        - Sätter row som success-coupled (samma sidoeffekt som prod-endpoint)"""
        mid = self._seed_processed()

        fake_drive = MagicMock()
        fake_drive.download_pdf.return_value = PDF_BYTES

        # Endpointen sätter bezala.call_log = [] vid start. Sätter därför
        # call_log via attach_file:s side_effect (just-in-time efter
        # endpointens reset).
        fake_bezala = MagicMock()
        fake_attachment = MagicMock()
        fake_attachment.attachment_id = "tx-9001"

        def _populate_call_log(*args, **kwargs):
            fake_bezala.call_log.extend([
                {
                    "method": "POST", "path": "/attachments",
                    "request_body": {
                        "files": ["file"],
                        "data": {"draft": "1", "bill_line_id": "2163467"},
                    },
                    "response_status": 200,
                    "response_body": '{"id": 111, "transaction_id": "tx-9001"}',
                },
                {
                    "method": "PUT", "path": "/transactions/tx-9001",
                    "request_body": {
                        "transaction": {
                            "description": "Anthropic API usage",
                            "date": "2026-04-14",
                            "state": "unapproved",
                        },
                    },
                    "response_status": 200,
                    "response_body": '{"id": "tx-9001", "state": "unapproved"}',
                },
            ])
            return fake_attachment
        fake_bezala.attach_file.side_effect = _populate_call_log
        fake_bezala.list_missing_receipts.return_value = [
            {
                "id": 2163467, "description": "ANTHROPIC API",
                "amount": 112.95, "currency": "EUR",
                "date": "2026-04-14",
            },
        ]
        fake_bezala.list_accounts.return_value = []
        fake_bezala.list_cost_centers.return_value = []
        fake_bezala.list_vat_rates.return_value = []
        fake_bezala.get_transaction.return_value = {
            "id": "tx-9001",
            "state": "unapproved",
            "submitted_at": None,
        }

        with patch.object(self.app_module, "DriveClient", return_value=fake_drive), \
             patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.post(
                "/api/debug/test-bezala-draft-flow",
                json={"message_id": mid, "missing_receipt_id": 2163467},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["bill_line_id"], "2163467")
        self.assertEqual(body["transaction_id"], "tx-9001")
        self.assertIsNone(body["error"])

        # call_log fångar både POST och PUT
        methods = [c["method"] for c in body["call_log"]]
        self.assertIn("POST", methods)
        self.assertIn("PUT", methods)

        # Post-flight visar att Bezala-state är "unapproved" (= fix:en träffade)
        self.assertEqual(body["post_flight"]["state"], "unapproved")

        # get_transaction har anropats (post-flight)
        fake_bezala.get_transaction.assert_called_once_with("tx-9001")
        # record_diagnostics aktiverades
        self.assertTrue(fake_bezala.record_diagnostics)

    def test_debug_test_bezala_draft_flow_404_for_missing_message(self):
        resp = self.client.post(
            "/api/debug/test-bezala-draft-flow",
            json={"message_id": 99999, "missing_receipt_id": 1},
        )
        self.assertEqual(resp.status_code, 404)

    def test_debug_test_bezala_draft_flow_surfaces_bezala_error(self):
        """När attach_file höjer BezalaError ska debug-endpointen
        returnera felet i `error`-fältet — inte krascha. Mikko kan då
        läsa felmeddelandet utan att gräva i loggar."""
        from app.services.bezala_client import BezalaError
        mid = self._seed_processed()

        fake_drive = MagicMock()
        fake_drive.download_pdf.return_value = PDF_BYTES
        fake_bezala = MagicMock()
        fake_bezala.list_missing_receipts.return_value = []
        fake_bezala.list_accounts.return_value = []
        fake_bezala.list_cost_centers.return_value = []
        fake_bezala.list_vat_rates.return_value = []

        def _err(*args, **kwargs):
            fake_bezala.call_log.append({
                "method": "POST", "path": "/attachments",
                "request_body": {"data": {"draft": "1"}},
                "response_status": 422,
                "response_body": '{"error":"validation"}',
            })
            raise BezalaError(
                "validation failed", status_code=422, body="bad request",
            )
        fake_bezala.attach_file.side_effect = _err

        with patch.object(self.app_module, "DriveClient", return_value=fake_drive), \
             patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.post(
                "/api/debug/test-bezala-draft-flow",
                json={"message_id": mid, "missing_receipt_id": 2163467},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsNotNone(body["error"])
        self.assertEqual(body["error"]["status_code"], 422)
        # call_log finns även när error inträffar
        self.assertTrue(body["call_log"])

    # ---- FAS 5.27 (C18) — PUT-only debug-endpoint ----

    def test_put_only_endpoint_does_not_create_new_draft(self):
        """POST /api/debug/bezala-transaction-put får BARA kalla
        GET + PUT + GET — aldrig POST /attachments eller skapa nya
        rader. Skyddar mot att vi råkar skapa nya stuck-drafter när
        Mikko iterar fältnamn mot Finnair O87UJ3J."""
        fake_bezala = MagicMock()
        fake_bezala.call_log = []
        fake_bezala.get_transaction.side_effect = [
            {"id": "2200999", "state": "submitted", "draft": False},
            {"id": "2200999", "state": "draft", "draft": True},
        ]
        fake_bezala.raw_put_transaction.return_value = {
            "status_code": 200,
            "body": {"id": "2200999", "state": "draft"},
        }

        with patch.object(
            self.app_module, "BezalaClient", return_value=fake_bezala,
        ):
            resp = self.client.post(
                "/api/debug/bezala-transaction-put",
                json={
                    "transaction_id": "2200999",
                    "fields": {"state": "draft", "submit": False},
                },
            )
        self.assertEqual(resp.status_code, 200, resp.text)

        # Endast PUT-anropet får ha gjorts, inget annat skrivande.
        fake_bezala.raw_put_transaction.assert_called_once_with(
            "2200999", {"state": "draft", "submit": False},
        )
        # attach_file / upload_attachment / create_transaction får
        # ALDRIG kallas av denna endpoint.
        fake_bezala.attach_file.assert_not_called()
        fake_bezala.upload_attachment.assert_not_called()
        if hasattr(fake_bezala, "create_transaction"):
            fake_bezala.create_transaction.assert_not_called()

        body = resp.json()
        self.assertEqual(body["transaction_id"], "2200999")
        self.assertTrue(body["status_changed"])

    def test_put_only_endpoint_returns_pre_and_post_state(self):
        """Endpointen returnerar pre_flight, put_call och post_flight
        — så Mikko kan se exakt vad som ändrades."""
        fake_bezala = MagicMock()
        # Endpointen sätter call_log = [] vid start, så side_effects
        # appendar just-in-time efter reset.
        get_responses = [
            {"id": "2200999", "state": "submitted",
             "approval_status": "pending", "draft": False},
            {"id": "2200999", "state": "draft",
             "approval_status": None, "draft": True},
        ]

        def _get_tx(tx_id):
            r = get_responses.pop(0)
            fake_bezala.call_log.append({
                "method": "GET",
                "path": f"/transactions/{tx_id}",
                "request_body": None,
                "response_status": 200,
                "response_body": str(r),
            })
            return r
        fake_bezala.get_transaction.side_effect = _get_tx

        def _put_tx(tx_id, fields):
            fake_bezala.call_log.append({
                "method": "PUT",
                "path": f"/transactions/{tx_id}",
                "request_body": {"transaction": dict(fields)},
                "response_status": 200,
                "response_body": '{"state": "draft"}',
            })
            return {"status_code": 200,
                    "body": {"id": str(tx_id), "state": "draft"}}
        fake_bezala.raw_put_transaction.side_effect = _put_tx

        with patch.object(
            self.app_module, "BezalaClient", return_value=fake_bezala,
        ):
            resp = self.client.post(
                "/api/debug/bezala-transaction-put",
                json={
                    "transaction_id": "2200999",
                    "fields": {"state": "draft"},
                },
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()

        self.assertEqual(body["pre_flight"]["state"], "submitted")
        self.assertEqual(body["post_flight"]["state"], "draft")
        self.assertEqual(
            body["put_call"]["request_fields"], {"state": "draft"},
        )
        self.assertEqual(body["put_call"]["status_code"], 200)
        self.assertTrue(body["status_changed"])
        # call_log mirrar de tre anropen (GET, PUT, GET)
        methods = [c["method"] for c in body["call_log"]]
        self.assertEqual(methods, ["GET", "PUT", "GET"])
        # record_diagnostics aktiverades
        self.assertTrue(fake_bezala.record_diagnostics)

    def test_put_only_endpoint_handles_unknown_tx_id_gracefully(self):
        """Om GET /transactions/{tx_id} svarar 404 ska endpointen
        returnera 200 med error-info i pre_flight — inte krascha eller
        gå vidare till PUT (vi vill inte PUT:a mot en tx vi inte ens
        kunde läsa)."""
        from app.services.bezala_client import BezalaError
        fake_bezala = MagicMock()
        fake_bezala.call_log = []
        fake_bezala.get_transaction.side_effect = BezalaError(
            "not found", status_code=404, body='{"error":"not_found"}',
        )

        with patch.object(
            self.app_module, "BezalaClient", return_value=fake_bezala,
        ):
            resp = self.client.post(
                "/api/debug/bezala-transaction-put",
                json={
                    "transaction_id": "9999999",
                    "fields": {"state": "draft"},
                },
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["pre_flight"]["status_code"], 404)
        # PUT får ALDRIG köras när pre-flight misslyckades
        fake_bezala.raw_put_transaction.assert_not_called()
        self.assertIsNone(body["put_call"])
        self.assertIsNone(body["post_flight"])
        self.assertFalse(body["status_changed"])

    def test_put_only_endpoint_rejects_attachment_fields_in_body(self):
        """Defensiv check: alla fält som hintar om attachment/file/
        bill_line/create nekas med 400 INNAN BezalaClient ens
        instansieras. Vi vill inte ge oss själva ammunition att av
        misstag trigga ny draft-creation via PUT-endpointen."""
        # Vi behöver inte ens mocka BezalaClient — 400 ska komma
        # från endpointens egen guard.
        cases = [
            {"attachment_id": "555"},
            {"attachments_attributes": [{"file": "x"}]},
            {"file": "data:application/pdf;base64,..."},
            {"bill_line_id": "777"},
            {"bill_lines_attributes": [{"id": 1}]},
            {"ATTACHMENT": "x"},  # case-insensitive
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                resp = self.client.post(
                    "/api/debug/bezala-transaction-put",
                    json={
                        "transaction_id": "2200999",
                        "fields": fields,
                    },
                )
                self.assertEqual(resp.status_code, 400, resp.text)
                self.assertIn("attach", resp.json()["detail"].lower())

        # Sanity: rena state-fält PASSERAR guard:en (når BezalaClient).
        fake_bezala = MagicMock()
        fake_bezala.call_log = []
        fake_bezala.get_transaction.return_value = {"state": "draft"}
        fake_bezala.raw_put_transaction.return_value = {
            "status_code": 200, "body": {"state": "draft"},
        }
        with patch.object(
            self.app_module, "BezalaClient", return_value=fake_bezala,
        ):
            resp = self.client.post(
                "/api/debug/bezala-transaction-put",
                json={
                    "transaction_id": "2200999",
                    "fields": {"state": "draft", "submit": False,
                               "is_draft": True},
                },
            )
        self.assertEqual(resp.status_code, 200, resp.text)


# ---------- FAS 5.24 — robust coupling-tester ----------


class RobustCouplingTest(unittest.TestCase):
    """FAS 5.24 — match-to-bezala måste skicka rätt belopp/valuta till
    Bezala även när AI-extracted skiljer sig från bank_row, så att
    drafterns 💳-koppling håller."""

    @classmethod
    def setUpClass(cls):
        db_module = _configure_memory_engine()
        from app.db import Base
        from app import models  # noqa: F401
        from app import main as app_module
        from app.models import ProcessedMessage
        from fastapi.testclient import TestClient
        from contextlib import contextmanager

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

    def _seed(self, **over):
        defaults = dict(
            message_id="m-rc",
            sender="invoice@example.com",
            subject="Receipt",
            status="saved",
            file_name="kvitto.pdf",
            drive_file_id="drv-rc",
            drive_link="https://drive/drv-rc",
            bezala_upload_status="pending",
        )
        defaults.update(over)
        with self.SessionLocal() as db:
            row = self.ProcessedMessage(**defaults)
            db.add(row)
            db.flush()
            mid = row.id
            db.commit()
        return mid

    # Bezala-konton som täcker kategorierna vi använder i RobustCouplingTest.
    # Vi tillhandahåller default_vat_id=864 (24 %) så build_vat_lines_attributes
    # bygger en rad även utan vat_rates-listan.
    _DEFAULT_ACCOUNTS = [
        {"id": 67100, "name": "Matkaliput", "default_vat_id": 864},
        {"id": 67113, "name": "Paikoituskulut", "default_vat_id": 864},
        {"id": 67110, "name": "Muut matkakulut", "default_vat_id": 864},
    ]

    def _mk_fake_bezala(self, missing_receipts, accounts=None,
                        cost_centers=None, vat_rates=None):
        fake_bezala = MagicMock()
        attach_ret = MagicMock()
        attach_ret.attachment_id = "att-rc"
        fake_bezala.attach_file.return_value = attach_ret
        fake_bezala.list_missing_receipts.return_value = missing_receipts
        fake_bezala.list_accounts.return_value = (
            self._DEFAULT_ACCOUNTS if accounts is None else accounts
        )
        fake_bezala.list_cost_centers.return_value = cost_centers or []
        fake_bezala.list_vat_rates.return_value = vat_rates or []
        return fake_bezala

    def _post_match(self, mid, bill_line_id, fake_bezala, pdf=PDF_BYTES):
        fake_drive = MagicMock()
        fake_drive.download_pdf.return_value = pdf
        with patch.object(self.app_module, "DriveClient", return_value=fake_drive), \
             patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            return self.client.post(
                f"/api/messages/{mid}/match-to-bezala",
                json={"missing_receipt_id": bill_line_id},
            )

    # ---- amount/currency-källa ----

    def test_coupling_uses_bank_row_amount_not_ai_extracted(self):
        """När bank_row.amount (366.32) ≠ row.amount (333.00) ska
        attach_file få bank_row-beloppet i vat_lines."""
        mid = self._seed(
            vendor="Finnair", category="flyg",
            amount=333.00, currency="EUR", receipt_date="2026-05-01",
            ai_description_en="Helsinki–Stockholm return flight",
        )
        fake_bezala = self._mk_fake_bezala([
            {
                "id": 7001,
                "description": "MIKKO: FINNAIR HEL-ARN, VANTAA, FI 366.32 EUR",
                "amount": 366.32,
                "currency": "EUR",
                "date": "2026-05-02",
            },
        ])
        resp = self._post_match(mid, 7001, fake_bezala)
        self.assertEqual(resp.status_code, 200, resp.text)
        kwargs = fake_bezala.attach_file.call_args.kwargs
        vat_lines = kwargs.get("vat_lines_attributes") or []
        self.assertTrue(vat_lines, "vat_lines_attributes ska inte vara tom")
        self.assertEqual(vat_lines[0]["taxable"], "366.32")
        # och INTE 333.00 (AI-extracted)
        self.assertNotEqual(vat_lines[0]["taxable"], "333.00")

    def test_coupling_uses_bank_row_currency_not_receipt_currency(self):
        """När bank_row.currency = EUR och row.currency = SEK ska
        vat_lines få EUR (kuponger flyter via banken i EUR)."""
        mid = self._seed(
            vendor="Skånetrafiken", category="kollektivtrafik",
            amount=245.00, currency="SEK", receipt_date="2026-05-03",
        )
        fake_bezala = self._mk_fake_bezala([
            {
                "id": 7002,
                "description": "MIKKO: SKANETRAFIKEN APP, KRISTIANSTAD, SE 23.14 EUR",
                "amount": 23.14,
                "currency": "EUR",
                "date": "2026-05-04",
            },
        ])
        resp = self._post_match(mid, 7002, fake_bezala)
        self.assertEqual(resp.status_code, 200, resp.text)
        kwargs = fake_bezala.attach_file.call_args.kwargs
        vat_lines = kwargs.get("vat_lines_attributes") or []
        self.assertTrue(vat_lines)
        self.assertEqual(vat_lines[0]["currency"], "EUR")
        self.assertEqual(vat_lines[0]["taxable"], "23.14")

    def test_cross_currency_sek_receipt_eur_bank_row_couples_correctly(self):
        """FAIL 1-regression: SEK-kvitto kopplas mot EUR-bankrad — drafterns
        belopp ska vara EUR-motsvarigheten, inte 0,00 och inte 245 SEK."""
        mid = self._seed(
            vendor="Skånetrafiken", category="kollektivtrafik",
            amount=245.00, currency="SEK", receipt_date="2026-05-03",
        )
        fake_bezala = self._mk_fake_bezala([
            {
                "id": 7003,
                "description": "MIKKO: SKANETRAFIKEN APP, KRISTIANSTAD, SE 23.22 EUR",
                "amount": 23.22,
                "currency": "EUR",
                "date": "2026-05-05",
            },
        ])
        resp = self._post_match(mid, 7003, fake_bezala)
        self.assertEqual(resp.status_code, 200, resp.text)
        # attach_file ska få bill_line_id (coupling) + EUR-belopp
        args = fake_bezala.attach_file.call_args.args
        kwargs = fake_bezala.attach_file.call_args.kwargs
        self.assertEqual(args[0], "7003")
        self.assertEqual(kwargs.get("date"), "2026-05-05")
        vat_lines = kwargs.get("vat_lines_attributes") or []
        self.assertTrue(vat_lines)
        self.assertEqual(vat_lines[0]["taxable"], "23.22")
        self.assertEqual(vat_lines[0]["currency"], "EUR")

    def test_match_to_bezala_with_sek_receipt_eur_bank_row_still_correctly_uses_eur_amount(self):
        """FAS 5.25 — bevarad C12/C15-coupling från PR #51: när vi nu
        även skickar draft=true i PUT-payloaden får det INTE bryta
        cross-currency-fixen (Skånetrafiken 245 SEK → 23.14 EUR).

        Verifierar både:
          - attach_file får rätt EUR-belopp/-valuta i vat_lines
          - kwargs.date = bank_row.date (2026-05-04)
          - kwargs.credit_account_id är satt (mapping/account-val funkar)
        Drafterns 'Utkast'-status verifieras separat i
        test_match_to_bezala_payload_includes_draft_status_explicitly."""
        mid = self._seed(
            message_id="m-skane-2314",
            vendor="Skånetrafiken", category="kollektivtrafik",
            amount=245.00, currency="SEK", receipt_date="2026-05-03",
            ai_description_en="Skånetrafiken app — single ticket Kristianstad",
        )
        fake_bezala = self._mk_fake_bezala([
            {
                "id": 7777,
                "description": "MIKKO: SKANETRAFIKEN APP, KRISTIANSTAD, SE 23.14 EUR",
                "amount": 23.14,
                "currency": "EUR",
                "date": "2026-05-04",
            },
        ])
        resp = self._post_match(mid, 7777, fake_bezala)
        self.assertEqual(resp.status_code, 200, resp.text)

        fake_bezala.attach_file.assert_called_once()
        args = fake_bezala.attach_file.call_args.args
        kwargs = fake_bezala.attach_file.call_args.kwargs
        # Coupling — rätt bill_line_id
        self.assertEqual(args[0], "7777")
        # Datum från bank_row, inte från kvittot
        self.assertEqual(kwargs.get("date"), "2026-05-04")
        # Konto är satt (mapping eller default)
        self.assertIsNotNone(kwargs.get("credit_account_id"))
        # Belopp/valuta i EUR — INTE 245 SEK och INTE 0.00
        vat_lines = kwargs.get("vat_lines_attributes") or []
        self.assertTrue(vat_lines, "vat_lines måste ha en EUR-rad")
        self.assertEqual(vat_lines[0]["taxable"], "23.14")
        self.assertEqual(vat_lines[0]["currency"], "EUR")
        self.assertNotEqual(vat_lines[0]["taxable"], "245.00")
        self.assertNotEqual(vat_lines[0]["taxable"], "0.00")
        self.assertNotEqual(vat_lines[0]["currency"], "SEK")

    def test_finnair_amount_mismatch_uses_bank_row_366_32(self):
        """FAIL 2-regression: Finnair eticket där AI extracted 333.00 från
        Fare; bank_row har 366.32 (Grand Total). Drafterns belopp = 366.32."""
        mid = self._seed(
            message_id="m-finnair-596",
            vendor="Finnair", category="flyg",
            amount=333.00, currency="EUR", receipt_date="2026-04-30",
            ai_description_en="HEL-CPH economy ticket",
        )
        fake_bezala = self._mk_fake_bezala([
            {
                "id": 8001,
                "description": "MIKKO: FINNAIR O87UJ3J, VANTAA, FI 366.32 EUR",
                "amount": 366.32,
                "currency": "EUR",
                "date": "2026-05-01",
            },
        ])
        resp = self._post_match(mid, 8001, fake_bezala)
        self.assertEqual(resp.status_code, 200, resp.text)
        kwargs = fake_bezala.attach_file.call_args.kwargs
        vat_lines = kwargs.get("vat_lines_attributes") or []
        self.assertTrue(vat_lines)
        self.assertEqual(vat_lines[0]["taxable"], "366.32")
        self.assertEqual(vat_lines[0]["currency"], "EUR")
        # ai_description_en ska finnas kvar i description (C12-regression
        # vävs in i C14 — Mikko vill inte tappa AI-beskrivningen)
        self.assertEqual(kwargs.get("description"), "HEL-CPH economy ticket")

    # ---- mapping-prio (C12-regression vävd in i C14) ----

    def test_moovy_mapping_priority_holds(self):
        """FAIL 3-regression: Moovy-kvitto ska få Paikoituskulut + 25.5%
        även när det går via match-to-bezala (inte bara via direkt upload)."""
        from app.services.bezala_config import create_mapping
        with self.SessionLocal() as db:
            create_mapping(
                db,
                vendor_pattern="moovy",
                bezala_account_id=67113,
                vat_rate="25.5",
                description_override=None,
            )
            db.commit()

        mid = self._seed(
            message_id="m-moovy-622",
            vendor="Moovy",
            category="parkering",
            amount=37.49,
            currency="EUR",
            receipt_date="2026-05-10",
            sender="noreply@moovy.fi",
        )
        fake_bezala = self._mk_fake_bezala(
            [
                {
                    "id": 9001,
                    "description": "MIKKO: MOOVY VANTAA, FI 37.49 EUR",
                    "amount": 37.49,
                    "currency": "EUR",
                    "date": "2026-05-10",
                },
            ],
            accounts=[{"id": 67113, "name": "Paikoituskulut", "default_vat_id": None}],
        )
        resp = self._post_match(mid, 9001, fake_bezala)
        self.assertEqual(resp.status_code, 200, resp.text)
        kwargs = fake_bezala.attach_file.call_args.kwargs
        vat_lines = kwargs.get("vat_lines_attributes") or []
        self.assertTrue(vat_lines, "Moovy ska få vat_lines via mapping")
        self.assertEqual(vat_lines[0]["expense_account_id"], 67113)
        # 25.5 % i Bezala-format = "0.255"
        self.assertEqual(vat_lines[0]["tax_percentage"], "0.255")

    def test_description_uses_ai_description_en_when_no_mapping(self):
        """C12-regression: när vendor saknar mapping ska description fallback
        bli row.ai_description_en (inte filnamnet)."""
        mid = self._seed(
            message_id="m-desc",
            vendor="UnknownVendor",
            category="other",
            amount=10.0,
            currency="EUR",
            receipt_date="2026-05-12",
            ai_description_en="Tasty lunch in Helsinki",
        )
        fake_bezala = self._mk_fake_bezala([
            {
                "id": 9101,
                "description": "MIKKO: UNKNOWN 10.00 EUR",
                "amount": 10.0,
                "currency": "EUR",
                "date": "2026-05-12",
            },
        ])
        resp = self._post_match(mid, 9101, fake_bezala)
        self.assertEqual(resp.status_code, 200, resp.text)
        kwargs = fake_bezala.attach_file.call_args.kwargs
        self.assertEqual(kwargs.get("description"), "Tasty lunch in Helsinki")


if __name__ == "__main__":
    unittest.main()
