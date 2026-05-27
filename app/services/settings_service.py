"""Inställningar som singleton-rad i DB.

load_settings returnerar AppSettings-raden (id=1) och skapar den med defaults
om den inte finns. build_gmail_query bygger en Gmail-sökquery från
inställningsraden, inklusive include/exclude av avsändare och ämnen samt
hårdkodade builtin-avsändare.
"""

from __future__ import annotations

import logging
from typing import Iterable

from app.config import get_settings as get_env_settings
from app.models import AppSettings


logger = logging.getLogger(__name__)

SETTINGS_ID = 1

# Hårdkodade avsändare vi alltid scannar. Synliga i Settings-UI som
# read-only pills så användaren vet att de är aktiva.
BUILTIN_INCLUDES: tuple[str, ...] = (
    "eticket@amadeus.com",
    "noreply@finnair.com",
    # Bred prefix-match: fångar alla `invoice+statements@*`-avsändare
    # — Anthropic (`invoice+statements@mail.anthropic.com`), Stripe-genererade
    # fakturor (Bolt/StackBlitz: `invoice+statements+acct_1EPyda…@stripe.com`,
    # m.fl.) samt Lovable (`invoice+statements@lovable.dev`). Gmails
    # `from:`-operator gör substring/token-match, så en bar `from:invoice+statements`
    # plockar in samtliga utan att öppna för bredare trafik (verifierat i prod
    # 2026-05-26: `from:invoice+statements@stripe.com` med fullt
    # domän-suffix matchade 0 mail, `from:invoice+statements` matchade 25).
    # `-category:promotions` är fortsatt aktivt så ev. marknadsmail sållas bort.
    "invoice+statements",
    "noreply@skanetrafiken.se",
    "noreply@moovy.fi",
    "cl.seau@strawberry.se",
    "info.cl.live@strawberry.se",
    "reception.amaranten@strawberry.se",
    "flytoget@flytoget.no",
    "reservations_no_reply@scandichotels.com",
    "noreply@arlandaexpress.se",
)

# Alltid exkluderade avsändare/domäner — får inte överstyras av användaren.
BUILTIN_EXCLUDES_FROM: tuple[str, ...] = (
    "@visma.com",
    "approval.do.not.reply@approval.visma.net",
    "noreply-preflight@email.finnair.com",
)

# Alltid exkluderade subject-fragment.
BUILTIN_EXCLUDES_SUBJECT: tuple[str, ...] = ("ready to take off",)

# Default-avsändare som behöver länk-fetch-flödet (första load).
DEFAULT_LINK_FETCH_SENDERS: tuple[str, ...] = ("noreply@arlandaexpress.se",)

# Gmail:s query-parameter har en praktisk gräns kring 1500 tecken. Vi
# varnar om vi överskrider detta och prioriterar builtins.
GMAIL_QUERY_SOFT_LIMIT = 1500


def load_settings(db) -> AppSettings:
    """Hämta settings-raden, skapa med env-baserade defaults om den saknas."""
    row = db.query(AppSettings).filter(AppSettings.id == SETTINGS_ID).first()
    if row is not None:
        return row

    env = get_env_settings()
    row = AppSettings(
        id=SETTINGS_ID,
        scan_interval_minutes=env.scan_interval_minutes,
        ai_naming_enabled=True,
        auto_upload_enabled=False,
        confidence_threshold=90,
        require_attachments=True,
        exclude_promotions=True,
        exclude_social=True,
        exclude_calendar=True,
        include_senders=[],
        exclude_senders=[],
        exclude_subjects=[],
        trash_auto_purge_days=0,
        ai_min_confidence_to_save=40,
        link_fetch_senders=list(DEFAULT_LINK_FETCH_SENDERS),
        html_to_pdf_enabled=True,
    )
    db.add(row)
    db.flush()
    return row


def settings_to_dict(row: AppSettings) -> dict:
    return {
        "scan_interval_minutes": row.scan_interval_minutes,
        "ai_naming_enabled": row.ai_naming_enabled,
        "auto_upload_enabled": row.auto_upload_enabled,
        "confidence_threshold": row.confidence_threshold,
        "require_attachments": row.require_attachments,
        "exclude_promotions": row.exclude_promotions,
        "exclude_social": row.exclude_social,
        "exclude_calendar": row.exclude_calendar,
        "include_senders": list(row.include_senders or []),
        "exclude_senders": list(row.exclude_senders or []),
        "exclude_subjects": list(row.exclude_subjects or []),
        "trash_auto_purge_days": int(row.trash_auto_purge_days or 0),
        "ai_min_confidence_to_save": int(row.ai_min_confidence_to_save or 40),
        "link_fetch_senders": list(row.link_fetch_senders or []),
        "html_to_pdf_enabled": bool(
            row.html_to_pdf_enabled if row.html_to_pdf_enabled is not None else True
        ),
        "gmail_auth_required": bool(getattr(row, "gmail_auth_required", False)),
        "drive_auth_required": bool(getattr(row, "drive_auth_required", False)),
        "builtin_senders": list(BUILTIN_INCLUDES),  # read-only i UI
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _clean_list(items: Iterable[str] | None) -> list[str]:
    return [s.strip() for s in (items or []) if s and s.strip()]


def _format_from_clause(sender: str) -> str:
    """Bygg `from:<sender>`-klausul. Wrappar med citationstecken om sender
    innehåller `+` — Gmails query-parser bryter OR-clause:r när `+`-tecknet
    förekommer i bara local-part (verifierat i prod 2026-05-26 via
    /api/gmail/peek: `(from:noreply@x.se OR from:invoice+statements)` →
    0 träffar; `(from:noreply@x.se OR from:"invoice+statements")` → 2)."""
    if "+" in sender:
        return f'from:"{sender}"'
    return f"from:{sender}"


def build_gmail_query(
    row: AppSettings,
    done_label: str,
    *,
    html_only_patterns: Iterable[str] | None = None,
) -> str:
    """Bygg Gmail-query: hårdkodade includes + user-includes (OR) samt alla
    exkluderingar. Prioriterar builtins om kombinationen närmar sig
    GMAIL_QUERY_SOFT_LIMIT.

    `html_only_patterns` (PR #20 follow-up): aktiva html_only_senders som
    standard-passet ska EXKLUDERA. Dessa mail ligger oftast helt utan
    PDF-bilaga (eller med inline-logos som Gmail räknar som attachment)
    och blir antingen dropped i pipelinen ELLER deduperade bort innan
    html-only-passet hinner ta dem. Genom att exkludera redan här
    säkerställer vi att html-only-passet är ENDA platsen de processas.
    """
    parts: list[str] = [
        f'-label:"{done_label}"',
        "-in:spam",
        "-in:trash",
        "after:2026/03/21",
    ]

    if row.require_attachments:
        parts.append("has:attachment")
    if row.exclude_promotions:
        parts.append("-category:promotions")
    if row.exclude_social:
        parts.append("-category:social")
    if row.exclude_calendar:
        parts.append("-filename:ics")

    # Builtin-exkluderingar (alltid)
    for sender in BUILTIN_EXCLUDES_FROM:
        parts.append(f"-from:{sender}")
    for subj in BUILTIN_EXCLUDES_SUBJECT:
        parts.append(f'-subject:"{subj}"')

    # User-exkluderingar
    for sender in _clean_list(row.exclude_senders):
        parts.append(f"-from:{sender}")
    for subj in _clean_list(row.exclude_subjects):
        safe = subj.replace('"', '\\"')
        parts.append(f'-subject:"{safe}"')

    # html_only_senders-exkluderingar — ren separation mellan passen.
    # Standard-passet ska aldrig hämta dessa mail; html-only-passet
    # (`build_gmail_query_html_only`) tar dem ostört.
    for pattern in _clean_list(html_only_patterns):
        parts.append(f"-from:{pattern}")

    # Include-OR-klausul: builtins först (hög prio), user-chips efter.
    # Om total längd skulle överskrida Gmails gräns prioriterar vi builtins.
    user_includes = _clean_list(row.include_senders)
    all_includes = list(BUILTIN_INCLUDES) + user_includes
    if all_includes:
        base_len = len(" ".join(parts))
        or_parts: list[str] = []
        dropped: list[str] = []
        for i, s in enumerate(all_includes):
            candidate = or_parts + [_format_from_clause(s)]
            clause = "(" + " OR ".join(candidate) + ")"
            if base_len + 1 + len(clause) > GMAIL_QUERY_SOFT_LIMIT:
                # Släpp resten (som är user-chips eftersom builtins kommer först)
                dropped = all_includes[i:]
                break
            or_parts.append(_format_from_clause(s))
        if or_parts:
            parts.append("(" + " OR ".join(or_parts) + ")")
        if dropped:
            logger.warning(
                "Gmail-query nådde längdgräns — %d user-includes föll bort: %s",
                len(dropped),
                dropped,
            )

    return " ".join(parts)


def build_gmail_query_html_only(
    row: AppSettings,
    html_only_patterns: Iterable[str],
    *,
    done_label: str,
) -> str | None:
    """Bygg en SEPARAT Gmail-query som matchar BARA html-only-avsändarna.

    Den vanliga build_gmail_query använder `has:attachment` som ett
    hårt filter — det blockerar t.ex. Skånetrafiken som skickar
    biljetten som HTML i mail-bodyn. Den här queryn skippar
    `has:attachment` men sätter en strikt `from:`-OR-clause så vi inte
    råkar dra in alla notify-mail.

    Returnerar None om html_only_patterns är tom — då hoppar pipelinen
    helt över andra passet.

    Behåller övriga base-filters (label, spam, trash, datum-fönster +
    user excludes) så samma städlogik gäller.
    """
    patterns = _clean_list(html_only_patterns)
    if not patterns:
        return None

    parts: list[str] = [
        f'-label:"{done_label}"',
        "-in:spam",
        "-in:trash",
        "after:2026/03/21",
    ]
    # OBS: ingen has:attachment här — det är hela poängen med passet.
    # Behåll övriga "kategori"-filter så vi inte plockar in spam-mail
    # från samma avsändare som råkar matcha mönstret.
    if row.exclude_promotions:
        parts.append("-category:promotions")
    if row.exclude_social:
        parts.append("-category:social")
    if row.exclude_calendar:
        parts.append("-filename:ics")

    # User-exkluderingar tillämpas också här — om Mikko har exkluderat
    # en specifik avsändare ska den inte komma in via html-only-passet.
    for sender in _clean_list(row.exclude_senders):
        parts.append(f"-from:{sender}")
    for subj in _clean_list(row.exclude_subjects):
        safe = subj.replace('"', '\\"')
        parts.append(f'-subject:"{safe}"')

    # Strikt include-OR-clause: BARA html_only_patterns. Soft-limit
    # samma som standard-queryn för att respektera Gmails query-gräns.
    base_len = len(" ".join(parts))
    or_parts: list[str] = []
    dropped: list[str] = []
    for i, s in enumerate(patterns):
        candidate = or_parts + [_format_from_clause(s)]
        clause = "(" + " OR ".join(candidate) + ")"
        if base_len + 1 + len(clause) > GMAIL_QUERY_SOFT_LIMIT:
            dropped = patterns[i:]
            break
        or_parts.append(_format_from_clause(s))
    if not or_parts:
        return None
    parts.append("(" + " OR ".join(or_parts) + ")")
    if dropped:
        logger.warning(
            "html_only Gmail-query nådde längdgräns — %d patterns föll bort: %s",
            len(dropped), dropped,
        )
    return " ".join(parts)


def subject_matches_exclusion(subject: str | None, excluded: Iterable[str]) -> bool:
    """Client-side dubbelkoll — returnerar True om subject innehåller någon
    av de exkluderade fraserna (case-insensitive)."""
    if not subject:
        return False
    lower = subject.lower()
    return any(term.lower() in lower for term in _clean_list(excluded))


def sender_matches_link_fetch(sender: str | None, link_fetch_senders: Iterable[str]) -> bool:
    """True om `sender` matchar någon av link_fetch_senders (enkel substring).
    Matchar både `foo@domain.com` och `@domain.com`-notation."""
    if not sender:
        return False
    lower = sender.lower()
    for needle in _clean_list(link_fetch_senders):
        if needle.lower() in lower:
            return True
    return False


