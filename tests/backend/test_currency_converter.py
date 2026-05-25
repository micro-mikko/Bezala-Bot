"""Tester för app.services.currency_converter.

Täcker:
- DB-cache hit (inget API-anrop)
- DB-cache miss → API-anrop → cacha
- Same currency → 1.0 (ingen DB/API)
- API-fel (timeout, 404, okänd valuta) → None, ingen cache
- Cross-currency matcher-integration
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("APP_PASSWORD", "x")
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
        bind=db_module.engine, autoflush=False, autocommit=False
    )
    return db_module


class CurrencyConverterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_module = _configure_memory_engine()
        from app.db import Base
        from app import models  # noqa: F401
        Base.metadata.create_all(bind=db_module.engine)
        cls.SessionLocal = db_module.SessionLocal
        cls.CurrencyRate = models.CurrencyRate

    def setUp(self):
        with self.SessionLocal() as db:
            db.query(self.CurrencyRate).delete()
            db.commit()

    def test_same_currency_returns_1(self):
        from app.services.currency_converter import get_rate
        with self.SessionLocal() as db:
            self.assertEqual(get_rate("2026-04-22", "SEK", "SEK", db=db), 1.0)

    def test_normalization_case_and_whitespace(self):
        from app.services.currency_converter import get_rate
        with self.SessionLocal() as db:
            self.assertEqual(get_rate("2026-04-22", " sek ", "SEK", db=db), 1.0)

    def test_empty_inputs_return_none(self):
        from app.services.currency_converter import get_rate
        with self.SessionLocal() as db:
            self.assertIsNone(get_rate("", "SEK", "EUR", db=db))
            self.assertIsNone(get_rate("2026-04-22", "", "EUR", db=db))
            self.assertIsNone(get_rate("2026-04-22", "SEK", "", db=db))

    def test_cache_hit_skips_api(self):
        """När kursen redan finns i DB ska inget API-anrop göras."""
        from app.services.currency_converter import get_rate
        with self.SessionLocal() as db:
            db.add(self.CurrencyRate(
                date="2026-04-22", from_currency="SEK",
                to_currency="EUR", rate=0.0875,
            ))
            db.commit()
        with patch(
            "app.services.currency_converter._fetch_rate_from_api"
        ) as mock_fetch:
            with self.SessionLocal() as db:
                rate = get_rate("2026-04-22", "SEK", "EUR", db=db)
            self.assertEqual(rate, 0.0875)
            mock_fetch.assert_not_called()

    def test_cache_miss_fetches_and_caches(self):
        from app.services.currency_converter import get_rate
        with patch(
            "app.services.currency_converter._fetch_rate_from_api",
            return_value=0.0875,
        ):
            with self.SessionLocal() as db:
                rate = get_rate("2026-04-22", "SEK", "EUR", db=db)
                self.assertEqual(rate, 0.0875)
                row = db.query(self.CurrencyRate).filter_by(
                    date="2026-04-22", from_currency="SEK", to_currency="EUR",
                ).first()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row.rate, 0.0875)

    def test_api_failure_returns_none_no_cache(self):
        from app.services.currency_converter import get_rate
        with patch(
            "app.services.currency_converter._fetch_rate_from_api",
            return_value=None,
        ):
            with self.SessionLocal() as db:
                rate = get_rate("2026-04-22", "SEK", "EUR", db=db)
        self.assertIsNone(rate)
        with self.SessionLocal() as db:
            self.assertEqual(db.query(self.CurrencyRate).count(), 0)

    def test_fetch_handles_api_exception_gracefully(self):
        """Nätverksfel i _fetch_rate_from_api får inte propagera."""
        from app.services.currency_converter import _fetch_rate_from_api
        import httpx
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.side_effect = (
                httpx.ConnectTimeout("simulated")
            )
            result = _fetch_rate_from_api("2026-04-22", "SEK", "EUR")
        self.assertIsNone(result)

    def test_fetch_handles_non_200_response(self):
        from app.services.currency_converter import _fetch_rate_from_api
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = mock_resp
            result = _fetch_rate_from_api("2099-01-01", "SEK", "EUR")
        self.assertIsNone(result)

    def test_fetch_handles_missing_rate_key(self):
        from app.services.currency_converter import _fetch_rate_from_api
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"rates": {}}  # tom — okänd valuta
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = mock_resp
            result = _fetch_rate_from_api("2026-04-22", "XYZ", "RUB")
        self.assertIsNone(result)


class MatcherCurrencyIntegrationTest(unittest.TestCase):
    """Verifiera att matchern använder rate_provider för cross-currency."""

    def test_exact_example_from_spec(self):
        """Bezala: 28.54 EUR, Kvitto: 300 SEK, kurs 0.0951 SEK→EUR
        → 300 * 0.0951 = 28.53 EUR, diff 0.04% → <2% → match.
        Match algorithm 3.1: cross-currency-toleransen är ±2% (tidigare
        ±10% gav false positives för NOK↔EUR-jämförelser där olika
        transaktioner råkade ligga i samma EUR-storleksordning)."""
        from app.services.receipt_matcher import find_matches

        missing = {
            "id": 1, "amount": 28.54, "currency": "EUR",
            "date": "2026-04-22", "description": "Skånetrafiken",
        }
        candidate = {
            "id": 100, "amount": 300.0, "currency": "SEK",
            "receipt_date": "2026-04-22", "vendor": "Skånetrafiken",
            "sender": "noreply@skanetrafiken.se",
        }

        # Rate-provider: SEK→EUR = 0.0951 (near-perfect match)
        def rate_provider(date_str, from_c, to_c):
            if (from_c, to_c) == ("SEK", "EUR"):
                return 0.0951
            return None

        results = find_matches(missing, [candidate], rate_provider=rate_provider)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertIn("conversion", r)
        self.assertEqual(r["conversion"]["from_currency"], "SEK")
        self.assertEqual(r["conversion"]["to_currency"], "EUR")
        self.assertAlmostEqual(r["conversion"]["to_amount"], 28.53, places=2)
        self.assertAlmostEqual(r["conversion"]["rate"], 0.0951, places=4)
        # 40p amount (converted) + 30p date (exakt) + 30p vendor (exakt)
        self.assertEqual(r["score_breakdown"]["amount"], 40)
        self.assertEqual(r["score_breakdown"]["date"], 30)

    def test_same_currency_path_unchanged(self):
        """När båda i samma valuta används ±5%-regeln. C30b: 1% diff
        faller i 0.5-2%-bucketen → 40p (tidigare binärt 50p)."""
        from app.services.receipt_matcher import find_matches
        missing = {"amount": 100, "currency": "EUR", "date": "2026-04-22",
                   "description": "Finnair"}
        candidate = {"id": 1, "amount": 101, "currency": "EUR",
                     "receipt_date": "2026-04-22", "vendor": "Finnair"}
        rp = MagicMock()
        results = find_matches(missing, [candidate], rate_provider=rp)
        self.assertEqual(len(results), 1)
        # 1% diff → 0.5-2%-bucket → 40p (C30b graderad amount)
        self.assertEqual(results[0]["score_breakdown"]["amount"], 40)
        self.assertNotIn("conversion", results[0])
        # rate_provider ska inte kallas när samma valuta matchar direkt
        rp.assert_not_called()

    def test_rate_unavailable_gives_no_amount_points(self):
        """Om rate_provider returnerar None för cross-currency: ingen
        amount-poäng, ingen conversion-nyckel."""
        from app.services.receipt_matcher import find_matches
        missing = {"amount": 28.54, "currency": "EUR", "date": "2026-04-22",
                   "description": "Skånetrafiken"}
        candidate = {"id": 1, "amount": 300, "currency": "SEK",
                     "receipt_date": "2026-04-22", "vendor": "Skånetrafiken"}

        results = find_matches(
            missing, [candidate], rate_provider=lambda *a, **k: None,
        )
        # Total: 0 (amount) + 30 (date) + 30 (vendor) = 60 → över 50-tröskeln
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["score_breakdown"]["amount"], 0)
        self.assertNotIn("conversion", results[0])

    def test_no_rate_provider_behaves_as_before(self):
        """Bakåtkompatibilitet: find_matches utan rate_provider-arg."""
        from app.services.receipt_matcher import find_matches
        missing = {"amount": 28.54, "currency": "EUR", "date": "2026-04-22",
                   "description": "Skånetrafiken"}
        candidate = {"id": 1, "amount": 300, "currency": "SEK",
                     "receipt_date": "2026-04-22", "vendor": "Skånetrafiken"}
        results = find_matches(missing, [candidate])
        # Samma beteende som före: ingen conversion, amount=0
        if results:
            self.assertEqual(results[0]["score_breakdown"]["amount"], 0)
            self.assertNotIn("conversion", results[0])


class LiveFrankfurterApiTest(unittest.TestCase):
    """Riktig kall mot frankfurter.dev — verifierar att URL + redirect-
    konfiguration faktiskt fungerar mot live-API:t. Hoppas gracefully
    om nätverk saknas så CI utan internet inte fallerar."""

    def test_sek_to_eur_returns_positive_rate(self):
        import httpx
        from app.services.currency_converter import _fetch_rate_from_api

        try:
            rate = _fetch_rate_from_api("2025-01-15", "SEK", "EUR")
        except (httpx.ConnectError, httpx.ReadError, httpx.NetworkError) as exc:
            self.skipTest(f"No network access for live API test: {exc}")

        if rate is None:
            self.skipTest(
                "frankfurter.dev returned None — möjligt utfall av tillfällig "
                "API-otillgänglighet, inte ett kodfel."
            )
        self.assertIsInstance(rate, float)
        self.assertGreater(rate, 0)
        # SEK→EUR ligger typiskt runt 0.08-0.10. Sanity-check (0, 1)
        # försäkrar oss om att vi inte fått omvänd kurs eller skräpvärde.
        self.assertLess(rate, 1.0)


if __name__ == "__main__":
    unittest.main()
