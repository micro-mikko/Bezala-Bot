"""Scanning-pipeline med 3-lagers dubblettskydd.

Flöde per mail:
  1. Kolla att message_id inte redan finns i DB (lager 2).
  2. Hämta meddelandet, iterera bilagor.
  3. Validera PDF (magic bytes).
  4. Claude namnger filen.
  5. Kolla att (filnamn, datum) inte redan finns i DB (lager 3).
  6. Kolla att filnamnet inte redan finns i Drive-mappen.
  7. Ladda upp till Drive.
  8. Logga i DB (ProcessedMessage + SavedFile).
  9. Sätt etiketten 'Bezala-Klar' på mailet (lager 1).

FAS 8: innan analyzer.analyze hämtas senaste användar-rättelser från
ai_feedback-tabellen och bifogas Claude-prompten som few-shot. Tomt
om tabellen är tom eller fetch failar — analysen ska aldrig blockeras.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from app.db import session_scope
from app.models import ProcessedMessage, SavedFile, ScanRun
from app.services.ai_namer import FileNamer
from app.services.bezala_client import BezalaClient, BezalaError
from app.services.bezala_field_mapper import build_receipt_params
from app.services.drive_client import DriveClient
from app.services.gmail_client import Attachment, DONE_LABEL, GmailClient, GmailMessage
from app.services.html_pdf_converter import HtmlToPdfError, html_to_pdf
from app.services.pdf_validator import looks_like_pdf
from app.services.receipt_analyzer import (
    AnalyzerError,
    ReceiptAnalysis,
    ReceiptAnalyzer,
)
from app.services.link_extractor import extract_receipt_link
from app.services.vendor_handlers import fetch_link_receipt_for_message
from app.services.settings_service import (
    build_gmail_query,
    build_gmail_query_html_only,
    load_settings,
    sender_matches_link_fetch,
    subject_matches_exclusion,
)

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    found: int = 0
    processed: int = 0
    skipped: int = 0
    errors: int = 0
    notes: list[str] = None
    # Gate 1.5 — full detalj om varje mail som INTE sparades (inkl. sparade
    # kvitton som filtrerades av AI eller var duplicates). Serialiseras
    # till scan_runs.filtered_messages.
    filtered: list[dict] = None

    def __post_init__(self):
        if self.notes is None:
            self.notes = []
        if self.filtered is None:
            self.filtered = []


# Låst reason-enum (speglas i frontend-i18n)
FILTERED_REASON_AI_FILTERED = "ai_filtered"
FILTERED_REASON_NOT_RECEIPT = "not_receipt"
FILTERED_REASON_NO_PDF = "no_pdf"
FILTERED_REASON_NO_CONTENT = "no_content"
FILTERED_REASON_HTML_PDF_FAILED = "html_pdf_failed"
FILTERED_REASON_EXCLUDED_SUBJECT = "excluded_subject"
FILTERED_REASON_NO_LINK = "no_link"
FILTERED_REASON_ALREADY_PROCESSED = "already_processed"
FILTERED_REASON_FILENAME_ALREADY_SAVED = "filename_already_saved"
FILTERED_REASON_DRIVE_FILENAME_EXISTS = "drive_filename_exists"
FILTERED_REASON_INTEGRITY_RACE = "integrity_race"


def _record_filtered(
    result: "ScanResult",
    msg: "GmailMessage | None",
    reason: str,
    *,
    message_id: str | None = None,
    confidence: int | None = None,
    detail: str | None = None,
) -> None:
    """Lägg till en entry i result.filtered för Log-vyn."""
    received_at = None
    if msg is not None and getattr(msg, "received_at", None):
        received_at = msg.received_at.isoformat()
    result.filtered.append({
        "message_id": (msg.message_id if msg is not None else message_id) or "",
        "sender": (msg.sender if msg is not None else None),
        "subject": (msg.subject if msg is not None else None),
        "received_at": received_at,
        "reason": reason,
        "confidence": confidence,
        "detail": detail,
    })


def _message_already_processed(db, message_id: str) -> bool:
    return (
        db.query(ProcessedMessage.id)
        .filter(ProcessedMessage.message_id == message_id)
        .first()
        is not None
    )


def _filename_already_saved(db, filename: str, date_str: str) -> bool:
    return (
        db.query(SavedFile.id)
        .filter(SavedFile.file_name == filename, SavedFile.file_date == date_str)
        .first()
        is not None
    )


def _fetch_few_shot_for_sender(sender: str | None) -> list[dict]:
    """Hämta few-shot-exempel inom en kortlivad session. Säkert att
    kalla även om feedback-tabellen är tom eller saknas."""
    try:
        from app.services.feedback import get_examples_for_sender
        with session_scope() as db:
            return get_examples_for_sender(db, sender, limit=10)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Few-shot fetch misslyckades för sender=%r — fortsätter utan",
            sender,
        )
        return []


def _fetch_not_receipt_for_sender(sender: str | None) -> list[dict]:
    """FAS 8.1 — hämta not_a_receipt-exempel inom en kortlivad session.
    Användaren har tidigare markerat liknande mail som icke-kvitto.
    Säkert att kalla även om tabellen är tom — returnerar [] vid fel."""
    try:
        from app.services.feedback import get_not_receipt_examples
        with session_scope() as db:
            return get_not_receipt_examples(db, sender, limit=5)
    except Exception:  # noqa: BLE001
        logger.exception(
            "not_a_receipt fetch misslyckades för sender=%r — fortsätter utan",
            sender,
        )
        return []


def run_scan(max_results: int = 50) -> ScanResult:
    """Kör en scanning-omgång. Skapar en ScanRun-rad och returnerar resultatet."""

    result = ScanResult()

    with session_scope() as db:
        app_settings = load_settings(db)
        excluded_subjects = list(app_settings.exclude_subjects or [])
        ai_enabled = bool(app_settings.ai_naming_enabled)
        auto_upload = bool(app_settings.auto_upload_enabled)
        confidence_threshold = int(app_settings.confidence_threshold or 0)
        ai_min_confidence = int(app_settings.ai_min_confidence_to_save or 0)
        link_fetch_senders = list(app_settings.link_fetch_senders or [])
        # None betyder att kolumnen är NULL på en legacy-rad (ALTER TABLE-
        # defaults backfillar inte alltid i PG). bool(None)==False skulle
        # felaktigt stänga av HTML→PDF för alla — tolka None som TRUE
        # (matchar default i models.py + settings_to_dict-tolkningen).
        _htmlpdf_raw = getattr(app_settings, "html_to_pdf_enabled", None)
        html_to_pdf_enabled = True if _htmlpdf_raw is None else bool(_htmlpdf_raw)
        # HTML-only-senders (Skånetrafiken, Moovy, Cursor m.fl.) — hämta
        # FÖRE byggen så standard-queryn kan exkludera dem och html-only-
        # passet (utan has:attachment) blir ENDA platsen de hämtas.
        # Bugg fångad i prod: tidigare hämtades samma mail-IDs av BÅDA
        # passen, sen deduperades html-only-IDs bort och processades aldrig.
        from app.services.html_only_senders import list_active_patterns
        html_only_patterns = list_active_patterns(db)
        gmail_query = build_gmail_query(
            app_settings,
            done_label=DONE_LABEL,
            html_only_patterns=html_only_patterns,
        )
        html_only_gmail_query = build_gmail_query_html_only(
            app_settings,
            html_only_patterns,
            done_label=DONE_LABEL,
        )
        # DIAGNOSTIK: behåll [html-only-diag]-prefix så Mikko kan verifiera
        # i Railway-loggarna att fixen tog effekt.
        logger.info(
            "[html-only-diag] active_patterns_count=%d patterns=%s "
            "query_built=%s",
            len(html_only_patterns),
            html_only_patterns,
            html_only_gmail_query,
        )
        run = ScanRun(started_at=datetime.utcnow(), status="running")
        db.add(run)
        db.flush()
        run_id = run.id

    try:
        gmail = GmailClient()
    except Exception as exc:
        logger.exception("Gmail-klienten kunde inte initialiseras: %s", exc)
        _finalize_run(run_id, result, status="error", note=f"Gmail init: {exc}")
        raise

    try:
        drive = DriveClient()
    except Exception as exc:
        logger.exception("Drive-klienten kunde inte initialiseras: %s", exc)
        _finalize_run(run_id, result, status="error", note=f"Drive init: {exc}")
        raise

    namer = FileNamer()
    analyzer = ReceiptAnalyzer()
    use_ai = ai_enabled and analyzer.enabled
    if ai_enabled and not analyzer.enabled:
        logger.warning(
            "ai_naming_enabled=true men ANTHROPIC_API_KEY saknas — faller tillbaka på heuristisk namngivning."
        )

    bezala: BezalaClient | None = None
    bezala_metadata: dict = {"accounts": [], "cost_centers": [], "vat_rates": []}
    if auto_upload:
        try:
            bezala = BezalaClient()
            logger.info(
                "Bezala auto-upload aktivt (confidence_threshold=%d).",
                confidence_threshold,
            )
            # Hämta referensdata en gång per scan — inte per kvitto.
            bezala_metadata = fetch_bezala_metadata(bezala)
            bezala_metadata["vendor_mappings"] = _load_vendor_mappings()
            logger.info(
                "Bezala metadata: accounts=%d cost_centers=%d vat_rates=%d "
                "vendor_mappings=%d",
                len(bezala_metadata["accounts"]),
                len(bezala_metadata["cost_centers"]),
                len(bezala_metadata["vat_rates"]),
                len(bezala_metadata["vendor_mappings"]),
            )
        except BezalaError as exc:
            logger.warning("Bezala-klienten kunde inte initialiseras: %s", exc)

    try:
        message_ids = gmail.list_candidate_message_ids(
            query=gmail_query, max_results=max_results
        )
    except Exception as exc:
        logger.exception("Kunde inte lista Gmail-meddelanden: %s", exc)
        _finalize_run(run_id, result, status="error", note=f"Gmail list: {exc}")
        raise

    # Andra passet: html-only-senders utan has:attachment. Dedupa
    # mot första passet (samma message_id kan teoretiskt matcha båda
    # om en sender finns både i include_senders och html_only_senders).
    html_only_ids: list[str] = []
    if html_only_gmail_query:
        try:
            logger.info(
                "[html-only-diag] kör Gmail-query: %s",
                html_only_gmail_query,
            )
            raw_html_only = gmail.list_candidate_message_ids(
                query=html_only_gmail_query, max_results=max_results,
            )
            logger.info(
                "[html-only-diag] Gmail returned %d raw IDs: %s",
                len(raw_html_only),
                list(raw_html_only)[:10],
            )
            seen = set(message_ids)
            for mid in raw_html_only:
                if mid not in seen:
                    html_only_ids.append(mid)
                    seen.add(mid)
            logger.info(
                "[html-only-diag] after dedup vs standard pass: %d nya IDs %s",
                len(html_only_ids),
                html_only_ids[:10],
            )
        except Exception as exc:  # noqa: BLE001 — html-only-pass får inte
            # krascha hela scan-loopen. Vi loggar och fortsätter med
            # standard-passet, sen markerar runet som "ok" ändå.
            logger.exception(
                "html-only Gmail-query misslyckades — hoppar passet: %s", exc,
            )
    else:
        logger.info(
            "[html-only-diag] html_only_gmail_query=None → "
            "passet hoppas helt (active_patterns=%s)",
            html_only_patterns,
        )

    result.found = len(message_ids) + len(html_only_ids)
    logger.info(
        "Scanning hittade %d kandidater (standard=%d, html-only=%d, "
        "AI=%s, html_to_pdf=%s, query: %s)",
        result.found,
        len(message_ids),
        len(html_only_ids),
        "on" if use_ai else "off",
        "on" if html_to_pdf_enabled else "off",
        gmail_query,
    )
    if html_only_gmail_query:
        logger.info(
            "html-only-query: %s", html_only_gmail_query,
        )

    for mid in list(message_ids) + html_only_ids:
        try:
            _process_one_message(
                mid, gmail, drive, namer, analyzer, bezala, result,
                excluded_subjects=excluded_subjects,
                use_ai=use_ai,
                auto_upload=auto_upload,
                confidence_threshold=confidence_threshold,
                ai_min_confidence=ai_min_confidence,
                link_fetch_senders=link_fetch_senders,
                html_to_pdf_enabled=html_to_pdf_enabled,
                bezala_metadata=bezala_metadata,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fel under bearbetning av %s: %s", mid, exc)
            result.errors += 1
            result.notes.append(f"{mid}: {exc}")
            _log_error(mid, str(exc))

    if bezala is not None:
        bezala.close()

    _finalize_run(run_id, result, status="ok")
    return result


def fetch_bezala_metadata(bezala: BezalaClient) -> dict:
    """Hämta Bezala-metadata (accounts, cost_centers, vat_rates). Sväljer
    fel per endpoint så vi returnerar det vi faktiskt fick — anroparen
    kan fatta beslut om mappningen är komplett nog."""
    def _safe(fn, label: str) -> list[dict]:
        try:
            return fn()
        except BezalaError as exc:
            logger.warning("Bezala metadata %s misslyckades: %s", label, exc)
            return []

    return {
        "accounts": _safe(bezala.list_accounts, "accounts"),
        "cost_centers": _safe(bezala.list_cost_centers, "cost_centers"),
        "vat_rates": _safe(bezala.list_vat_rates, "vat_rates"),
    }


def _load_vendor_mappings() -> list:
    """Hämta bezala_vendor_mappings en gång per scan. Sväljer DB-fel —
    upload-flödet ska INTE krascha om config-tabellen är otillgänglig,
    bara faller tillbaka på kategori-baserad logik."""
    try:
        from app.services.bezala_config import list_mappings
        with session_scope() as db:
            rows = list_mappings(db)
            return [
                {
                    "vendor_pattern": r.vendor_pattern,
                    "bezala_account_id": r.bezala_account_id,
                    "vat_rate": r.vat_rate,
                    "description_override": r.description_override,
                }
                for r in rows
            ]
    except Exception:  # noqa: BLE001
        logger.exception("Kunde inte ladda bezala_vendor_mappings — fortsätter utan")
        return []


def _attempt_bezala_upload(
    bezala: BezalaClient | None,
    analysis: ReceiptAnalysis | None,
    msg: GmailMessage,
    pdf_bytes: bytes,
    filename: str,
    *,
    auto_upload: bool,
    confidence_threshold: int,
    metadata: dict | None = None,
) -> tuple[str, str | None, str | None]:
    """Försök ladda upp till Bezala. Returnerar (status, transaction_id, error).

    Status: 'success' | 'failed' | 'pending'.

    Funktionen kallas bara för filer som faktiskt sparats till Drive — alltså
    kvitton. 'skipped' används aldrig här (reserverat för icke-kvitton som
    aldrig når detta steg). När auto-upload är av, eller AI-data saknas, eller
    confidence ligger under tröskeln, markeras raden 'pending' så användaren
    kan ladda upp manuellt via UI-knappen.
    """
    if not auto_upload:
        return "pending", None, None
    if analysis is None:
        # Ingen AI-data = kan inte auto-fylla transaktionen. Låt användaren
        # ladda upp manuellt via knappen.
        return "pending", None, None
    if bezala is None:
        return "failed", None, "Bezala-klient kunde inte initialiseras"
    if analysis.confidence < confidence_threshold:
        logger.info(
            "Bezala: confidence %d < tröskel %d — väntar på manuell upload (%s)",
            analysis.confidence, confidence_threshold, filename,
        )
        return "pending", None, None

    if analysis.amount is None or not analysis.date:
        logger.warning(
            "Bezala: saknar amount eller date för %s — kan inte bygga vat_lines",
            filename,
        )
        return "pending", None, "amount/date saknas för Bezala-upload"

    meta = metadata or {"accounts": [], "cost_centers": [], "vat_rates": []}
    params = build_receipt_params(
        file_name=filename,
        sender=msg.sender,
        vendor=analysis.vendor,
        category=analysis.category,
        amount=analysis.amount,
        currency=analysis.currency,
        receipt_date=analysis.date,
        subject=msg.subject,
        accounts=meta["accounts"],
        cost_centers=meta["cost_centers"],
        vat_rates=meta["vat_rates"],
        # FAS 5.9 — använd AI-genererad engelsk Bezala-beskrivning
        description_override=analysis.description_en,
        # FAS 5.10 — vendor→account+VAT-overrides (Moovy/Finavia → 67113 25.5%)
        vendor_mappings=meta.get("vendor_mappings") or [],
    )
    # vat_lines=[] är OK — Bezala använder kontots default_vat_id
    # automatiskt. Vi fortsätter med upload.
    try:
        receipt = bezala.upload_receipt(
            filename=filename,
            pdf_bytes=pdf_bytes,
            description=params["description"],
            date=params["date"],
            credit_account_id=params.get("credit_account_id"),
            vat_lines_attributes=params.get("vat_lines_attributes", []),
        )
        return "success", receipt.attachment_id, None
    except BezalaError as exc:
        logger.exception("Bezala-upload misslyckades för %s", filename)
        err = f"{exc}"
        if exc.body:
            err = f"{exc} | body={exc.body}"
        return "failed", None, err[:2000]


def _process_one_message(
    message_id: str,
    gmail: GmailClient,
    drive: DriveClient,
    namer: FileNamer,
    analyzer: ReceiptAnalyzer,
    bezala: BezalaClient | None,
    result: ScanResult,
    *,
    excluded_subjects: list[str] | None = None,
    use_ai: bool = False,
    auto_upload: bool = False,
    confidence_threshold: int = 0,
    ai_min_confidence: int = 0,
    link_fetch_senders: list[str] | None = None,
    html_to_pdf_enabled: bool = True,
    bezala_metadata: dict | None = None,
    force: bool = False,
) -> None:
    with session_scope() as db:
        existing = (
            db.query(ProcessedMessage)
            .filter(ProcessedMessage.message_id == message_id)
            .first()
        )
        if existing is not None:
            result.skipped += 1
            # Plocka sender/subject/received_at från DB-raden så Log-vyn
            # visar vilket mail som filtrerades (inte bara "—" kolumner).
            received_at_iso = (
                existing.received_at.isoformat()
                if existing.received_at else None
            )
            result.filtered.append({
                "message_id": message_id,
                "sender": existing.sender,
                "subject": existing.subject,
                "received_at": received_at_iso,
                "reason": FILTERED_REASON_ALREADY_PROCESSED,
                "confidence": None,
                "detail": None,
            })
            logger.debug("Hoppar över redan bearbetat %s", message_id)
            return

    msg = gmail.fetch_message(message_id)

    # Vendor-specifik kvitto-länk (C37): vissa avsändare (Arlanda Express)
    # skickar BÅDE bilaga (biljett/boarding pass) OCH en länk till det
    # faktiska kvittot. Bilagan har QR-kod men saknar belopp/moms — för
    # Bezala-attestering behöver vi länk-PDFen. Försök hämta den nu och
    # skriv över bilagorna. Om hämtningen failar (ingen länk, fel domän,
    # timeout) faller vi tillbaka på bilagan (bättre än inget underlag).
    _vendor_link_pdf = fetch_link_receipt_for_message(msg)
    if _vendor_link_pdf is not None:
        msg.attachments = [
            Attachment(
                filename=f"kvitto-{message_id}.pdf",
                mime_type="application/pdf",
                data=_vendor_link_pdf,
            )
        ]

    # Länk-fetch-gren: avsändare matchar link_fetch_senders OCH mailet
    # saknar PDF-bilaga. Om en giltig PDF FINNS bifogad använder vi den
    # istället — Arlanda Express-mail "biljett och kvitto" har t.ex. både
    # PDF (biljett) och länk (kvitto) i samma mail; PDF:en räcker som
    # underlag och vi slipper extra fetch-anrop.
    has_valid_pdf = any(
        looks_like_pdf(a.filename, a.mime_type, a.data) for a in msg.attachments
    )
    if (
        link_fetch_senders
        and sender_matches_link_fetch(msg.sender, link_fetch_senders)
        and not has_valid_pdf
    ):
        link = extract_receipt_link(msg.body_text, msg.body_html)
        if link:
            prelim = _extract_preliminary_fields(
                msg,
                analyzer=analyzer,
                use_ai=use_ai,
                html_to_pdf_enabled=html_to_pdf_enabled,
            )
            try:
                with session_scope() as db:
                    db.add(
                        ProcessedMessage(
                            message_id=msg.message_id,
                            thread_id=msg.thread_id,
                            sender=msg.sender,
                            subject=msg.subject,
                            received_at=msg.received_at,
                            status="needs_manual_download",
                            pending_link=link,
                            vendor=prelim.get("vendor"),
                            amount=prelim.get("amount"),
                            currency=prelim.get("currency"),
                            receipt_date=prelim.get("receipt_date"),
                            category=prelim.get("category"),
                        )
                    )
                result.processed += 1
                logger.info(
                    "Link-fetch: sparade %s som needs_manual_download "
                    "(%s) prelim=%s",
                    message_id, link,
                    {k: v for k, v in prelim.items() if v is not None},
                )
            except IntegrityError:
                result.skipped += 1
                logger.warning("Race: %s redan loggat — hoppar", message_id)
            # INTE mark_done — användaren ska kunna trigga hämtning.
        else:
            result.skipped += 1
            _record_filtered(result, msg, FILTERED_REASON_NO_LINK)
            logger.info(
                "Link-fetch: ingen kvitto-länk hittades i %s — hoppar",
                message_id,
            )
            _log_skip(msg, reason="no_link")
            try:
                gmail.mark_done(message_id)
            except Exception:
                logger.exception("Kunde inte sätta etikett på %s", message_id)
        return

    if excluded_subjects and subject_matches_exclusion(msg.subject, excluded_subjects):
        result.skipped += 1
        _record_filtered(
            result, msg, FILTERED_REASON_EXCLUDED_SUBJECT,
            detail=msg.subject,
        )
        logger.info("Hoppar över %s — ämnet matchar exkludering: %r", message_id, msg.subject)
        _log_skip(msg, reason="excluded_subject")
        try:
            gmail.mark_done(message_id)
        except Exception:
            logger.exception("Kunde inte sätta etikett på %s", message_id)
        return

    pdf_attachments = [
        a for a in msg.attachments if looks_like_pdf(a.filename, a.mime_type, a.data)
    ]

    if not pdf_attachments:
        # Mailet saknar PDF-bilaga. Två alternativ:
        #   a) html_to_pdf_enabled=False → original "no_pdf"-skip
        #   b) annars: konvertera mail-bodyn till PDF och behandla som vanlig bilaga
        if not html_to_pdf_enabled:
            result.skipped += 1
            _record_filtered(result, msg, FILTERED_REASON_NO_PDF)
            logger.info("Inga giltiga PDF-bilagor i %s — markerar som klar", message_id)
            _log_skip(msg, reason="no_pdf")
            try:
                gmail.mark_done(message_id)
            except Exception:
                logger.exception("Kunde inte sätta etikett på %s", message_id)
            return

        logger.info(
            "HTML→PDF triggas för %s (sender=%r subject=%r html_len=%d text_len=%d)",
            message_id, msg.sender, msg.subject,
            len(msg.body_html or ""), len(msg.body_text or ""),
        )

        if not (msg.body_html or msg.body_text):
            result.skipped += 1
            _record_filtered(result, msg, FILTERED_REASON_NO_CONTENT)
            logger.info("HTML→PDF: %s saknar både html och text — hoppar", message_id)
            _log_skip(msg, reason="no_content")
            try:
                gmail.mark_done(message_id)
            except Exception:
                logger.exception("Kunde inte sätta etikett på %s", message_id)
            return

        try:
            pdf_bytes = html_to_pdf(
                msg.body_html or None,
                plain_text_fallback=msg.body_text or None,
            )
        except HtmlToPdfError as exc:
            result.skipped += 1
            _record_filtered(
                result, msg, FILTERED_REASON_HTML_PDF_FAILED,
                detail=str(exc),
            )
            logger.warning(
                "HTML→PDF misslyckades för %s (sender=%r subject=%r): %s",
                message_id, msg.sender, msg.subject, exc,
            )
            _log_skip(msg, reason="html_pdf_failed")
            try:
                gmail.mark_done(message_id)
            except Exception:
                logger.exception("Kunde inte sätta etikett på %s", message_id)
            return

        synth_filename = (msg.subject or f"mail-{message_id}").strip() or f"mail-{message_id}"
        synth_filename = synth_filename.replace("/", "-").replace("\\", "-")[:200]
        if not synth_filename.lower().endswith(".pdf"):
            synth_filename = f"{synth_filename}.pdf"
        pdf_attachments = [
            Attachment(
                filename=synth_filename,
                mime_type="application/pdf",
                data=pdf_bytes,
            )
        ]
        logger.info(
            "HTML→PDF: konverterade %s → %s (%d bytes, sender=%r)",
            message_id, synth_filename, len(pdf_bytes), msg.sender,
        )

    saved_any = False
    any_non_receipt = False

    # FAS 8 — hämta few-shot-exempel innan AI-anropet (gemensamt per
    # mail även om det finns flera bilagor)
    few_shot_examples = (
        _fetch_few_shot_for_sender(msg.sender) if use_ai else []
    )
    # FAS 8.1 — och negativa exempel (mail användaren tidigare markerat
    # som icke-kvitto). Skickas till analyzer så Claude kan filtrera.
    not_receipt_examples = (
        _fetch_not_receipt_for_sender(msg.sender) if use_ai else []
    )

    for att in pdf_attachments:
        analysis: ReceiptAnalysis | None = None
        if use_ai:
            try:
                analysis = analyzer.analyze(
                    attachment_bytes=att.data,
                    mime_type=att.mime_type,
                    original_filename=att.filename,
                    sender=msg.sender,
                    subject=msg.subject,
                    snippet=msg.snippet,
                    received_at=msg.received_at,
                    examples=few_shot_examples,
                    negative_examples=not_receipt_examples,
                )
            except AnalyzerError as exc:
                logger.exception("Claude-analys misslyckades för %s: %s", message_id, exc)
                result.errors += 1
                result.notes.append(f"{message_id}: AI-analys: {exc}")
                _log_error(message_id, f"AI-analys: {exc}", msg=msg)
                return

            logger.info(
                "AI-analys %s: is_receipt=%s confidence=%d%% category=%r vendor=%r",
                message_id, analysis.is_receipt, analysis.confidence,
                analysis.category, analysis.vendor,
            )

            if not analysis.is_receipt:
                any_non_receipt = True
                _record_filtered(
                    result, msg, FILTERED_REASON_NOT_RECEIPT,
                    confidence=analysis.confidence,
                )
                logger.info(
                    "Claude: %s är inte ett kvitto (confidence=%d) — hoppar utan logg",
                    message_id,
                    analysis.confidence,
                )
                continue

            # Låg confidence → SPARA INTE i DB. Samma beteende som
            # is_receipt=False: mark_done i Gmail + logga i scan_run.notes.
            if ai_min_confidence > 0 and analysis.confidence < ai_min_confidence:
                any_non_receipt = True
                _record_filtered(
                    result, msg, FILTERED_REASON_AI_FILTERED,
                    confidence=analysis.confidence,
                )
                result.notes.append(
                    f"{message_id}: låg confidence {analysis.confidence}% < {ai_min_confidence}% — sparas ej"
                )
                logger.info(
                    "Filtrerat av AI-tröskel: %s confidence=%d%% < %d%% (sender=%r subject=%r)",
                    message_id,
                    analysis.confidence,
                    ai_min_confidence,
                    msg.sender,
                    msg.subject,
                )
                continue

        if analysis is not None:
            new_name = analysis.filename
        else:
            new_name = namer.name_for(
                sender=msg.sender,
                subject=msg.subject,
                snippet=msg.snippet,
                received_at=msg.received_at,
                original_filename=att.filename,
            )

        date_str = (msg.received_at or datetime.utcnow()).strftime("%Y%m%d")

        if force:
            # Force-läget kringgår SavedFile- och Drive-filnamnsdubbletter
            # och rensar ev. blockerande SavedFile-rader så uploaden går
            # igenom även när tidigare körningar lämnat hängande rader.
            with session_scope() as db:
                db.query(SavedFile).filter(
                    SavedFile.file_name == new_name,
                    SavedFile.file_date == date_str,
                ).delete(synchronize_session=False)
        else:
            with session_scope() as db:
                if _filename_already_saved(db, new_name, date_str):
                    result.skipped += 1
                    _record_filtered(
                        result, msg,
                        FILTERED_REASON_FILENAME_ALREADY_SAVED,
                        detail=f"{new_name} @ {date_str}",
                    )
                    logger.info("Dubblett (filnamn+datum): %s", new_name)
                    continue

            if drive.filename_exists(new_name):
                result.skipped += 1
                _record_filtered(
                    result, msg,
                    FILTERED_REASON_DRIVE_FILENAME_EXISTS,
                    detail=new_name,
                )
                logger.info("Filnamn finns redan i Drive: %s", new_name)
                continue

        upload = drive.upload_pdf(new_name, att.data)

        bezala_status, bezala_txn_id, bezala_err = _attempt_bezala_upload(
            bezala, analysis, msg, att.data, new_name,
            auto_upload=auto_upload,
            confidence_threshold=confidence_threshold,
            metadata=bezala_metadata,
        )

        try:
            with session_scope() as db:
                db.add(
                    ProcessedMessage(
                        message_id=msg.message_id,
                        thread_id=msg.thread_id,
                        sender=msg.sender,
                        subject=msg.subject,
                        received_at=msg.received_at,
                        file_name=new_name,
                        drive_file_id=upload.file_id,
                        drive_link=upload.web_view_link,
                        status="saved",
                        vendor=analysis.vendor if analysis else None,
                        amount=analysis.amount if analysis else None,
                        currency=analysis.currency if analysis else None,
                        receipt_date=analysis.date if analysis else None,
                        category=analysis.category if analysis else None,
                        summary=analysis.summary if analysis else None,
                        ai_description_en=(
                            analysis.description_en if analysis else None
                        ),
                        ai_confidence=analysis.confidence if analysis else None,
                        bezala_transaction_id=bezala_txn_id,
                        bezala_upload_status=bezala_status,
                        bezala_error_message=bezala_err,
                    )
                )
                db.add(
                    SavedFile(
                        file_name=new_name,
                        file_date=date_str,
                        drive_file_id=upload.file_id,
                    )
                )
            saved_any = True
            result.processed += 1
        except IntegrityError:
            logger.warning("Race: %s redan loggat — hoppar", msg.message_id)
            result.skipped += 1
            _record_filtered(
                result, msg, FILTERED_REASON_INTEGRITY_RACE,
            )
            break

    if saved_any or any_non_receipt:
        try:
            gmail.mark_done(message_id)
        except Exception:
            logger.exception("Kunde inte sätta etikett på %s efter bearbetning", message_id)
    if any_non_receipt and not saved_any:
        result.skipped += 1


def _log_skip(msg: GmailMessage, reason: str) -> None:
    try:
        with session_scope() as db:
            db.add(
                ProcessedMessage(
                    message_id=msg.message_id,
                    thread_id=msg.thread_id,
                    sender=msg.sender,
                    subject=msg.subject,
                    received_at=msg.received_at,
                    status=f"skipped:{reason}",
                )
            )
    except IntegrityError:
        pass


def _extract_preliminary_fields(
    msg: "GmailMessage",
    *,
    analyzer: "ReceiptAnalyzer",
    use_ai: bool,
    html_to_pdf_enabled: bool,
) -> dict:
    """För link_fetch-mail: försök extrahera vendor/date/amount ur mail-
    bodyn (via HTML→PDF + Claude) INNAN användaren hämtar själva kvittot.
    Användaren ser då kontext i Översikt. När PDFen senare laddas ner
    skriver fetch-endpointen över med exakta värden.

    Returnerar alltid en dict — tom om AI avaktiverad, html_to_pdf
    avstängd, body saknas, eller konverterings-/analysfel."""
    empty: dict[str, object] = {
        "vendor": None, "amount": None, "currency": None,
        "receipt_date": None, "category": None,
    }
    if not (use_ai and analyzer.enabled and html_to_pdf_enabled):
        return empty
    if not (msg.body_html or msg.body_text):
        return empty

    try:
        pdf_bytes = html_to_pdf(
            msg.body_html or None,
            plain_text_fallback=msg.body_text or None,
        )
    except HtmlToPdfError as exc:
        logger.info("Link-fetch prelim HTML→PDF misslyckades för %s: %s",
                    msg.message_id, exc)
        return empty

    try:
        analysis = analyzer.analyze(
            attachment_bytes=pdf_bytes,
            mime_type="application/pdf",
            original_filename=f"preliminary-{msg.message_id}.pdf",
            sender=msg.sender,
            subject=msg.subject,
            snippet=msg.snippet,
            received_at=msg.received_at,
            examples=_fetch_few_shot_for_sender(msg.sender),
            negative_examples=_fetch_not_receipt_for_sender(msg.sender),
        )
    except AnalyzerError as exc:
        logger.info("Link-fetch prelim AI-analys misslyckades för %s: %s",
                    msg.message_id, exc)
        return empty

    return {
        "vendor": analysis.vendor,
        "amount": analysis.amount,
        "currency": analysis.currency,
        "receipt_date": analysis.date,
        "category": analysis.category,
    }


def _log_error(
    message_id: str,
    error: str,
    *,
    msg: "GmailMessage | None" = None,
) -> None:
    """Skapa eller uppdatera en error-rad för message_id. När msg ges
    populeras sender/subject/received_at/thread_id också — annars
    visas raden utan kontext i Översikt (och vendor-fallback kan inte
    härleda något från tom sender)."""
    try:
        with session_scope() as db:
            existing = (
                db.query(ProcessedMessage)
                .filter(ProcessedMessage.message_id == message_id)
                .first()
            )
            if existing:
                existing.status = "error"
                existing.error_message = error[:2000]
                if msg is not None:
                    # Fyll BARA tomma fält — skriv inte över tidigare
                    # lyckade extraheringar om raden reprocessas.
                    if not existing.sender and msg.sender:
                        existing.sender = msg.sender
                    if not existing.subject and msg.subject:
                        existing.subject = msg.subject
                    if not existing.received_at and msg.received_at:
                        existing.received_at = msg.received_at
                    if not existing.thread_id and msg.thread_id:
                        existing.thread_id = msg.thread_id
            else:
                db.add(
                    ProcessedMessage(
                        message_id=message_id,
                        sender=(msg.sender if msg else None),
                        subject=(msg.subject if msg else None),
                        received_at=(msg.received_at if msg else None),
                        thread_id=(msg.thread_id if msg else None),
                        status="error",
                        error_message=error[:2000],
                    )
                )
    except Exception:
        logger.exception("Kunde inte logga fel för %s", message_id)


def _build_reprocess_query(days: int, vendor_filter: str | None) -> str:
    """Bygg Gmail-query för reprocess-fönstret. Skiljer sig från standard-
    scan-queryn på två sätt:
      - INGEN has:attachment (vi vill fånga html_to_pdf-targets som
        tidigare missades)
      - INGEN -label:Bezala-Klar (vi söker brett; redan-processade mail
        filtreras bort efteråt via ProcessedMessage-lookup)
    """
    after = (datetime.utcnow() - timedelta(days=days)).strftime("%Y/%m/%d")
    parts: list[str] = [
        f"after:{after}",
        "-in:spam",
        "-in:trash",
        "-category:promotions",
        "-category:social",
    ]
    if vendor_filter:
        token = vendor_filter.strip().lower()
        if token:
            parts.append(f"(from:{token} OR subject:{token})")
    return " ".join(parts)


def reprocess_gmail_window(
    *,
    days: int = 30,
    vendor_filter: str | None = None,
    max_results: int = 100,
) -> dict:
    """Sök Gmail i ett datum-fönster, filtrera bort redan-processade mail
    (de som finns i ProcessedMessage med samma message_id) och kör
    pipeline-orkestreringen för resten.

    Returnerar en dict:
      {
        "found":      antal kandidater från Gmail-sökningen (efter filter),
        "processed":  antal som sparades till Drive/DB,
        "failed":     antal som kraschade i pipeline,
        "skipped":    antal som filtrerades (no_pdf, not_receipt, …),
        "details":    [ {message_id, outcome, sender?, subject?, error?}, … ],
        "query":      slutgiltig Gmail-query (för felsökning),
      }

    Använder samma `_process_one_message` som `run_scan` — vi rör inte
    AI-extraktionen, bara orkestreringen runtomkring. INGEN ScanRun skapas
    (detta är inte en periodisk scan utan en manuell återprocessning).
    """
    query = _build_reprocess_query(days, vendor_filter)
    details: list[dict] = []

    try:
        gmail = GmailClient()
    except Exception as exc:
        logger.exception("reprocess_gmail_window: Gmail-init misslyckades")
        return {
            "found": 0, "processed": 0, "failed": 0, "skipped": 0,
            "details": [], "query": query,
            "error": f"Gmail init: {exc}",
        }

    try:
        drive = DriveClient()
    except Exception as exc:
        logger.exception("reprocess_gmail_window: Drive-init misslyckades")
        return {
            "found": 0, "processed": 0, "failed": 0, "skipped": 0,
            "details": [], "query": query,
            "error": f"Drive init: {exc}",
        }

    try:
        candidate_ids = gmail.list_candidate_message_ids(
            query=query, max_results=max_results,
        )
    except Exception as exc:
        logger.exception("reprocess_gmail_window: Gmail-sökning misslyckades")
        return {
            "found": 0, "processed": 0, "failed": 0, "skipped": 0,
            "details": [], "query": query,
            "error": f"Gmail list: {exc}",
        }

    # Filtrera bort de som redan har en ProcessedMessage-rad — de räknas
    # som processade (även om status='skipped:*' eller 'error'; för dem
    # finns dedikerade reprocess-endpoints).
    unprocessed_ids: list[str] = []
    if candidate_ids:
        with session_scope() as db:
            existing = {
                row.message_id
                for row in db.query(ProcessedMessage.message_id)
                .filter(ProcessedMessage.message_id.in_(candidate_ids))
                .all()
            }
        unprocessed_ids = [mid for mid in candidate_ids if mid not in existing]

    logger.info(
        "reprocess_gmail_window: query=%r kandidater=%d ej_processade=%d "
        "(days=%d vendor_filter=%r max_results=%d)",
        query, len(candidate_ids), len(unprocessed_ids),
        days, vendor_filter, max_results,
    )

    if not unprocessed_ids:
        return {
            "found": 0, "processed": 0, "failed": 0, "skipped": 0,
            "details": [], "query": query,
            "candidates_total": len(candidate_ids),
        }

    # Hämta settings/dependencies en gång (samma uppsättning som run_scan).
    with session_scope() as db:
        app_settings = load_settings(db)
        excluded_subjects = list(app_settings.exclude_subjects or [])
        ai_enabled = bool(app_settings.ai_naming_enabled)
        auto_upload = bool(app_settings.auto_upload_enabled)
        confidence_threshold = int(app_settings.confidence_threshold or 0)
        ai_min_confidence = int(app_settings.ai_min_confidence_to_save or 0)
        link_fetch_senders = list(app_settings.link_fetch_senders or [])
        _htmlpdf_raw = getattr(app_settings, "html_to_pdf_enabled", None)
        html_to_pdf_enabled = True if _htmlpdf_raw is None else bool(_htmlpdf_raw)

    namer = FileNamer()
    analyzer = ReceiptAnalyzer()
    use_ai = ai_enabled and analyzer.enabled

    bezala: BezalaClient | None = None
    bezala_metadata: dict = {"accounts": [], "cost_centers": [], "vat_rates": []}
    if auto_upload:
        try:
            bezala = BezalaClient()
            bezala_metadata = fetch_bezala_metadata(bezala)
            bezala_metadata["vendor_mappings"] = _load_vendor_mappings()
        except BezalaError as exc:
            logger.warning(
                "reprocess_gmail_window: Bezala-init misslyckades: %s", exc,
            )

    result = ScanResult()
    result.found = len(unprocessed_ids)

    for mid in unprocessed_ids:
        before_processed = result.processed
        before_errors = result.errors
        before_skipped = result.skipped
        detail: dict = {"message_id": mid}
        try:
            _process_one_message(
                mid, gmail, drive, namer, analyzer, bezala, result,
                excluded_subjects=excluded_subjects,
                use_ai=use_ai,
                auto_upload=auto_upload,
                confidence_threshold=confidence_threshold,
                ai_min_confidence=ai_min_confidence,
                link_fetch_senders=link_fetch_senders,
                html_to_pdf_enabled=html_to_pdf_enabled,
                bezala_metadata=bezala_metadata,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("reprocess_gmail_window: fel under %s", mid)
            result.errors += 1
            detail["outcome"] = "error"
            detail["error"] = str(exc)[:500]
            _log_error(mid, str(exc))
            details.append(detail)
            continue

        if result.processed > before_processed:
            detail["outcome"] = "processed"
        elif result.errors > before_errors:
            detail["outcome"] = "error"
        elif result.skipped > before_skipped:
            detail["outcome"] = "skipped"
        else:
            detail["outcome"] = "skipped"

        # Plocka sender/subject från den nyss skapade DB-raden (om finns)
        # för bättre översikt i UI-svaret.
        try:
            with session_scope() as db:
                row = (
                    db.query(ProcessedMessage)
                    .filter(ProcessedMessage.message_id == mid)
                    .first()
                )
                if row is not None:
                    detail["sender"] = row.sender
                    detail["subject"] = row.subject
                    detail["status"] = row.status
        except Exception:  # noqa: BLE001
            logger.debug("reprocess: kunde inte plocka DB-detaljer för %s", mid)

        details.append(detail)

    if bezala is not None:
        bezala.close()

    return {
        "found": result.found,
        "processed": result.processed,
        "failed": result.errors,
        "skipped": result.skipped,
        "details": details,
        "query": query,
        "candidates_total": len(candidate_ids),
    }


def force_process_message_ids(
    message_ids: list[str],
    *,
    remove_done_label: bool = True,
    delete_existing_row: bool = True,
) -> dict:
    """Kör pipelinens orkestrering på en explicit lista message_id, helt
    förbi Gmail-queryns filter (has:attachment / Bezala-Klar / Promotions
    osv.) OCH förbi alla lokala dedup-paths (SavedFile-rad,
    drive.filename_exists). Den ENDA filterkanal som behålls är AI-gaten
    "is_receipt=false" / låg confidence — om Claude säger att det inte är
    ett kvitto vill vi fortfarande hoppa över det.

    För varje id:
      1. (Valfritt) Ta bort Bezala-Klar-etiketten i Gmail.
      2. (Valfritt) Radera ev. ProcessedMessage-rad så pipeline-dedupen
         tillåter en ny körning.
      3. Kör `_process_one_message(..., force=True)` — vilket även rensar
         eventuella SavedFile-rader som annars hade tystat uploaden.

    Returnerar samma form som reprocess_gmail_window, plus per-detail
    fält `skip_reason` + `skip_detail` + `filtered_entries` så caller
    ser EXAKT varför ett mail hamnade som skipped."""
    cleaned = [m.strip() for m in (message_ids or []) if m and m.strip()]
    if not cleaned:
        return {
            "found": 0, "processed": 0, "failed": 0, "skipped": 0,
            "details": [],
        }

    details: list[dict] = []

    try:
        gmail = GmailClient()
    except Exception as exc:
        logger.exception("force_process_message_ids: Gmail-init misslyckades")
        return {
            "found": 0, "processed": 0, "failed": 0, "skipped": 0,
            "details": [], "error": f"Gmail init: {exc}",
        }

    try:
        drive = DriveClient()
    except Exception as exc:
        logger.exception("force_process_message_ids: Drive-init misslyckades")
        return {
            "found": 0, "processed": 0, "failed": 0, "skipped": 0,
            "details": [], "error": f"Drive init: {exc}",
        }

    if remove_done_label:
        for mid in cleaned:
            try:
                gmail.remove_done(mid)
            except Exception:  # noqa: BLE001
                logger.info(
                    "force_process: kunde inte ta bort Bezala-Klar för %s "
                    "(kan vara harmlöst om labeln aldrig sattes)", mid,
                )

    if delete_existing_row:
        with session_scope() as db:
            db.query(ProcessedMessage).filter(
                ProcessedMessage.message_id.in_(cleaned)
            ).delete(synchronize_session=False)

    with session_scope() as db:
        app_settings = load_settings(db)
        excluded_subjects = list(app_settings.exclude_subjects or [])
        ai_enabled = bool(app_settings.ai_naming_enabled)
        auto_upload = bool(app_settings.auto_upload_enabled)
        confidence_threshold = int(app_settings.confidence_threshold or 0)
        ai_min_confidence = int(app_settings.ai_min_confidence_to_save or 0)
        link_fetch_senders = list(app_settings.link_fetch_senders or [])
        _htmlpdf_raw = getattr(app_settings, "html_to_pdf_enabled", None)
        html_to_pdf_enabled = True if _htmlpdf_raw is None else bool(_htmlpdf_raw)

    namer = FileNamer()
    analyzer = ReceiptAnalyzer()
    use_ai = ai_enabled and analyzer.enabled

    bezala: BezalaClient | None = None
    bezala_metadata: dict = {"accounts": [], "cost_centers": [], "vat_rates": []}
    if auto_upload:
        try:
            bezala = BezalaClient()
            bezala_metadata = fetch_bezala_metadata(bezala)
            bezala_metadata["vendor_mappings"] = _load_vendor_mappings()
        except BezalaError as exc:
            logger.warning(
                "force_process: Bezala-init misslyckades: %s", exc,
            )

    result = ScanResult()
    result.found = len(cleaned)

    for mid in cleaned:
        before_processed = result.processed
        before_errors = result.errors
        before_skipped = result.skipped
        before_filtered = len(result.filtered)
        detail: dict = {"message_id": mid}
        try:
            _process_one_message(
                mid, gmail, drive, namer, analyzer, bezala, result,
                excluded_subjects=excluded_subjects,
                use_ai=use_ai,
                auto_upload=auto_upload,
                confidence_threshold=confidence_threshold,
                ai_min_confidence=ai_min_confidence,
                link_fetch_senders=link_fetch_senders,
                html_to_pdf_enabled=html_to_pdf_enabled,
                bezala_metadata=bezala_metadata,
                force=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("force_process: fel under %s", mid)
            result.errors += 1
            detail["outcome"] = "error"
            detail["error"] = str(exc)[:500]
            _log_error(mid, str(exc))
            details.append(detail)
            continue

        if result.processed > before_processed:
            detail["outcome"] = "processed"
        elif result.errors > before_errors:
            detail["outcome"] = "error"
        elif result.skipped > before_skipped:
            detail["outcome"] = "skipped"
        else:
            detail["outcome"] = "skipped"

        # Plocka senaste filtered-entry för detta meddelande så caller får
        # en konkret skip_reason istället för bara "skipped". Bryts av om
        # två entries skapades (icke-kvitto + filename-dubblett t.ex.) —
        # rapportera alla i nya fältet.
        new_filtered = result.filtered[before_filtered:]
        if new_filtered:
            detail["skip_reason"] = new_filtered[-1].get("reason")
            detail["skip_detail"] = new_filtered[-1].get("detail")
            detail["filtered_entries"] = new_filtered

        try:
            with session_scope() as db:
                row = (
                    db.query(ProcessedMessage)
                    .filter(ProcessedMessage.message_id == mid)
                    .first()
                )
                if row is not None:
                    detail["sender"] = row.sender
                    detail["subject"] = row.subject
                    detail["status"] = row.status
                    detail["vendor"] = row.vendor
        except Exception:  # noqa: BLE001
            logger.debug("force_process: kunde inte plocka DB-detaljer för %s", mid)

        details.append(detail)

    if bezala is not None:
        bezala.close()

    logger.info(
        "force_process klar: found=%d processed=%d failed=%d skipped=%d ids=%s",
        result.found, result.processed, result.errors, result.skipped, cleaned,
    )

    return {
        "found": result.found,
        "processed": result.processed,
        "failed": result.errors,
        "skipped": result.skipped,
        "details": details,
    }


def _finalize_run(run_id: int, result: ScanResult, *, status: str, note: str | None = None) -> None:
    try:
        with session_scope() as db:
            run = db.query(ScanRun).filter(ScanRun.id == run_id).first()
            if not run:
                return
            run.finished_at = datetime.utcnow()
            run.messages_found = result.found
            run.messages_processed = result.processed
            run.messages_skipped = result.skipped
            run.errors = result.errors
            run.status = status
            run.filtered_messages = list(result.filtered or [])
            if note:
                result.notes.append(note)
            if result.notes:
                run.notes = "\n".join(result.notes)[:4000]
    except Exception:
        logger.exception("Kunde inte uppdatera ScanRun %s", run_id)
