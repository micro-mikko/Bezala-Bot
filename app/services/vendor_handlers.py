"""Vendor-specifika kvitto-hämtare.

Vissa avsändare skickar mail som innehåller BÅDE en bilaga och en länk:
  - bilaga = reseunderlag (biljett, boarding pass, QR-kod)
  - länk i body = faktiskt kvitto med pris/moms/betalmetod

Den vanliga pipelinen plockar bilagan, men för Bezala-attestering är det
KVITTOT vi behöver (det är där momsen står). För dessa vendors hämtar vi
kvittot via länken och använder den PDF:en istället för bilagan.

Vi går med vendor-SPECIFIKA handlers, inte en generell heuristik som
"om bodyn innehåller 'kvitto' + länk → hämta". Det är säkrare:

  - Vi validerar att länken pekar mot samma domän som avsändaren
    (skydd mot manipulerade kvitto-länkar i mail-bodyn)
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
    allowed_domain_suffix: kvitto-länken måste peka mot denna domän (eller
        en subdomän) — annars hoppas vi. Skydd mot phishing-länkar i
        kapade mail.
    """
    name: str
    sender_substring: str
    allowed_domain_suffix: str


# Registry. Lägg till fler entries här när nya vendors identifierats.
_HANDLERS: tuple[LinkReceiptHandler, ...] = (
    LinkReceiptHandler(
        name="Arlanda Express",
        sender_substring="@arlandaexpress.se",
        allowed_domain_suffix="arlandaexpress.se",
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


def _link_matches_allowed_domain(url: str, allowed_suffix: str) -> bool:
    """True om URL:en är HTTPS och hostnamnet är exakt allowed_suffix
    eller en sub-domän av den. http:// avvisas alltid (kvitton går över TLS)."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    suffix = allowed_suffix.lower()
    return host == suffix or host.endswith("." + suffix)


def fetch_link_receipt_for_message(
    msg: GmailMessage,
    *,
    _fetcher: Callable[[str], bytes] | None = None,
) -> bytes | None:
    """Returnera PDF-bytes från en vendor-specifik kvitto-länk om handler
    matchar och hämtning lyckas. Returnerar None i alla andra fall:

      - Avsändaren matchar ingen handler
      - Ingen kvitto-länk hittades i mail-bodyn
      - Länken pekar mot en oväntad domän (säkerhetsskydd)
      - HTTP-hämtning failade (timeout, 404, content-type, magic bytes)

    Pipelinen ska då falla tillbaka på den vanliga bilage-baserade vägen
    — bilagan är inte rätt underlag, men bättre än ingen logg alls.

    `_fetcher` är en injection-point för tester — anropas med URL,
    returnerar PDF-bytes eller raisar LinkFetchError.
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

    if not _link_matches_allowed_domain(url, handler.allowed_domain_suffix):
        logger.warning(
            "vendor_handler %s: länk %r matchar inte tillåten domän %s — "
            "ignoreras (möjligen phishing eller fel-extraherad länk)",
            handler.name, url, handler.allowed_domain_suffix,
        )
        return None

    fetcher = _fetcher if _fetcher is not None else fetch_pdf_from_link
    try:
        pdf_bytes = fetcher(url)
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

    logger.info(
        "vendor_handler %s: använder länk-kvitto från %s (%d bytes) "
        "istället för mail-bilaga för %s",
        handler.name, url, len(pdf_bytes), msg.message_id,
    )
    return pdf_bytes
