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
        from app.services.receipt_matcher import score_match
        # 112.95 vs 117.00 → 3.6% diff → inom 5%
        s = score_match(self._missing(), self._candidate(amount=117.00))
        self.assertEqual(s["breakdown"]["amount"], 50)

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
        self.assertEqual(captured["data"], {
            "draft": "1",
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

    def test_match_to_bezala_links_via_bill_line_id(self):
        """Match-flödet anropar attach_file med bill_line_id (UI:s
        'Koppla till existerande'-flöde) — inga metadata, inga PUT."""
        mid = self._seed_processed()

        fake_drive = MagicMock()
        fake_drive.download_pdf.return_value = PDF_BYTES

        fake_bezala = MagicMock()
        fake_attachment = MagicMock()
        fake_attachment.attachment_id = "att-1"
        fake_bezala.attach_file.return_value = fake_attachment

        with patch.object(self.app_module, "DriveClient", return_value=fake_drive), \
             patch.object(self.app_module, "BezalaClient", return_value=fake_bezala):
            resp = self.client.post(
                f"/api/messages/{mid}/match-to-bezala",
                json={"missing_receipt_id": 2163467},
            )

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["bezala_upload_status"], "success")
        self.assertEqual(body["bezala_transaction_id"], "2163467")

        # PUT/update_transaction får INTE kallas
        fake_bezala.update_transaction.assert_not_called()
        # Vi bygger inte metadata längre (bill_line äger den)
        fake_bezala.list_accounts.assert_not_called()
        fake_bezala.list_cost_centers.assert_not_called()
        fake_bezala.list_vat_rates.assert_not_called()

        # attach_file anropas med bill_line_id + filename + pdf +
        # description (filnamn utan .pdf) — inga andra metadata
        fake_bezala.attach_file.assert_called_once_with(
            "2163467", "20260414 Anthropic API.pdf", PDF_BYTES,
            description="20260414 Anthropic API",
        )

    def test_match_to_bezala_404_for_missing_message(self):
        resp = self.client.post(
            "/api/messages/99999/match-to-bezala",
            json={"missing_receipt_id": 1},
        )
        self.assertEqual(resp.status_code, 404)

    def test_match_to_bezala_400_when_missing_drive_file(self):
        mid = self._seed_processed(drive_file_id=None, file_name=None)
        resp = self.client.post(
            f"/api/messages/{mid}/match-to-bezala",
            json={"missing_receipt_id": 1},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Drive-fil", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
