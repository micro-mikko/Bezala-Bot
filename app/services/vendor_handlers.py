"""Vendor-specifika kvitto-hämtare.

Vissa avsändare skickar mail som innehåller BÅDE en bilaga och en länk:
  - bilaga = reseunderlag (biljett, boarding pass, QR-kod)
  - länk i body = faktiskt kvitto med pris/moms/betalmetod

Den vanliga pipelinen plockar bilagan, men för Bezala-attestering är det
KVITTOT vi behöver (det är där momsen står). För dessa vendors hämtar vi
kvittot via länken och använder den PDF:en istället för bilagan.

Vi går med vendor-SPECIFIKA handlers, inte en generell heuristik som
"om bodyn innehåller 'kvitto' + länk → hämta". Det är säkrare:

  - Vi validerar att FINAL URL (efter följda redirects) pekar mot
    vendor-domänen — skydd mot manipulerade kvitto-länkar i mail-bodyn
    OCH mot hijackade redirector-konton (om någon stjäl Arlandas
    SendGrid-konto och pekar redirect mot evil.com → final URL ≠
    arlandaexpress.se → vi rejectar)
  - Entry-URLen får vara en click-tracker (SendGrid, Mailgun, etc.) —
    avsändarna använder dem för bounce/open-spårning, kan inte skippas
  - Falskpositiva i en allmän heuristik kan dra in fel länkar
    (avbokningssidor, "betala igen"-länkar, etc.)

Just nu finns endast Arlanda Express. Lägg till fler när de identifierats
i prod (se C37-rapport för spaningslista).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from app.services.gmail_client import GmailMessage
from app.services.link_extractor import extract_receipt_link
from app.services.link_fetcher import LinkFetchError, fetch_pdf_from_link

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LinkReceiptHandler:
    """En vendor vars länk-PDF ska användas istället för mail-bilagan.

    sender_substring: case-insensitive substring som matchas mot msg.sender
    allowed_domain_suffixes: FINAL URL (efter följda redirects) måste peka
        mot någon av dessa domäner eller en subdomän — annars rejectar vi
        PDFen. Entry-URLen får vara vad som helst som är HTTPS (click-
        trackers som SendGrid är normalt för transaktionsmail). Tuple för
        att vendor-företag ibland hostar kvitton på operatör-domän (t.ex.
        Arlanda Express → A-Train AB → atrain.se).
    """
    name: str
    sender_substring: str
    allowed_domain_suffixes: tuple[str, ...]


# Registry. Lägg till fler entries här när nya vendors identifierats.
#
# Arlanda Express drivs av A-Train AB — mailen kommer från
# noreply@arlandaexpress.se men kvitto-PDFer hostas på api.atrain.se
# (verifierat i prod 2026-05-27: SendGrid-redirect landar där). Båda
# domänerna allowlistas så final-URL-checken accepterar atrain-PDFer.
_HANDLERS: tuple[LinkReceiptHandler, ...] = (
    LinkReceiptHandler(
        name="Arlanda Express",
        sender_substring="@arlandaexpress.se",
        allowed_domain_suffixes=("arlandaexpress.se", "atrain.se"),
    ),
)


def _find_handler(sender: str | None) -> LinkReceiptHandler | None:
    if not sender:
        return None
    lower = sender.lower()
    for h in _HANDLERS:
        if h.sender_substring.lower() in lower:
            return h
    return None


def _is_https(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    return parsed.scheme == "https"


def _host_matches_any_suffix(url: str, allowed_suffixes: tuple[str, ...]) -> bool:
    """True om hostnamnet är exakt någon allowed_suffix eller en sub-domän
    av någon. Tom tuple → alltid False."""
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    host = (parsed.hostname or "").lower()
    for suffix in allowed_suffixes:
        s = suffix.lower()
        if host == s or host.endswith("." + s):
            return True
    return False


def fetch_link_receipt_for_message(
    msg: GmailMessage,
    *,
    _fetcher: Callable[[str], tuple[bytes, str]] | None = None,
) -> bytes | None:
    """Returnera PDF-bytes från en vendor-specifik kvitto-länk om handler
    matchar och hämtning lyckas. Returnerar None i alla andra fall:

      - Avsändaren matchar ingen handler
      - Ingen kvitto-länk hittades i mail-bodyn
      - Entry-URLen är inte HTTPS (kvitton går alltid över TLS)
      - HTTP-hämtning failade (timeout, 404, content-type, magic bytes)
      - FINAL URL (efter följda redirects) är inte HTTPS, eller pekar mot
        en oväntad domän — skydd mot hijackade click-trackers

    Pipelinen ska då falla tillbaka på den vanliga bilage-baserade vägen
    — bilagan är inte rätt underlag, men bättre än ingen logg alls.

    `_fetcher` är en injection-point för tester — anropas med URL,
    returnerar (pdf_bytes, final_url) eller raisar LinkFetchError.
    """
    handler = _find_handler(msg.sender)
    if handler is None:
        return None

    url = extract_receipt_link(msg.body_text, msg.body_html)
    if not url:
        logger.info(
            "vendor_handler %s: ingen kvitto-länk hittades i %s — "
            "faller tillbaka på bilaga",
            handler.name, msg.message_id,
        )
        return None

    # Entry-URL: tillåt vilken HTTPS-domän som helst (SendGrid/Mailgun
    # click-trackers är normalt). Final-URL valideras efter fetch.
    if not _is_https(url):
        logger.warning(
            "vendor_handler %s: entry-länk %r är inte HTTPS — ignoreras",
            handler.name, url,
        )
        return None

    fetcher = _fetcher if _fetcher is not None else fetch_pdf_from_link
    try:
        pdf_bytes, final_url = fetcher(url)
    except LinkFetchError as exc:
        logger.warning(
            "vendor_handler %s: hämtning av %s failade (%s) — "
            "faller tillbaka på bilaga",
            handler.name, url, exc.message,
        )
        return None
    except Exception:  # noqa: BLE001 — pipelinen får inte krascha här
        logger.exception(
            "vendor_handler %s: oväntat fel vid hämtning av %s — "
            "faller tillbaka på bilaga",
            handler.name, url,
        )
        return None

    if not _is_https(final_url):
        logger.warning(
            "vendor_handler %s: redirect-kedjan landade på icke-HTTPS URL "
            "%r — rejectar (möjlig downgrade-attack)",
            handler.name, final_url,
        )
        return None

    if not _host_matches_any_suffix(final_url, handler.allowed_domain_suffixes):
        logger.warning(
            "vendor_handler %s: final URL %r matchar ingen tillåten domän "
            "%s (entry var %r) — rejectar och faller tillbaka på bilaga",
            handler.name, final_url, handler.allowed_domain_suffixes, url,
        )
        return None

    logger.info(
        "vendor_handler %s: använder länk-kvitto från %s (final=%s, "
        "%d bytes) istället för mail-bilaga för %s",
        handler.name, url, final_url, len(pdf_bytes), msg.message_id,
    )
    return pdf_bytes
