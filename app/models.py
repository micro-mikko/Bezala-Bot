from datetime import datetime
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer, JSON,
    Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.sql import func

from app.db import Base


class ProcessedMessage(Base):
    """Loggar varje bearbetat Gmail-meddelande. message_id är unikt = dubblettskydd."""

    __tablename__ = "processed_messages"
    __table_args__ = (UniqueConstraint("message_id", name="uq_processed_message_id"),)

    id = Column(Integer, primary_key=True)
    message_id = Column(String(255), nullable=False, index=True)
    thread_id = Column(String(255), nullable=True)
    sender = Column(String(512), nullable=True)
    subject = Column(Text, nullable=True)
    received_at = Column(DateTime, nullable=True)
    processed_at = Column(DateTime, server_default=func.now(), nullable=False)
    file_name = Column(String(512), nullable=True)
    drive_file_id = Column(String(255), nullable=True)
    drive_link = Column(Text, nullable=True)
    status = Column(String(64), nullable=False, default="pending")
    error_message = Column(Text, nullable=True)

    vendor = Column(String(255), nullable=True)
    amount = Column(Float, nullable=True)
    currency = Column(String(16), nullable=True)
    receipt_date = Column(String(32), nullable=True)
    category = Column(String(64), nullable=True)
    summary = Column(Text, nullable=True)
    # FAS 5.9 — engelsk beskrivning för Bezala-utkast på formatet
    # "{vendor} + {place/purpose} + {date}", t.ex.
    # "Parking at Helsinki-Vantaa Airport P2, 22-24 April 2026".
    # summary (svenska) behålls för UI-visning; ai_description_en används
    # som description-fält när vi skickar transaktionen till Bezala.
    ai_description_en = Column(String(500), nullable=True)
    ai_confidence = Column(Integer, nullable=True)

    bezala_transaction_id = Column(String(255), nullable=True)
    bezala_upload_status = Column(String(32), nullable=True)
    bezala_error_message = Column(Text, nullable=True)
    # FAS 8.5 — tidsstämpel när ett kvitto kopplades till en Bezala-tx.
    # Sätts av match-to-bezala-endpointen och rensas av unmatch.
    # NULL för pre-existing rader och avkopplade rader.
    matched_at = Column(DateTime, nullable=True, index=True)

    # Snapshot av Bezala bill_line vid match-tillfället (för Matchade-vyn).
    # Sätts av match-to-bezala om bill_line kunde slås upp. Rensas av
    # unmatch. Tillåter UI att visa merchant/amount/date utan att fråga
    # Bezala API på nytt vid varje page-load.
    bezala_payment_merchant = Column(String(255), nullable=True)
    bezala_payment_amount = Column(Float, nullable=True)
    bezala_payment_currency = Column(String(16), nullable=True)
    bezala_payment_date = Column(String(32), nullable=True)

    # Soft-delete (FAS 5.1). deleted_at = NULL → aktiv rad.
    # delete_reason: manual | calendar | spam | misclassified
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    delete_reason = Column(String(32), nullable=True)

    # Länk-baserad PDF-hantering (ny): när leverantören skickar kvitto
    # bakom en klick-länk sparas URL:en här och status sätts till
    # 'needs_manual_download' tills användaren triggar /fetch-pdf.
    pending_link = Column(String(2048), nullable=True)

    # C33 / FAS 7a — manuell kvittouppladdning. 'company_card' (default)
    # går samma väg som Gmail-processade kvitton. 'private_card' är stubbad
    # i UI:t tills FAS 7b lägger till Netvisor-routing.
    payment_method = Column(String(20), nullable=True, default="company_card")
    # 'gmail' = vanligt Gmail-scan-flöde. 'manual_upload' = användaren
    # laddade upp filen via Travel Tinder upload-card.
    upload_source = Column(String(20), nullable=True, default="gmail")
    manual_uploaded_at = Column(DateTime, nullable=True)
    manual_comment = Column(Text, nullable=True)


class SavedFile(Base):
    """Unikhetsindex för filnamn + datum (tredje dubblettskiktet)."""

    __tablename__ = "saved_files"
    __table_args__ = (UniqueConstraint("file_name", "file_date", name="uq_filename_date"),)

    id = Column(Integer, primary_key=True)
    file_name = Column(String(512), nullable=False)
    file_date = Column(String(32), nullable=False)
    drive_file_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ScanRun(Base):
    """Logg av varje schemalagd scanning."""

    __tablename__ = "scan_runs"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, server_default=func.now(), nullable=False)
    finished_at = Column(DateTime, nullable=True)
    messages_found = Column(Integer, default=0, nullable=False)
    messages_processed = Column(Integer, default=0, nullable=False)
    messages_skipped = Column(Integer, default=0, nullable=False)
    errors = Column(Integer, default=0, nullable=False)
    status = Column(String(32), default="running", nullable=False)
    notes = Column(Text, nullable=True)

    # Gate 1.5 — Loggtransparens. Array av filtrerade mail med reason +
    # confidence. NULL = gammal körning utan detaljer (tolkas som [] i API).
    filtered_messages = Column(JSON, nullable=True)


class AppSettings(Base):
    """Applikationsinställningar (singleton — id=1)."""

    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)

    scan_interval_minutes = Column(Integer, nullable=False, default=60)
    ai_naming_enabled = Column(Boolean, nullable=False, default=True)
    auto_upload_enabled = Column(Boolean, nullable=False, default=False)
    confidence_threshold = Column(Integer, nullable=False, default=90)

    require_attachments = Column(Boolean, nullable=False, default=True)
    exclude_promotions = Column(Boolean, nullable=False, default=True)
    exclude_social = Column(Boolean, nullable=False, default=True)
    exclude_calendar = Column(Boolean, nullable=False, default=True)

    include_senders = Column(JSON, nullable=False, default=list)
    exclude_senders = Column(JSON, nullable=False, default=list)
    exclude_subjects = Column(JSON, nullable=False, default=list)

    # Auto-purge för papperskorg. 0 = aldrig (default). 30/60/90 dagar
    # är tillåtna värden i UI, men fältet lagrar vilken siffra som helst.
    trash_auto_purge_days = Column(Integer, nullable=False, default=0)

    # AI-tröskel: mail med confidence lägre än detta SPARAS INTE i DB
    # (raden hoppas permanent över — mark_done sätts i Gmail). Default 40.
    ai_min_confidence_to_save = Column(Integer, nullable=False, default=40)

    # Leverantörer där Bezala Bot ignorerar bilagor och letar efter en
    # kvitto-länk i mailets body istället. Förifylld med Arlanda Express.
    link_fetch_senders = Column(JSON, nullable=False, default=list)

    # Konvertera mail-body till PDF när bilaga saknas (t.ex. Moovy,
    # Skånetrafiken). Default ON.
    html_to_pdf_enabled = Column(Boolean, nullable=False, default=True)

    # Sätts av Gmail/Drive-klienterna när refresh-tokenen är ogiltig
    # (invalid_grant). UI visar varningsbanner + Återanslut-knapp.
    gmail_auth_required = Column(Boolean, nullable=False, default=False)
    drive_auth_required = Column(Boolean, nullable=False, default=False)

    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OAuthToken(Base):
    """Persistenta OAuth-tokens (Gmail/Drive) — sparas i DB istället för
    fil eller env så de överlever Railway-redeploys.

    `service` är primärnyckel ('gmail' eller 'drive'). `token_data` är ett
    JSON-objekt med minst nyckeln `refresh_token` (kan även innehålla
    access_token/expiry för felsökning, men access_token genereras alltid
    om från refresh_token vid behov).
    """

    __tablename__ = "oauth_tokens"

    service = Column(String(32), primary_key=True)
    token_data = Column(JSON, nullable=False, default=dict)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CurrencyRate(Base):
    """Cache för historiska ECB-växelkurser från frankfurter.app.
    Historiska kurser ändras inte → ingen TTL, unik (date, from, to)."""

    __tablename__ = "currency_rates"
    __table_args__ = (
        UniqueConstraint("date", "from_currency", "to_currency",
                         name="uq_currency_rate_date_pair"),
    )

    id = Column(Integer, primary_key=True)
    date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    from_currency = Column(String(3), nullable=False)
    to_currency = Column(String(3), nullable=False)
    rate = Column(Float, nullable=False)
    fetched_at = Column(DateTime, server_default=func.now(), nullable=False)


class MaintenanceTask(Base):
    """Spår engångs-underhållsjobb som körts (kleaning, seed-data etc)."""

    __tablename__ = "maintenance_tasks"

    name = Column(String(128), primary_key=True)
    ran_at = Column(DateTime, server_default=func.now(), nullable=False)


class AiFeedback(Base):
    """FAS 8 — feedback-loop. Loggar 👍/👎 + rättelser från användaren
    så AI:n kan lära sig genom few-shot-exempel i nästa anrop.

    feedback_type: 'thumbs_up' | 'thumbs_down' | 'correction'
    field_name: 'vendor' | 'amount' | 'date' | 'category' (NULL för
    thumbs_up + thumbs_down utan specificerade fält).
    vendor_context: leverantörsnamn för indexering — gör det möjligt
    att hämta vendor-specifika few-shot-exempel.

    OBS: vi använder INTE en hård FK till processed_messages eftersom
    SQLite/Postgres-FK till en UNIQUE (icke-PK) kolumn beter sig olika.
    Föräldralösa rader (efter hard-delete) är OK — de behöver inte
    putsas bort, eftersom de fortfarande är användbara träningsdata."""

    __tablename__ = "ai_feedback"

    id = Column(Integer, primary_key=True)
    message_id = Column(String(255), nullable=False, index=True)
    feedback_type = Column(String(20), nullable=False)
    field_name = Column(String(50), nullable=True)
    ai_value = Column(Text, nullable=True)
    correct_value = Column(Text, nullable=True)
    vendor_context = Column(String(255), nullable=True, index=True)
    # FAS 8.1.1 — subject sparas tillsammans med sender för not_a_receipt-
    # rader så AI:n kan skilja på subject-mönster för samma avsändare
    # (t.ex. Finnair-bokningsbekräftelse vs Finnair-eticket-kvitto).
    subject_context = Column(String(500), nullable=True)
    created_at = Column(
        DateTime, server_default=func.now(), nullable=False, index=True,
    )


# ---------- FAS 11.1 — Resor (trip-gruppering) ----------


class Trip(Base):
    """En resa: en grupp av kvitton (flygbiljett som anchor + relaterade
    kostnader). Skapas av `suggest_trips` (status='suggested') och
    aktiveras när användaren accepterar.

    status:
      'suggested' = AI-förslag, väntar på användarbeslut
      'active'    = användaren har accepterat
      'rejected'  = användaren har avvisat (sparas för feedback)
      'archived'  = arkiverad / klar
    """

    __tablename__ = "trips"

    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    destination = Column(String(100), nullable=True)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    total_amount = Column(Numeric(10, 2), nullable=True)
    base_currency = Column(String(3), nullable=False, default="EUR")
    status = Column(String(20), nullable=False, default="suggested", index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    user_decision_at = Column(DateTime, nullable=True)
    ai_confidence = Column(Integer, nullable=True)
    description = Column(Text, nullable=True)

    # Reserverade fält för framtida FAS 11.5 (Netvisor-integration). Inte
    # populerade i FAS 11.1 — bara strukturen finns på plats.
    netvisor_trip_id = Column(String(50), nullable=True)
    netvisor_synced_at = Column(DateTime, nullable=True)

    user_edited = Column(Boolean, nullable=False, default=False)

    # FAS 11.5.1 — per diem (traktamente)
    destination_country = Column(String(2), nullable=True)
    departure_home_at = Column(DateTime, nullable=True)
    return_home_at = Column(DateTime, nullable=True)
    trip_route = Column(Text, nullable=True)
    per_diem_calculation = Column(JSON, nullable=True)
    per_diem_amount = Column(Numeric(10, 2), nullable=True)
    per_diem_currency = Column(String(3), nullable=True)


Index("idx_trips_dates", Trip.start_date, Trip.end_date)


class TripMessage(Base):
    """Många-till-många mellan Trip och ProcessedMessage (via message_id).
    Soft-delete via removed_at — så vi behåller historik om användaren
    tar bort ett kvitto från resan.

    OBS: vi använder INTE en hård FK till processed_messages eftersom
    SQLite/Postgres-FK till en UNIQUE (icke-PK) kolumn är inkonsekvent
    (samma val som AiFeedback)."""

    __tablename__ = "trip_messages"
    __table_args__ = (
        UniqueConstraint("trip_id", "message_id", name="uq_trip_message"),
    )

    id = Column(Integer, primary_key=True)
    trip_id = Column(
        Integer,
        ForeignKey("trips.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_id = Column(String(255), nullable=False, index=True)
    added_by = Column(String(20), nullable=False)  # 'ai_suggestion' | 'manual'
    added_at = Column(DateTime, server_default=func.now(), nullable=False)
    removed_at = Column(DateTime, nullable=True)


class PerDiemRate(Base):
    """FAS 11.5.1 — Verohallinto-rates per land och år för traktamente.

    full_day_amount = kokopäiväraha (>10h)
    half_day_amount = osapäiväraha (>6h) eller halv ulkomaanpäiväraha
    source = 'verohallinto' eller 'manual'

    Seed:as vid första startup för 2026 (FI, SE, NO, LV)."""

    __tablename__ = "per_diem_rates"
    __table_args__ = (
        UniqueConstraint("year", "country_code", name="uq_per_diem_year_country"),
    )

    id = Column(Integer, primary_key=True)
    year = Column(Integer, nullable=False)
    country_code = Column(String(2), nullable=False)  # ISO 3166-1
    country_name = Column(String(100), nullable=False)
    full_day_amount = Column(Numeric(10, 2), nullable=False)
    half_day_amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="EUR")
    source = Column(String(50), nullable=True)
    source_url = Column(String(500), nullable=True)
    last_updated = Column(DateTime, server_default=func.now(), nullable=True)


Index("idx_per_diem_year_country", PerDiemRate.year, PerDiemRate.country_code)


class TripFeedback(Base):
    """Loggar användarens beslut (accept/reject/edit/wrong_grouping etc.)
    så Claude kan dra lärdom via few-shot. `details` är ett JSON-objekt
    med ändrade fält när feedback_type='edited' (t.ex. {title: {from, to}})."""

    __tablename__ = "trip_feedback"

    id = Column(Integer, primary_key=True)
    trip_id = Column(
        Integer,
        ForeignKey("trips.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    feedback_type = Column(String(50), nullable=False)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ExcludedVendor(Base):
    """FAS 11.1.1 — vendors som aldrig ska räknas som resekvitton.

    Lagrar substring-mönster (t.ex. 'anthropic', 'spotify') som
    matchas case-insensitive mot ProcessedMessage.vendor när
    trip_grouper bygger förslag.

    `added_by` är 'system' (default-listan, seedad vid migration) eller
    'user' (egna tillägg från Inställningar-vyn). Inga FK-relationer.
    """

    __tablename__ = "excluded_vendors"
    __table_args__ = (
        UniqueConstraint(
            "vendor_pattern", name="uq_excluded_vendors_pattern",
        ),
    )

    id = Column(Integer, primary_key=True)
    vendor_pattern = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    added_by = Column(String(20), nullable=False, default="user")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class BezalaVendorMapping(Base):
    """Bezala config-admin — vendor→account+VAT override.

    Använder substring-match (case-insensitive) mot ProcessedMessage.vendor.
    Default-mappningar (Moovy/Finavia → 67113 25.5%) seedas en gång per
    deploy via MaintenanceTask, men användaren får ändra/ta bort dem.

    OBS: själva applicerandet i upload-flödet implementeras i ett
    senare PR — den här PR:en lagrar bara konfigurationen.
    """

    __tablename__ = "bezala_vendor_mappings"
    __table_args__ = (
        UniqueConstraint(
            "vendor_pattern", name="uq_bezala_vendor_mappings_pattern",
        ),
    )

    id = Column(Integer, primary_key=True)
    vendor_pattern = Column(String(200), nullable=False)
    bezala_account_id = Column(Integer, nullable=False)
    vat_rate = Column(Numeric(5, 2), nullable=False)
    description_override = Column(String(500), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class HtmlOnlySender(Base):
    """HTML-only senders: avsändare som skickar kvittot i mail-bodyn
    istället för som PDF-bilaga (Skånetrafiken, Moovy notifieringar,
    Cursor, Airport LRS m.fl.).

    För dessa avsändare hoppar pipeline över has:attachment-filtret och
    låter html_to_pdf-konverteraren producera en PDF av mail-bodyn.

    `sender_pattern` är substring-match (case-insensitive) mot Gmail
    from-fältet. `is_active=False` deaktiverar utan att radera
    (möjliggör snabb on/off utan att tappa beskrivning + historik).
    """

    __tablename__ = "html_only_senders"
    __table_args__ = (
        UniqueConstraint(
            "sender_pattern", name="uq_html_only_senders_pattern",
        ),
    )

    id = Column(Integer, primary_key=True)
    sender_pattern = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
