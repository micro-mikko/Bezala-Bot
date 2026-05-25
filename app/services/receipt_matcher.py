"""Match-algoritm för FAS 5.4 — kortmatchning.

Bezala har en endpoint för korttransaktioner utan kvitto. För varje
saknat kvitto försöker vi hitta matchande ProcessedMessage-rader i
vår DB baserat på belopp, datum och vendor-namn.

Pure functions — inga sidoeffekter, lätt att testa.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


# Tröskel för att ett förslag ska visas i UI:t.
MIN_DISPLAY_SCORE = 50
MAX_SUGGESTIONS_PER_MISSING = 5

# Datum-bonus (Match algorithm 3.0 — utökat fönster för fördröjd
# kortdebitering):
# Bucket-skala (max_days, score) för bästa matchen mellan kort-trans
# och receipt_date / received_at. Korttransaktioner kan komma 7-30+
# dagar efter köpet (parkeringar, transit-bokningar, försenade
# debiteringar) — för att ge belopp+vendor-perfekta matchningar en
# chans att nå tröskel utökar vi fönstret upp till 60 dagar med
# avtagande poäng.
# Match algorithm 3.1: hård cutoff vid >365d — bortom ett år nollas hela
# scoren oavsett andra signaler (se DATE_HARD_CUTOFF_DAYS nedan).
#
# C26: tidigare gav 0-3 dagars diff samma 30 poäng — en exakt datum-match
# kunde därför inte särskiljas från ett kvitto ±1 dag fel. När belopp +
# vendor matchade 100% blev båda kandidaterna 110p och valet godtyckligt
# (bekräftat i prod: Skånetrafiken 14,03 € valde kvitto från fel dag).
# Den första bucketen är nu uppdelad så exakt match (0d) > ±1d > ±2-3d.
# OBS — Finnair-nyans: Finnair-etickets daterar avresedatum, inte
# köpdatum, så stor datumskillnad är legitim. Därför är 4-60d-bucketarna
# medvetet OFÖRÄNDRADE (25/15/10/5) — uppdelningen rör bara 0-3d, och den
# skarpa datum-prioriteringen ligger i sorteringens tie-breaker
# (find_matches) som bara triggar när två kandidater faktiskt konkurrerar.
DATE_BUCKETS: tuple[tuple[int, int], ...] = (
    (0, 30),
    (1, 28),
    (3, 26),
    (7, 25),
    (14, 15),
    (30, 10),
    (60, 5),
)

# Match algorithm 3.1: kvitton mer än ett år från korttransaktionen kan
# aldrig vara den verkliga matchen. Total scoren nollas oavsett belopp +
# vendor — fångar fall som "FINNAIR 2026 vs Finnair-kvitto 2021".
DATE_HARD_CUTOFF_DAYS = 365

# Match algorithm 3.1: vendor-floor. När enda vendor-signalen är
# fuzzy SequenceMatcher med <20% similarity är det inte en match —
# liknande belopp/datum händer av en slump för ofta. Kapas vid 49
# (en under MIN_DISPLAY_SCORE). Alias / substring / override returnerar
# 0.95-1.0 så de påverkas aldrig av floor:en.
VENDOR_FLOOR_SIMILARITY = 0.20
VENDOR_FLOOR_MAX_TOTAL = 49

# Belopp-tolerans: ±5% (valutakurser + avrundning) för samma valuta.
# C30b: scoringen är nu GRADERAD inom 5%-fönstret — se _amount_score nedan.
# AMOUNT_BONUS är max-poäng (exakt match / avrundningsnivå); near-matches
# inom toleransen får lägre poäng så att ett exakt belopp ALLTID rankar
# över ett 3-4% nära-belopp för samma vendor (Finnair-fall 2026-05-22 där
# 554,50-kvittot visade 100% mot 534,49-bankrad pga binär 50p-bonus).
# Yttre gränsen på 5% är OFÖRÄNDRAD — utanför ger fortfarande 0p, och
# cross-currency går genom den separata _amount_matches_via_conversion-
# vägen som ej påverkas.
AMOUNT_TOLERANCE = 0.05
AMOUNT_BONUS = 50

# När vi konverterar via ECB-kurs kräver vi tight ±2%-match (Match
# algorithm 3.1). Tidigare ±10% gav false positives för icke-relaterade
# transaktioner som råkade hamna i samma EUR-storleksordning efter
# konvertering (NOK↔EUR, USD↔EUR). Kursbruset är för stort + slumpen för
# vanlig för att tillåta lös tolerans cross-currency.
AMOUNT_TOLERANCE_CONVERTED = 0.02
AMOUNT_BONUS_CONVERTED = 40

# Vendor-fuzzy: SequenceMatcher → 0..30
VENDOR_BONUS_MAX = 30

# C30b — max möjlig total raw-score: amount (50) + date (30) + vendor (30).
# Används av frontend (via API ELLER hårdkodning) för att normalisera den
# visade procenten så att 100 % faktiskt betyder "perfekt match" och inte
# bara "≥100 efter klamp". Om vikterna ovan ändras måste konstanten + den
# hårdkodade kopian i MatchCandidates.jsx (MAX_RAW_SCORE) uppdateras.
MAX_TOTAL_SCORE = AMOUNT_BONUS + 30 + VENDOR_BONUS_MAX

# Vendor-overrides: missing-receipt-beskrivning (substring) → kanonisk vendor
# som matchas mot ProcessedMessage.vendor. Bygger med erfarenhet av Bezala-
# korttransaktionsformat (versaler, leverantör + suffix).
VENDOR_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("claude.ai", "anthropic"),
    ("anthropic", "anthropic"),
    ("openai", "openai"),
    # Match algorithm 3.1: 'airport lrs' override borttagen — AIRPORT LRS
    # (Stockholm Arlanda P-bolaget) är inte samma vendor som Arlanda Express.
    ("arlandaexpress", "arlanda express"),
    ("uber", "uber"),
    ("finnair", "finnair"),
    ("scandic", "scandic"),
    ("clas ohlson", "clas ohlson"),
    ("moovy", "moovy"),
    ("sl ", "sl"),
    ("skanetraf", "skånetrafiken"),
    ("flytoget", "flytoget"),
    ("strawberry", "strawberry"),
)

# Vendor-aliasing (Match algorithm 3.0):
# Korttransaktionsbeskrivningar är råa kortbeskrivningar (versaler,
# tekniska identifierare) som inte fuzzy-matchar mot Gmail-vendors.
# Mappa kort-vendor → lista av aliaser som kan finnas i
# ProcessedMessage.vendor eller .sender. Träff räknas som lika stark
# som substring (30 poäng / 100% similarity).
# Match algorithm 3.1: alias-nycklar måste vara specifika brand-identifierare
# (fullständiga vendor-namn). Generiska ord som "AIRPORT", "EXPRESS", "BILL"
# triggar false positives mellan orelaterade vendors.
VENDOR_ALIASES: dict[str, list[str]] = {
    "LOVABLE": ["lovable", "lovable.dev"],
    "MJS.LIFE": ["mjs.life", "mjslife"],
    "CIRCLE K OERKELLJUNGA": ["circle k", "circlek"],
    "APPLE.COM/BILL": ["apple", "itunes", "apple.com"],
    # AIRPORT LRS borttagen — generisk "AIRPORT"-nyckel + aliaser
    # ["airport", "lrs"] gav falska träffar mot andra "airport"-vendors.
    "HERTZ SVERIGE": ["hertz"],
    "CURSOR": ["cursor.com", "cursor.sh", "anysphere"],
    "MOOVY": ["moovy", "finavia"],
    "SKANETRAFIKEN APP": ["skanetrafiken", "skånetrafiken"],
    # "arlanda" (utan "express") borttaget — partial-match mot t.ex.
    # "arlanda parking" gav false positives.
    "ARLANDA EXPRESS": ["arlanda express", "arlandaexpress"],
    "FINNAIR": ["finnair", "amadeus", "eticket"],
    "FLYTOGET": ["flytoget"],
    "ANTHROPIC": ["anthropic"],
}


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        # Stöd både 'YYYY-MM-DD' och ISO-datetimes
        if len(raw) == 10:
            return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_vendor(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip().lower())


def _vendor_canonical(missing_description: str) -> str | None:
    """Mappa kort-transaktionsbeskrivning till kanoniskt vendor-namn via
    overrides. Ex: 'CLAUDE.AI SUBSCRIPTION' → 'anthropic'."""
    text = _normalize_vendor(missing_description)
    if not text:
        return None
    for needle, canonical in VENDOR_OVERRIDES:
        if needle in text:
            return canonical
    return None


def alias_match(
    missing_description: str | None,
    candidate_vendor: str | None,
    candidate_sender: str | None = None,
) -> bool:
    """Match algorithm 3.0 — alias-matchning.

    Om någon nyckel i VENDOR_ALIASES förekommer i missing_description
    (case-insensitive) och något av motsvarande aliaser förekommer i
    candidate_vendor eller candidate_sender → True. Annars False.
    """
    if not missing_description:
        return False
    desc_upper = str(missing_description).upper()
    haystack_parts: list[str] = []
    if candidate_vendor:
        haystack_parts.append(str(candidate_vendor).lower())
    if candidate_sender:
        haystack_parts.append(str(candidate_sender).lower())
    if not haystack_parts:
        return False
    haystack = " ".join(haystack_parts)
    for key, aliases in VENDOR_ALIASES.items():
        if key in desc_upper:
            for alias in aliases:
                if alias.lower() in haystack:
                    return True
    return False


def vendor_similarity(
    missing_description: str | None,
    candidate_vendor: str | None,
    candidate_sender: str | None = None,
) -> float:
    """Returnerar 0..1-similarity mellan beskrivning och vendor-namn.

    candidate_sender är valfri — när angiven används den som extra
    haystack för alias-matchning (Match algorithm 3.0).
    """
    a = _normalize_vendor(missing_description)
    b = _normalize_vendor(candidate_vendor)
    # Alias-match (Match algorithm 3.0) — kan trigga även när
    # vendor-fältet i sig är svagt om sender bär signalen.
    if alias_match(missing_description, candidate_vendor, candidate_sender):
        return 1.0
    if not a or not b:
        return 0.0
    # Exact substring → max
    if b in a or a in b:
        return 1.0
    # Override-match räknas som hög likhet
    canonical = _vendor_canonical(a)
    if canonical and canonical in b:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


def _amount_matches(
    missing_amount: float | None,
    candidate_amount: float | None,
    missing_currency: str | None = None,
    candidate_currency: str | None = None,
) -> bool:
    """Direkt amount-match (samma valuta).

    Match algorithm 3.0: blockerar cross-currency match utan konvertering.
    Ex: 100 EUR vs 100 SEK ska INTE matcha även om siffran råkar stämma —
    konvertering måste ske via _amount_matches_via_conversion istället.
    """
    if missing_amount is None or candidate_amount is None:
        return False
    if missing_amount == 0:
        return False
    mc = (missing_currency or "").upper().strip()
    cc = (candidate_currency or "").upper().strip()
    if mc and cc and mc != cc:
        return False
    diff_pct = abs(missing_amount - candidate_amount) / abs(missing_amount)
    return diff_pct <= AMOUNT_TOLERANCE


def _amount_score(
    missing_amount: float | None,
    candidate_amount: float | None,
) -> int:
    """C30b — graderad belopp-poäng inom 5%-fönstret.

    Trappa (vald 50/40/25/0, inte plan-defaulten 50/45/38, motivering nedan):
      - 0–0.5  %  →  50  (exakt / avrundning)
      - 0.5–2  %  →  40  (VAT-/avrundningsskillnad)
      - 2–5    %  →  25  (svag belopp-signal; vendor+datum måste bära mer)
      - > 5    %  →   0  (utanför toleransen — oförändrat)

    Motivering för 50 vs 25 spread (i stället för 50 vs 38 i planen):
    Finnair-falet 2026-05-22 (rapporterat C30):
      bankrad O7CCY63 = 534,49 EUR 2026-05-20
      kvitto A = 554,50 EUR (~3,74 % diff), receipt_date 2026-05-19 (1d off)
      kvitto B = 534,49 EUR (exakt), receipt_date = flygdatum (8–60d off,
                 Finnair etickets daterar avresedatum, ej köpdatum)

    Med plan-trappa 50/45/38:
      A: 38 + 30(vendor) + 28(1d)  = 96
      B: 50 + 30        + 15(8-14d) = 95 — B FÖRLORAR fortfarande
      B: 50 + 30        + 10(15-30d) = 90 — B förlorar

    Med vald 50/40/25:
      A: 25 + 30 + 28 = 83
      B: 50 + 30 + 15 = 95 — B vinner ✓
      B: 50 + 30 + 10 = 90 — B vinner ✓
      B: 50 + 30 +  5 = 85 — B vinner (upp till 60d flyghorisont) ✓

    Cross-currency oförändrat — den vägen går via
    _amount_matches_via_conversion (separat 40p/0p-binär) och triggar
    bara när valutorna skiljer. Same-currency near-matches utanför 5 %
    får fortfarande 0 (totalförbud kvar).

    Returnerar int 0..50."""
    if missing_amount is None or candidate_amount is None:
        return 0
    if missing_amount == 0:
        return 0
    diff_pct = abs(missing_amount - candidate_amount) / abs(missing_amount)

    if diff_pct <= 0.005:
        return AMOUNT_BONUS              # 50
    if diff_pct <= 0.02:
        return 40
    if diff_pct <= AMOUNT_TOLERANCE:     # 0.05
        return 25
    return 0


def _amount_matches_via_conversion(
    missing_amount: float | None,
    missing_currency: str | None,
    candidate_amount: float | None,
    candidate_currency: str | None,
    date_str: str | None,
    rate_provider,
) -> tuple[bool, float | None, float | None]:
    """När kvitto- och kort-valuta skiljer: konvertera kvitto-beloppet
    till kort-valutan via ECB-kurs (närmast missing.date, eller
    candidate.date om missing saknar) och jämför med ±10% tolerans.

    Returnerar (matches, converted_amount, rate). converted_amount är
    candidate_amount i missing_currency (det Bezala-raden visar), som
    UI kan visa som t.ex. "300 SEK ≈ 26.25 EUR"."""
    if (
        missing_amount is None or candidate_amount is None
        or missing_amount == 0 or rate_provider is None
    ):
        return False, None, None
    mc = (missing_currency or "").upper().strip()
    cc = (candidate_currency or "").upper().strip()
    if not mc or not cc or mc == cc:
        return False, None, None
    if not date_str:
        return False, None, None

    rate = rate_provider(date_str, cc, mc)
    if rate is None:
        return False, None, None
    converted = candidate_amount * rate
    diff_pct = abs(missing_amount - converted) / abs(missing_amount)
    return diff_pct <= AMOUNT_TOLERANCE_CONVERTED, converted, rate


def _date_diff_days(a_str: str | None, b_str: str | None) -> int | None:
    """Returnera abs-skillnad i dagar mellan två datum-strängar.

    Stöder både 'YYYY-MM-DD' och fulla ISO-timestamps. Tids-komponenten
    ignoreras (jämförelse på datum-nivå) — viktigt för received_at som
    är en datetime medan kort-trans-date är ett rent datum.
    """
    a = _parse_date(a_str)
    b = _parse_date(b_str)
    if a is None or b is None:
        return None
    return abs((a.date() - b.date()).days)


def _date_score_dual(
    missing_date: str | None,
    receipt_date: str | None,
    received_at: str | None,
) -> tuple[int, str | None, int | None]:
    """FAS 5.12 — föredra receipt_date framför received_at.

    receipt_date är det faktiska kvittodatumet (från PDF/HTML) och är
    alltid mer relevant än received_at (när Gmail tog emot mejlet) när
    båda finns. Tidigare dual-date-logik plockade fältet med minst diff
    mot kortransaktionen, vilket gav fel matched_field i fall som Moovy
    (kvitto 2026-04-16, mejl 2026-04-17, kortrad 2026-04-15) — då vann
    received_at trots att receipt_date är det "sanna" datumet.

    Regel (FAS 5.12):
      - receipt_date finns → använd det
      - annars → fall back till received_at

    Returnerar (score, matched_field, days_off):
      - matched_field: 'receipt_date' | 'received_at' | None
      - None när varken receipt_date eller received_at gav ett datum
        att jämföra med
      - days_off: diff i dagar för det valda fältet (None bara när
        inget fält fanns att jämföra)
    """
    primary = _date_diff_days(missing_date, receipt_date)
    fallback = _date_diff_days(missing_date, received_at)

    if primary is not None:
        best_diff, best_field = primary, "receipt_date"
    elif fallback is not None:
        best_diff, best_field = fallback, "received_at"
    else:
        return 0, None, None

    for threshold, score in DATE_BUCKETS:
        if best_diff <= threshold:
            return score, best_field, best_diff
    # Bortom största bucket — ingen meningsfull match. matched_field=None
    # signalerar UI att visa varningstext istället för ✓.
    return 0, None, best_diff


def _date_score(missing_date: str | None, candidate_date: str | None) -> int:
    """Bakåtkompatibilitet: en-datum-API. Använd _date_score_dual för
    full kontext (matched_field + days_off)."""
    score, _, _ = _date_score_dual(missing_date, candidate_date, None)
    return score


def score_match(
    missing: dict,
    candidate: dict,
    *,
    rate_provider=None,
) -> dict:
    """Räkna ut total score 0..110+ för en kandidat mot ett saknat kvitto.

    missing: {amount, currency, date, description}
    candidate: ProcessedMessage-fält (amount, currency, receipt_date, vendor)
    rate_provider: valfri callable (date, from, to) → rate|None som
        möjliggör cross-currency-matchning via ECB-kurs.

    Returnerar {total, breakdown: {amount, date, vendor}, conversion?:
    {from_amount, from_currency, to_amount, to_currency, rate, date}}."""
    breakdown = {"amount": 0, "date": 0, "vendor": 0}
    conversion: dict | None = None

    # Match algorithm 3.1 — hård date-cutoff. Beräkna best date-diff från
    # de tillgängliga date-fälten och avbryt om bortom DATE_HARD_CUTOFF_DAYS.
    _diffs = [
        d for d in (
            _date_diff_days(missing.get("date"), candidate.get("receipt_date")),
            _date_diff_days(missing.get("date"), candidate.get("received_at")),
        ) if d is not None
    ]
    if _diffs and min(_diffs) > DATE_HARD_CUTOFF_DAYS:
        breakdown["date_matched_field"] = None
        breakdown["date_days_off"] = min(_diffs)
        return {
            "total": 0,
            "breakdown": breakdown,
            "rejected_reason": "date_too_far",
        }

    # C30b — same-currency: graderad amount-poäng (50/40/25/0).
    # Different currency: gå genom konverteringsvägen (oförändrad binär
    # 40p/0p med ±2 %-tolerans). Currency-mismatch utan rate_provider
    # ger 0p precis som tidigare.
    m_cur = (missing.get("currency") or "").upper().strip()
    c_cur = (candidate.get("currency") or "").upper().strip()
    same_or_unknown_currency = (
        not m_cur or not c_cur or m_cur == c_cur
    )
    if same_or_unknown_currency:
        breakdown["amount"] = _amount_score(
            missing.get("amount"), candidate.get("amount"),
        )
    elif rate_provider is not None:
        matches, converted, rate = _amount_matches_via_conversion(
            missing.get("amount"), missing.get("currency"),
            candidate.get("amount"), candidate.get("currency"),
            missing.get("date"), rate_provider,
        )
        if matches and converted is not None and rate is not None:
            breakdown["amount"] = AMOUNT_BONUS_CONVERTED
            conversion = {
                "from_amount": candidate.get("amount"),
                "from_currency": (candidate.get("currency") or "").upper(),
                "to_amount": round(converted, 2),
                "to_currency": (missing.get("currency") or "").upper(),
                "rate": rate,
                "date": missing.get("date"),
            }

    date_score, matched_field, days_off = _date_score_dual(
        missing.get("date"),
        candidate.get("receipt_date"),
        candidate.get("received_at"),
    )
    breakdown["date"] = date_score
    breakdown["date_matched_field"] = matched_field
    breakdown["date_days_off"] = days_off

    sim = vendor_similarity(
        missing.get("description"),
        candidate.get("vendor"),
        candidate.get("sender"),
    )
    breakdown["vendor"] = int(round(sim * VENDOR_BONUS_MAX))

    total = breakdown["amount"] + breakdown["date"] + breakdown["vendor"]

    # Match algorithm 3.1 — vendor-floor. Utan vendor-signal är liknande
    # belopp+datum oftast slump (Hertz Sverige 108.92 EUR ↔ Anthropic
    # 112.95 EUR). Kapa total under display-tröskel. Alias/substring/
    # override returnerar sim 0.95-1.0 så de är immuna.
    if sim < VENDOR_FLOOR_SIMILARITY and total > VENDOR_FLOOR_MAX_TOTAL:
        total = VENDOR_FLOOR_MAX_TOTAL

    result: dict = {"total": total, "breakdown": breakdown}
    if conversion is not None:
        result["conversion"] = conversion
    return result


def _rank_key(entry: dict) -> tuple[int, int]:
    """Sorteringsnyckel för kandidater (lägre = bättre).

    C26 tie-breaker: primärt högsta score, sekundärt minst datumskillnad.
    När två kandidater har samma vendor + belopp (vanligt: Skånetrafiken /
    Finnair) hamnar de ofta på exakt samma totalpoäng — utan tie-breaker
    blev valet godtyckligt (stabil sort → inmatningsordning). Nu vinner
    alltid kandidaten närmast bankradens datum.

    date_days_off kan vara None när inget datumfält fanns att jämföra —
    de sorteras sist bland kandidater med samma score.
    """
    days_off = entry["score_breakdown"].get("date_days_off")
    return (-entry["score"], days_off if days_off is not None else 10**9)


def find_matches(
    missing: dict,
    candidates: list[dict],
    *,
    rate_provider=None,
) -> list[dict]:
    """För ett saknat kvitto: returnera top N kandidater över tröskeln,
    sorterat på score desc. Vid lika score avgör minst datumskillnad mot
    bankraden (C26 tie-breaker). rate_provider möjliggör cross-currency-
    matchning (None → bara samma-valuta-jämförelser som tidigare)."""
    scored: list[dict] = []
    for cand in candidates:
        s = score_match(missing, cand, rate_provider=rate_provider)
        if s["total"] >= MIN_DISPLAY_SCORE:
            entry = {
                "message": cand,
                "score": s["total"],
                "score_breakdown": s["breakdown"],
            }
            if "conversion" in s:
                entry["conversion"] = s["conversion"]
            scored.append(entry)
    scored.sort(key=_rank_key)
    return scored[:MAX_SUGGESTIONS_PER_MISSING]
