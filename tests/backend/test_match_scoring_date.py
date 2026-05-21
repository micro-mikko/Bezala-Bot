"""C26 — AI MATCH date-prioritering.

Regressionsskydd för buggen där AI MATCH valde fel kvitto när två
kandidater hade samma vendor + belopp men olika datum (±1-2 dagar).
Bekräftat i prod tre gånger (Skånetrafiken/Finnair).

Två kompletterande mekanismer testas:
  A) Graderade datum-buckets — exakt datum-match (0d) > ±1d > ±2-3d.
  B) Tie-breaker i find_matches — vid lika totalpoäng vinner kandidaten
     närmast bankradens datum.

Finnair-nyansen testas separat: stor (legitim) datumskillnad ska
fortfarande matcha bra och 4-7d-bucketen är oförändrad.

Rena pure-function-tester — ingen DB, inga env-beroenden.
"""

from __future__ import annotations

import unittest

from app.services.receipt_matcher import (
    MIN_DISPLAY_SCORE,
    find_matches,
    score_match,
)


def make_bill_line(*, date, amount, vendor, currency="EUR"):
    """Bankrad (Bezala missing receipt) — kort-transaktionens beskrivning
    bär vendor-namnet på Bezala-kortformat (versaler)."""
    return {
        "amount": amount,
        "currency": currency,
        "date": date,
        "description": f"MIKKO KEINONEN: {vendor.upper()} APP, MALMO, SE {amount} {currency}",
    }


def make_receipt(*, date, amount, vendor, currency="EUR", rid=None):
    """Kvitto-kandidat (ProcessedMessage-fält)."""
    return {
        "id": rid,
        "amount": amount,
        "currency": currency,
        "receipt_date": date,
        "vendor": vendor,
    }


class ExactDatePriorityTest(unittest.TestCase):
    """A) Graderade datum-buckets."""

    def test_exact_date_beats_one_day_off(self):
        """Två kandidater, samma vendor + belopp, olika datum.
        Den med exakt datum-match ska ranka högst."""
        bill = make_bill_line(date="2026-05-19", amount=14.03, vendor="Skanetrafiken")
        candidate_exact = make_receipt(
            date="2026-05-19", amount=14.03, vendor="Skånetrafiken", rid="exact",
        )
        candidate_off = make_receipt(
            date="2026-05-20", amount=14.03, vendor="Skånetrafiken", rid="off",
        )

        ranked = find_matches(bill, [candidate_off, candidate_exact])

        self.assertEqual(ranked[0]["message"]["id"], "exact")

    def test_exact_date_breakdown_higher_than_one_day_off(self):
        """C26 root cause: tidigare gav 0d och ±1d samma 30p. Nu ska
        exakt match ge strikt mer datum-poäng än ±1 dag."""
        bill = make_bill_line(date="2026-05-19", amount=14.03, vendor="Skanetrafiken")
        exact = score_match(
            bill, make_receipt(date="2026-05-19", amount=14.03, vendor="Skånetrafiken"),
        )
        one_off = score_match(
            bill, make_receipt(date="2026-05-20", amount=14.03, vendor="Skånetrafiken"),
        )

        self.assertEqual(exact["breakdown"]["date"], 30)
        self.assertEqual(one_off["breakdown"]["date"], 28)
        self.assertGreater(exact["total"], one_off["total"])

    def test_skanetrafiken_14_03_prod_case(self):
        """Prod-case 1: bankrad Skånetrafiken 14,03 € (2026-05-19) valde
        kvitto från 2026-05-20. Rätt kvitto är 2026-05-19."""
        bill = make_bill_line(date="2026-05-19", amount=14.03, vendor="Skanetrafiken")
        wrong = make_receipt(
            date="2026-05-20", amount=14.03, vendor="Skånetrafiken", rid="wrong",
        )
        correct = make_receipt(
            date="2026-05-19", amount=14.03, vendor="Skånetrafiken", rid="correct",
        )

        ranked = find_matches(bill, [wrong, correct])

        self.assertEqual(ranked[0]["message"]["id"], "correct")

    def test_skanetrafiken_23_22_prod_case(self):
        """Prod-case 2: bankrad Skånetrafiken 23,22 € (2026-05-07) valde
        kvitto från 2026-05-08."""
        bill = make_bill_line(date="2026-05-07", amount=23.22, vendor="Skanetrafiken")
        wrong = make_receipt(
            date="2026-05-08", amount=23.22, vendor="Skånetrafiken", rid="wrong",
        )
        correct = make_receipt(
            date="2026-05-07", amount=23.22, vendor="Skånetrafiken", rid="correct",
        )

        ranked = find_matches(bill, [wrong, correct])

        self.assertEqual(ranked[0]["message"]["id"], "correct")


class FinnairNuanceTest(unittest.TestCase):
    """Finnair-nyans: e-ticket daterar avresedatum, inte köpdatum, så
    stor datumskillnad mellan bankrad och kvitto är NORMAL och får inte
    straffas hårt."""

    def test_finnair_large_date_gap_still_matches(self):
        """Finnair: bankrad-datum (köp) långt före kvitto-datum (avresa).
        Ska fortfarande matcha bra trots stor datumskillnad."""
        bill = make_bill_line(date="2026-04-24", amount=366.32, vendor="Finnair")
        candidate = make_receipt(date="2026-04-30", amount=366.32, vendor="Finnair")

        result = score_match(bill, candidate)

        self.assertGreaterEqual(result["total"], MIN_DISPLAY_SCORE)

    def test_finnair_4_7_day_window_unchanged_by_c26(self):
        """C26 lovar att 4-7d-bucketen är oförändrad (25p) så Finnair-
        matchningar inte får sämre score. 6 dagars skillnad → 25p."""
        bill = make_bill_line(date="2026-04-24", amount=366.32, vendor="Finnair")
        result = score_match(
            bill, make_receipt(date="2026-04-30", amount=366.32, vendor="Finnair"),
        )

        self.assertEqual(result["breakdown"]["date_days_off"], 6)
        self.assertEqual(result["breakdown"]["date"], 25)


class TieBreakerTest(unittest.TestCase):
    """B) Tie-breaker: vid lika totalpoäng vinner närmaste datum."""

    def test_tie_breaker_picks_closest_date(self):
        """Två kandidater med identisk vendor + belopp landar i samma
        datum-bucket (2 resp. 3 dagar → båda 26p) → exakt samma totalpoäng.
        Tie-breaker ska ranka kandidaten närmast bankradens datum först."""
        bill = make_bill_line(date="2026-05-10", amount=14.03, vendor="Skanetrafiken")
        closer = make_receipt(
            date="2026-05-12", amount=14.03, vendor="Skånetrafiken", rid="closer",
        )
        farther = make_receipt(
            date="2026-05-13", amount=14.03, vendor="Skånetrafiken", rid="farther",
        )

        ranked = find_matches(bill, [farther, closer])

        # Förutsättning: scoren är faktiskt lika (annars testar vi inte
        # tie-breakern utan bara A-mekanismen).
        self.assertEqual(ranked[0]["score"], ranked[1]["score"])
        self.assertEqual(ranked[0]["message"]["id"], "closer")

    def test_tie_breaker_deterministic_regardless_of_input_order(self):
        """Tie-breakern är deterministisk — närmaste datum vinner oavsett
        kandidaternas inmatningsordning (en stabil sort utan tie-breaker
        hade behållit inmatningsordningen)."""
        bill = make_bill_line(date="2026-05-10", amount=14.03, vendor="Skanetrafiken")
        closer = make_receipt(
            date="2026-05-12", amount=14.03, vendor="Skånetrafiken", rid="closer",
        )
        farther = make_receipt(
            date="2026-05-13", amount=14.03, vendor="Skånetrafiken", rid="farther",
        )

        ranked_a = find_matches(bill, [closer, farther])
        ranked_b = find_matches(bill, [farther, closer])

        self.assertEqual(ranked_a[0]["message"]["id"], "closer")
        self.assertEqual(ranked_b[0]["message"]["id"], "closer")


if __name__ == "__main__":
    unittest.main()
