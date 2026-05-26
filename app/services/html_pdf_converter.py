"""HTML → PDF konvertering för mail där själva mailet är kvittot.

Används av pipeline när ett mail saknar PDF-bilaga men avsändaren INTE
ligger i link_fetch_senders — t.ex. Moovy och Skånetrafiken som lägger
hela kvittot i mail-bodyn. Konverteras med weasyprint.

C35 — image_to_pdf används av match-to-bezala-flödet för manuellt
uppladdade JPG/PNG-kvitton som behöver bli PDF innan de skickas till
Bezala (som bara accepterar application/pdf).
"""

from __future__ import annotations

import base64
import logging

logger = logging.getLogger(__name__)


class HtmlToPdfError(RuntimeError):
    """Konverteringen misslyckades — pipeline loggar 'html_pdf_failed'."""


_BASE_CSS = """
@page { size: A4; margin: 18mm; }
body { font-family: 'Helvetica', 'Arial', sans-serif; font-size: 11pt;
       line-height: 1.4; color: #111; }
img { max-width: 100%; }
table { border-collapse: collapse; }
pre, code { font-family: 'Courier New', monospace; white-space: pre-wrap; }
"""


def _wrap_plain_text(text: str) -> str:
    safe = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f"<html><body><pre>{safe}</pre></body></html>"


def _runtime_diagnostics() -> dict:
    """Runtime-info som är identisk per-process men kan skilja mellan
    uvicorn-workers och APScheduler-tråden. Loggas vid weasyprint-
    kraschar så vi kan jämföra miljön när det fungerar vs inte."""
    import ctypes.util
    import os
    import sys
    import threading
    return {
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
        "python": sys.executable,
        "ld_library_path": os.environ.get("LD_LIBRARY_PATH", ""),
        "find_gobject": ctypes.util.find_library("gobject-2.0"),
        "find_pango": ctypes.util.find_library("pango-1.0"),
        "find_cairo": ctypes.util.find_library("cairo"),
    }


def _html_diagnostics(source: str) -> dict:
    """Snabb struktur-diagnostik för Railway-loggen: ger dev en känsla
    för vad weasyprint fick in utan att klistra in hela HTML:en."""
    low = source.lower()
    return {
        "len": len(source),
        "head": source[:400],
        "link_rel_stylesheet": low.count("<link"),
        "style_tags": low.count("<style"),
        "img_tags": low.count("<img"),
        "external_img_https": low.count('src="https://'),
        "external_img_http": low.count('src="http://'),
        "script_tags": low.count("<script"),
        "svg_tags": low.count("<svg"),
        "has_doctype": "<!doctype" in low,
    }


def html_to_pdf(html: str | None, *, plain_text_fallback: str | None = None) -> bytes:
    """Returnerar PDF-bytes. Föredrar HTML; faller tillbaka på plain text.

    Höjer HtmlToPdfError om weasyprint saknas eller konverteringen kraschar.
    """
    source = (html or "").strip()
    used_fallback = False
    if not source and plain_text_fallback:
        source = _wrap_plain_text(plain_text_fallback)
        used_fallback = True
    if not source:
        raise HtmlToPdfError("Tomt mail — varken HTML eller text att konvertera.")

    try:
        from weasyprint import CSS, HTML  # importera lazily så testerna kan mocka
    except ImportError as exc:
        raise HtmlToPdfError(
            "weasyprint är inte installerat — kan inte konvertera HTML till PDF."
        ) from exc

    try:
        pdf = HTML(string=source).write_pdf(stylesheets=[CSS(string=_BASE_CSS)])
    except Exception as exc:  # noqa: BLE001 — weasyprint kastar olika typer
        diag = _html_diagnostics(source)
        runtime = _runtime_diagnostics()
        logger.exception(
            "HTML→PDF-konvertering misslyckades — fallback=%s exc_type=%s "
            "runtime=%s diag=%s",
            used_fallback, type(exc).__name__, runtime, diag,
        )
        raise HtmlToPdfError(
            f"HTML→PDF kraschade ({type(exc).__name__}) pid={runtime['pid']} "
            f"thread={runtime['thread']} find_gobject={runtime['find_gobject']!r}: {exc}"
        ) from exc

    if not pdf or not pdf.startswith(b"%PDF"):
        raise HtmlToPdfError("HTML→PDF returnerade inte giltig PDF.")
    return pdf


_IMAGE_PDF_CSS = """
@page { size: A4; margin: 12mm; }
body { margin: 0; padding: 0; }
img { display: block; max-width: 100%; max-height: 100%;
      margin: 0 auto; object-fit: contain; }
"""


def image_to_pdf(image_bytes: bytes, mime_type: str) -> bytes:
    """C35 — konvertera ett JPG/PNG till en PDF som Bezala accepterar.

    Manuellt uppladdade kvitton kan vara fotograferade kvitton (JPG/PNG)
    eftersom analoga kvitton inte finns som PDF. Bezalas attach_file
    accepterar endast application/pdf — vi bäddar därför in bilden i en
    enkel HTML och kör samma weasyprint-pipeline som html_to_pdf.

    Höjer HtmlToPdfError om bilden saknas, mime-typen är okänd eller
    weasyprint inte kunde rendera.
    """
    if not image_bytes:
        raise HtmlToPdfError("image_to_pdf: tom bild")
    mt = (mime_type or "").lower().strip()
    if mt == "image/jpg":
        mt = "image/jpeg"
    if mt not in ("image/jpeg", "image/png"):
        raise HtmlToPdfError(
            f"image_to_pdf: okänd mime-typ {mime_type!r} (stödjer jpeg/png)"
        )

    b64 = base64.b64encode(image_bytes).decode("ascii")
    html = (
        f"<html><body><img src=\"data:{mt};base64,{b64}\" "
        f"alt=\"manuellt kvitto\"/></body></html>"
    )

    try:
        from weasyprint import CSS, HTML
    except ImportError as exc:
        raise HtmlToPdfError(
            "weasyprint är inte installerat — kan inte konvertera bild till PDF."
        ) from exc

    try:
        pdf = HTML(string=html).write_pdf(
            stylesheets=[CSS(string=_IMAGE_PDF_CSS)]
        )
    except Exception as exc:  # noqa: BLE001
        runtime = _runtime_diagnostics()
        logger.exception(
            "image→PDF-konvertering misslyckades — exc_type=%s runtime=%s "
            "image_bytes=%d mime=%s",
            type(exc).__name__, runtime, len(image_bytes), mt,
        )
        raise HtmlToPdfError(
            f"image→PDF kraschade ({type(exc).__name__}) pid={runtime['pid']} "
            f"thread={runtime['thread']}: {exc}"
        ) from exc

    if not pdf or not pdf.startswith(b"%PDF"):
        raise HtmlToPdfError("image→PDF returnerade inte giltig PDF.")
    return pdf
