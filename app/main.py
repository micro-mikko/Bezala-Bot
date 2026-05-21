import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import get_db, init_db, session_scope
from app.models import MaintenanceTask, ProcessedMessage, SavedFile, ScanRun
from app.scheduler import reschedule_scheduler, shutdown_scheduler, start_scheduler
from app.routers.bezala_config import router as bezala_config_router
from app.services.bezala_client import BezalaClient, BezalaError
from app.services.bezala_field_mapper import build_receipt_params
from app.services.drive_client import DriveClient
from app.services.gmail_client import GmailClient
from app.services.html_pdf_converter import HtmlToPdfError, html_to_pdf
from app.services.html_sanitizer import extract_links, sanitize_html
from app.services.link_fetcher import LinkFetchError, fetch_pdf_from_link
from app.services.pipeline import (
    _build_reprocess_query,
    fetch_bezala_metadata,
    force_process_message_ids,
    reprocess_gmail_window,
    run_scan,
)
from app.services.receipt_analyzer import AnalyzerError, ReceiptAnalyzer
from app.services.currency_converter import make_db_rate_provider
from app.services.receipt_matcher import find_matches
from app.services.oauth_token_store import (
    OAuthAuthError,
    SERVICES as OAUTH_SERVICES,
    save_refresh_token,
    set_auth_required,
)
from app.services.settings_service import load_settings, settings_to_dict
from app.services.trash_service import (
    drive_delete_safe,
    gmail_mark_done_safe,
    gmail_remove_label_safe,
    normalise_reason,
    restore_row,
    soft_delete_row,
)

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bezala-bot")


CLEANUP_ERRORS_TASK = "cleanup_error_messages_v1"
FIX_SKIPPED_BEZALA_TASK = "fix_skipped_bezala_to_pending_v1"


def _run_once_cleanup_errors() -> None:
    """Ta bort alla rader där status='error' — en gång per deploy-version."""
    try:
        with session_scope() as db:
            if db.query(MaintenanceTask).filter(MaintenanceTask.name == CLEANUP_ERRORS_TASK).first():
                return
            deleted = (
                db.query(ProcessedMessage)
                .filter(ProcessedMessage.status == "error")
                .delete(synchronize_session=False)
            )
            db.add(MaintenanceTask(name=CLEANUP_ERRORS_TASK))
            logger.info("Engångsrensning: tog bort %d error-rader.", deleted)
    except Exception:
        logger.exception("Engångsrensning av error-rader misslyckades.")


def _run_once_seed_excluded_vendors() -> None:
    """FAS 11.1.1 — seed default-listan av exkluderade vendors."""
    try:
        from app.services.excluded_vendors import seed_default_vendors
        with session_scope() as db:
            seed_default_vendors(db)
    except Exception:
        logger.exception("Kunde inte seed:a excluded_vendors-listan.")


def _run_once_seed_bezala_vendor_mappings() -> None:
    """Bezala config-admin — seed default mappningar (Moovy/Finavia → 67113
    25.5%) idempotent."""
    try:
        from app.services.bezala_config import seed_default_mappings
        with session_scope() as db:
            seed_default_mappings(db)
    except Exception:
        logger.exception("Kunde inte seed:a bezala_vendor_mappings.")


def _run_once_seed_html_only_senders() -> None:
    """Seed default-listan av html-only senders (Skånetrafiken, Moovy
    notify, Cursor, Airport LRS)."""
    try:
        from app.services.html_only_senders import (
            seed_default_html_only_senders,
        )
        with session_scope() as db:
            seed_default_html_only_senders(db)
    except Exception:
        logger.exception("Kunde inte seed:a html_only_senders-listan.")


def _run_once_fix_skipped_bezala() -> None:
    """Migrera sparade-till-Drive-rader med bezala_upload_status='skipped' till
    'pending' så användaren kan ladda upp manuellt. Gammal logik satte
    'skipped' även för kvitton när auto-upload var av."""
    try:
        with session_scope() as db:
            if db.query(MaintenanceTask).filter(
                MaintenanceTask.name == FIX_SKIPPED_BEZALA_TASK
            ).first():
                return
            updated = (
                db.query(ProcessedMessage)
                .filter(
                    ProcessedMessage.status == "saved",
                    ProcessedMessage.bezala_upload_status == "skipped",
                )
                .update(
                    {"bezala_upload_status": "pending"},
                    synchronize_session=False,
                )
            )
            db.add(MaintenanceTask(name=FIX_SKIPPED_BEZALA_TASK))
            logger.info(
                "Engångsmigration: bezala_upload_status skipped→pending för %d rader.",
                updated,
            )
    except Exception:
        logger.exception("Engångsmigration skipped→pending misslyckades.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Startar Bezala Bot...")
    if not settings.session_secret:
        logger.warning("SESSION_SECRET saknas — sessioner är osäkra.")
    if not settings.app_password:
        logger.warning("APP_PASSWORD saknas — inloggning kommer alltid att misslyckas.")
    init_db()
    logger.info("Databas initialiserad.")
    _run_once_cleanup_errors()
    _run_once_fix_skipped_bezala()
    _run_once_seed_excluded_vendors()
    _run_once_seed_html_only_senders()
    _run_once_seed_bezala_vendor_mappings()
    start_scheduler()
    yield
    shutdown_scheduler()
    logger.info("Stoppar Bezala Bot.")


app = FastAPI(title="Bezala Bot", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret or secrets.token_hex(32),
    session_cookie="bezala_session",
    same_site="lax",
    https_only=True,
    max_age=60 * 60 * 24 * 7,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bezala_config_router)


def require_auth(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated")


LOGIN_PAGE = """<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bezala Bot — Logga in</title>
<style>
  body { font-family: system-ui, -apple-system, sans-serif; background: #111; color: #eee;
         display: grid; place-items: center; min-height: 100vh; margin: 0; }
  form { background: #1a1a1a; padding: 2rem; border-radius: 8px; width: 100%; max-width: 320px;
         box-shadow: 0 4px 24px rgba(0,0,0,0.4); }
  h1 { margin: 0 0 1.5rem; font-size: 1.2rem; }
  input { width: 100%; padding: 0.6rem; background: #222; color: #eee;
          border: 1px solid #333; border-radius: 4px; box-sizing: border-box; font-size: 1rem; }
  button { width: 100%; margin-top: 1rem; padding: 0.7rem; background: #3a82f6;
           color: white; border: 0; border-radius: 4px; cursor: pointer; font-size: 1rem; }
  button:hover { background: #2f6fd8; }
  .err { color: #f66; margin: 0 0 1rem; font-size: 0.9rem; }
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>Bezala Bot</h1>
  {error_block}
  <input type="password" name="password" placeholder="Lösenord" autofocus required>
  <button type="submit">Logga in</button>
</form>
</body>
</html>
"""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    if request.session.get("authenticated"):
        return RedirectResponse(url="/", status_code=303)
    error_block = f'<p class="err">{error}</p>' if error else ""
    return HTMLResponse(LOGIN_PAGE.replace("{error_block}", error_block))


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    expected = settings.app_password
    if not expected or not secrets.compare_digest(password, expected):
        return RedirectResponse(url="/login?error=Fel+l%C3%B6senord", status_code=303)
    request.session["authenticated"] = True
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"authenticated": True}


@app.get("/health")
def health():
    return {"status": "ok"}


# --- OAuth re-auth flow för Gmail/Drive ---------------------------------
#
# Refresh-tokens går ibland ut (Google återkallar dem efter t.ex. 6 mån
# inaktivitet, lösenordsbyte, eller om OAuth-clienten är i Testing-mode).
# I produktion kan vi inte köra scripts/generate_token.py — istället
# erbjuder vi ett OAuth-flöde direkt i appen:
#
#   GET  /api/auth/{service}/start      → redirect till Google
#   GET  /api/auth/{service}/callback   → tar emot code, sparar token
#
# Tokens persisteras i `oauth_tokens`-tabellen via oauth_token_store så
# de överlever Railway-redeploys.

OAUTH_SCOPES = {
    "gmail": [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.labels",
    ],
    "drive": [
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.readonly",
    ],
}


def _build_oauth_redirect_uri(request: Request, service: str) -> str:
    """Bygg redirect_uri för OAuth-callbacken.

    Prioritet:
      1. Env-variabel `<SERVICE>_OAUTH_REDIRECT_URI` (rekommenderat på
         Railway — mest pålitligt och slipper proxy-detektion).
      2. X-Forwarded-Proto + Host från Railway:s proxy. Railway termer
         TLS i sin edge och pratar HTTP internt med appen, så
         request.base_url returnerar `http://...`. Vi måste explicit
         läsa X-Forwarded-Proto för att få rätt scheme.
      3. Fallback: request.base_url (lokal körning utan proxy).
    """
    import os
    explicit = os.environ.get(f"{service.upper()}_OAUTH_REDIRECT_URI", "").strip()
    if explicit:
        return explicit

    forwarded_proto = (
        request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    )
    forwarded_host = (
        request.headers.get("x-forwarded-host", "").split(",")[0].strip()
        or request.headers.get("host", "").strip()
    )
    if forwarded_proto and forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}/api/auth/{service}/callback"

    base = str(request.base_url).rstrip("/")
    return f"{base}/api/auth/{service}/callback"


def _build_oauth_flow(service: str, redirect_uri: str):
    """Bygg Google OAuth Flow från env-credentials."""
    from google_auth_oauthlib.flow import Flow

    settings = get_settings()
    if not (settings.gmail_client_id and settings.gmail_client_secret):
        raise HTTPException(
            status_code=500,
            detail="OAuth client saknas (GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET).",
        )
    client_config = {
        "web": {
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    flow = Flow.from_client_config(
        client_config,
        scopes=OAUTH_SCOPES[service],
        redirect_uri=redirect_uri,
    )
    return flow


@app.get("/api/auth/{service}/start")
def oauth_start(
    service: str,
    request: Request,
    _: None = Depends(require_auth),
):
    """Initierar OAuth-flow. Redirectar till Google's consent-sida.

    Sparar `state` + redirect_uri i sessionen så callbacken kan validera
    att svaret kommer från samma användare.
    """
    if service not in OAUTH_SERVICES:
        raise HTTPException(status_code=404, detail=f"Okänd service: {service}")

    redirect_uri = _build_oauth_redirect_uri(request, service)
    flow = _build_oauth_flow(service, redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",  # tvinga ny refresh_token
        include_granted_scopes="false",
    )
    request.session[f"oauth_state_{service}"] = state
    request.session[f"oauth_redirect_{service}"] = redirect_uri
    logger.info("OAuth start för %s — redirectar till Google", service)
    return RedirectResponse(url=auth_url, status_code=303)


_OAUTH_RESULT_PAGE = """<!doctype html>
<html lang="sv"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Bezala Bot</title>
<style>
  /* Speglar Bezala Bots designtokens från frontend/src/styles/tokens.css.
     Inlinas eftersom denna sida renderas utanför SPA-bundlen. */
  :root {{
    --bg: #f7f7f4; --surface: #ffffff; --surface-2: #f3f3ee;
    --border: #e4e3db; --border-strong: #cfcec2;
    --text: #111412; --text-2: #4b524c; --muted: #8a8f88;
    --accent: oklch(48% 0.09 165); --accent-ink: #ffffff;
    --ok: oklch(52% 0.10 160); --err: oklch(52% 0.16 25);
    --radius: 8px; --radius-lg: 12px;
    --shadow-md: 0 2px 6px rgba(20, 40, 30, 0.06);
    --font-sans: 'IBM Plex Sans', system-ui, -apple-system, sans-serif;
    --font-display: 'Instrument Serif', Georgia, serif;
  }}
  html[data-theme='B'] {{
    --bg: #12221c; --surface: #1a2d26; --surface-2: #203830;
    --border: #264037; --border-strong: #35574a;
    --text: #f1ead8; --text-2: #b5b09c; --muted: #7d8078;
    --accent: oklch(80% 0.13 90); --accent-ink: #12221c;
    --ok: oklch(78% 0.13 160); --err: oklch(70% 0.16 25);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh;
    font-family: var(--font-sans);
    background: var(--bg); color: var(--text);
    display: grid; place-items: center; padding: 20px;
  }}
  .oauth-card {{
    width: 100%; max-width: 440px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 32px 28px;
    box-shadow: var(--shadow-md);
    text-align: center;
  }}
  .oauth-icon {{
    width: 64px; height: 64px;
    border-radius: 50%;
    display: grid; place-items: center;
    margin: 0 auto 18px;
    font-size: 32px; font-weight: 600;
    background: color-mix(in oklch, var(--ok) 12%, transparent);
    color: var(--ok);
    border: 2px solid color-mix(in oklch, var(--ok) 40%, transparent);
  }}
  .oauth-icon--err {{
    background: color-mix(in oklch, var(--err) 12%, transparent);
    color: var(--err);
    border-color: color-mix(in oklch, var(--err) 40%, transparent);
  }}
  h1 {{
    margin: 0 0 10px;
    font-family: var(--font-display);
    font-weight: 500;
    font-size: 24px;
    letter-spacing: -0.01em;
  }}
  p {{
    margin: 0 0 20px;
    color: var(--text-2);
    font-size: 14px;
    line-height: 1.5;
  }}
  .oauth-redirect-note {{
    color: var(--muted);
    font-size: 12px;
    margin: 14px 0 0;
  }}
  .oauth-btn {{
    display: inline-block;
    padding: 10px 20px;
    background: var(--accent);
    color: var(--accent-ink);
    border: 0; border-radius: var(--radius);
    font: inherit; font-weight: 500; font-size: 14px;
    text-decoration: none;
    cursor: pointer;
    transition: filter 0.12s;
  }}
  .oauth-btn:hover {{ filter: brightness(1.06); }}
  .oauth-btn--ghost {{
    background: transparent;
    color: var(--text);
    border: 1px solid var(--border-strong);
  }}
  .oauth-btn--ghost:hover {{ background: var(--surface-2); }}
</style>
</head><body>
<main class="oauth-card" data-testid="oauth-result-card">
  <div class="oauth-icon {icon_class}" aria-hidden="true">{icon}</div>
  <h1>{title}</h1>
  <p>{body}</p>
  <a href="/settings" class="oauth-btn" data-testid="oauth-back-btn">{back_label}</a>
  {auto_redirect_block}
</main>
<script>
  // Speglar SPA-temat: läs samma localStorage-nyckel som ThemeProvider
  // sätter ('bb_variant'), default = 'A' (ljust).
  try {{
    var v = window.localStorage.getItem('bb_variant');
    if (v === 'A' || v === 'B') {{
      document.documentElement.setAttribute('data-theme', v);
    }}
  }} catch (e) {{ /* ignorera om localStorage är blockerad */ }}
  {redirect_script}
</script>
</body></html>
"""


_AUTO_REDIRECT_BLOCK = (
    '<p class="oauth-redirect-note" data-testid="oauth-redirect-note">'
    'Tar dig tillbaka till Inställningar om <span id="oauth-countdown">3</span>&nbsp;s…'
    "</p>"
)

_AUTO_REDIRECT_SCRIPT = """
  var seconds = 3;
  var el = document.getElementById('oauth-countdown');
  var timer = setInterval(function () {
    seconds -= 1;
    if (el) el.textContent = String(seconds);
    if (seconds <= 0) {
      clearInterval(timer);
      window.location.href = '/settings';
    }
  }, 1000);
"""


def _oauth_result_html(*, ok: bool, service: str, message: str) -> HTMLResponse:
    title = (
        f"{service.capitalize()} återansluten"
        if ok
        else f"{service.capitalize()}-anslutning misslyckades"
    )
    return HTMLResponse(
        _OAUTH_RESULT_PAGE.format(
            title=title,
            icon="✓" if ok else "!",
            icon_class="" if ok else "oauth-icon--err",
            body=message,
            back_label="Tillbaka till Inställningar",
            # Auto-redirect bara vid framgång — vid fel vill användaren
            # läsa felmeddelandet i lugn och ro innan de går vidare.
            auto_redirect_block=_AUTO_REDIRECT_BLOCK if ok else "",
            redirect_script=_AUTO_REDIRECT_SCRIPT if ok else "",
        ),
        status_code=200 if ok else 400,
    )


@app.get("/api/auth/{service}/callback")
def oauth_callback(
    service: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    _: None = Depends(require_auth),
):
    """Tar emot Google's redirect, växlar `code` mot tokens och sparar
    refresh-tokenen i `oauth_tokens`-tabellen.

    Vid framgång: rensar gmail/drive_auth_required-flaggan och visar en
    success-sida med länk tillbaka till /settings.
    """
    if service not in OAUTH_SERVICES:
        raise HTTPException(status_code=404, detail=f"Okänd service: {service}")

    if error:
        return _oauth_result_html(
            ok=False, service=service, message=f"Google avvisade flödet: {error}",
        )
    if not code:
        return _oauth_result_html(
            ok=False, service=service, message="Saknar OAuth-kod i callbacken.",
        )

    expected_state = request.session.pop(f"oauth_state_{service}", None)
    if not expected_state or state != expected_state:
        return _oauth_result_html(
            ok=False,
            service=service,
            message="State-parametern matchar inte. Starta om återanslutningen.",
        )

    redirect_uri = request.session.pop(
        f"oauth_redirect_{service}", None,
    ) or _build_oauth_redirect_uri(request, service)

    try:
        flow = _build_oauth_flow(service, redirect_uri)
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001
        logger.exception("OAuth-token-utbyte misslyckades för %s", service)
        return _oauth_result_html(
            ok=False, service=service, message=f"Token-utbyte misslyckades: {exc}",
        )

    creds = flow.credentials
    refresh_token = getattr(creds, "refresh_token", None)
    if not refresh_token:
        # Händer när Google återanvänder ett tidigare consent. Vi tvingar
        # prompt=consent i /start, så detta ska normalt inte ske, men
        # rapportera tydligt om det gör det.
        return _oauth_result_html(
            ok=False,
            service=service,
            message=(
                "Google returnerade ingen refresh_token. Återkalla appens "
                "åtkomst i ditt Google-konto (myaccount.google.com → Säkerhet "
                "→ Tredjepartsappar) och försök igen."
            ),
        )

    try:
        save_refresh_token(
            service,  # type: ignore[arg-type]
            refresh_token,
            extra={
                "scopes": list(getattr(creds, "scopes", []) or []),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Kunde inte spara refresh-token för %s", service)
        return _oauth_result_html(
            ok=False, service=service, message=f"Sparning misslyckades: {exc}",
        )

    logger.info("OAuth callback OK — refresh-token sparad för %s", service)
    return _oauth_result_html(
        ok=True,
        service=service,
        message=(
            f"{service.capitalize()}-anslutningen är återställd. "
            "Nästa scanning körs automatiskt."
        ),
    )


# Global exception-hanterare: konvertera OAuth-fel från Gmail/Drive till
# 401 (inte 500) så frontend kan reagera korrekt och visa banner.
@app.exception_handler(OAuthAuthError)
def _handle_oauth_auth_error(request: Request, exc: OAuthAuthError):
    from fastapi.responses import JSONResponse
    set_auth_required(exc.service, True)
    return JSONResponse(
        status_code=401,
        content={
            "detail": str(exc),
            "auth_required": exc.service,
        },
    )


FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    def _serve_spa():
        return FileResponse(FRONTEND_DIST / "index.html")

    @app.get("/")
    def spa_root():
        return _serve_spa()

    @app.get("/settings")
    def spa_settings():
        return _serve_spa()


class SettingsPayload(BaseModel):
    scan_interval_minutes: int = Field(ge=5, le=1440)
    ai_naming_enabled: bool
    auto_upload_enabled: bool
    confidence_threshold: int = Field(ge=0, le=100)
    require_attachments: bool
    exclude_promotions: bool
    exclude_social: bool
    exclude_calendar: bool
    include_senders: list[str] = Field(default_factory=list)
    exclude_senders: list[str] = Field(default_factory=list)
    exclude_subjects: list[str] = Field(default_factory=list)
    trash_auto_purge_days: int = Field(default=0, ge=0, le=365)
    ai_min_confidence_to_save: int = Field(default=40, ge=0, le=100)
    link_fetch_senders: list[str] = Field(default_factory=list)
    html_to_pdf_enabled: bool = True


@app.get("/api/settings")
def get_app_settings(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    row = load_settings(db)
    db.commit()
    return settings_to_dict(row)


@app.put("/api/settings")
def update_app_settings(
    payload: SettingsPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    row = load_settings(db)
    data = payload.model_dump()
    for key, value in data.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    reschedule_scheduler(row.scan_interval_minutes)
    return settings_to_dict(row)


@app.post("/api/scan")
def trigger_scan(
    background: BackgroundTasks,
    max_results: int = 50,
    _: None = Depends(require_auth),
):
    """Kör en scanning i bakgrunden. Returnerar direkt — resultatet hamnar i ScanRun."""
    background.add_task(run_scan, max_results=max_results)
    return {"status": "started", "max_results": max_results}


@app.delete("/api/messages/errors")
def delete_error_messages(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Rensa alla rader med status='error' så mailen kan processas om igen."""
    deleted = (
        db.query(ProcessedMessage)
        .filter(ProcessedMessage.status == "error")
        .delete(synchronize_session=False)
    )
    db.commit()
    logger.info("Rensade %d error-rader via API.", deleted)
    return {"deleted": deleted}


@app.get("/api/messages")
def list_messages(
    limit: int = 50,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    q = db.query(ProcessedMessage)
    if not include_deleted:
        q = q.filter(ProcessedMessage.deleted_at.is_(None))
    rows = q.order_by(desc(ProcessedMessage.processed_at)).limit(limit).all()
    return [_serialize_message(r) for r in rows]


@app.get("/api/messages/trash")
def list_trash(
    limit: int = 200,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    rows = (
        db.query(ProcessedMessage)
        .filter(ProcessedMessage.deleted_at.is_not(None))
        .order_by(desc(ProcessedMessage.deleted_at))
        .limit(limit)
        .all()
    )
    return [_serialize_message(r) for r in rows]


@app.get("/api/messages/trash/count")
def trash_count(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    count = (
        db.query(func.count(ProcessedMessage.id))
        .filter(ProcessedMessage.deleted_at.is_not(None))
        .scalar()
        or 0
    )
    return {"count": int(count)}


class DeletePayload(BaseModel):
    reason: str | None = None


class BulkDeletePayload(BaseModel):
    ids: list[int] = Field(default_factory=list)
    reason: str | None = None
    permanent: bool = False
    purge_drive: bool = False


def _get_gmail_client_safe() -> GmailClient | None:
    """Instansiera Gmail-klient best-effort. Vid fel → None (operationen
    fortsätter utan Gmail-sidoeffekt). OAuthAuthError sätter auth_required-
    flaggan internt; vi låter ändå huvudoperationen lyckas."""
    try:
        return GmailClient()
    except OAuthAuthError:
        logger.warning("Gmail kräver återanslutning — Gmail-sidoeffekten hoppas över.")
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Gmail-klient kunde inte initialiseras för trash-op.")
        return None


def _get_drive_client_safe() -> DriveClient | None:
    try:
        return DriveClient()
    except OAuthAuthError:
        logger.warning("Drive kräver återanslutning — Drive-sidoeffekten hoppas över.")
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Drive-klient kunde inte initialiseras för trash-op.")
        return None


def _get_gmail_or_401() -> GmailClient:
    """Strikt Gmail-init: OAuthAuthError bubblar upp till global handler → 401."""
    try:
        return GmailClient()
    except OAuthAuthError:
        raise
    except Exception as exc:
        logger.exception("Gmail-klient kunde inte initialiseras")
        raise HTTPException(status_code=500, detail=f"Gmail-init: {exc}") from exc


def _get_drive_or_401() -> DriveClient:
    try:
        return DriveClient()
    except OAuthAuthError:
        raise
    except Exception as exc:
        logger.exception("Drive-klient kunde inte initialiseras")
        raise HTTPException(status_code=500, detail=f"Drive-init: {exc}") from exc


@app.delete("/api/messages/trash")
def empty_trash(
    purge_drive: bool = False,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    rows = (
        db.query(ProcessedMessage)
        .filter(ProcessedMessage.deleted_at.is_not(None))
        .all()
    )
    if not rows:
        return {"deleted": 0}

    drive_file_ids = [r.drive_file_id for r in rows if r.drive_file_id]
    for row in rows:
        db.delete(row)
    db.commit()

    if purge_drive and drive_file_ids:
        drive = _get_drive_client_safe()
        if drive:
            for file_id in drive_file_ids:
                drive_delete_safe(drive, file_id)

    logger.info("Tömde papperskorgen — %d rader.", len(rows))
    return {"deleted": len(rows)}


@app.delete("/api/messages/{msg_id}")
def delete_message(
    msg_id: int,
    payload: DeletePayload | None = None,
    permanent: bool = False,
    purge_drive: bool = False,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Soft-delete default. Permanent=true → hard-delete raden.
    purge_drive=true → radera även Drive-fil (endast meningsfull vid permanent)."""
    row = db.query(ProcessedMessage).filter(ProcessedMessage.id == msg_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Meddelandet finns inte")

    reason = normalise_reason((payload.reason if payload else None))

    if permanent:
        file_id = row.drive_file_id
        message_id = row.message_id
        db.delete(row)
        db.commit()
        if purge_drive and file_id:
            drive = _get_drive_client_safe()
            if drive:
                drive_delete_safe(drive, file_id)
        logger.info("Hard-delete av msg_id=%s (purge_drive=%s)", msg_id, purge_drive)
        return {"status": "deleted", "permanent": True}

    soft_delete_row(row, reason)
    db.commit()
    gmail = _get_gmail_client_safe()
    if gmail:
        gmail_remove_label_safe(gmail, row.message_id)
    logger.info("Soft-delete av msg_id=%s (reason=%s)", msg_id, reason)
    return _serialize_message(row)


@app.post("/api/messages/{msg_id}/restore")
def restore_message(
    msg_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    row = db.query(ProcessedMessage).filter(ProcessedMessage.id == msg_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Meddelandet finns inte")
    if row.deleted_at is None:
        return _serialize_message(row)
    restore_row(row)
    db.commit()
    gmail = _get_gmail_client_safe()
    if gmail:
        gmail_mark_done_safe(gmail, row.message_id)
    logger.info("Restore av msg_id=%s", msg_id)
    return _serialize_message(row)


def _fetch_pdf_helper(url: str) -> bytes:
    """Wrapper som finns som funktion för att kunna mockas i tester."""
    return fetch_pdf_from_link(url)


@app.get("/api/messages/{msg_id}/body")
def get_message_body(
    msg_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Hämtar mail-bodyn från Gmail + saniterar för preview.

    Returnerar {html, text, links}. html är saniterad (scripts/styles/
    event-handlers borttagna, externa bilder ersatta med placeholder).
    links är en lista av extraherade URL:er så UI kan erbjuda 'Hämta
    PDF från denna länk' per länk."""
    row = db.query(ProcessedMessage).filter(ProcessedMessage.id == msg_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Meddelandet finns inte")
    if not row.message_id:
        raise HTTPException(status_code=400, detail="Meddelandet saknar Gmail message_id")

    gmail = _get_gmail_or_401()

    try:
        msg = gmail.fetch_message(row.message_id)
    except OAuthAuthError:
        raise
    except Exception as exc:
        logger.exception("Kunde inte hämta mail-body för %s", row.message_id)
        raise HTTPException(
            status_code=502, detail=f"Gmail-fetch: {exc}",
        ) from exc

    html = sanitize_html(msg.body_html or "")
    links = extract_links(msg.body_html or "")
    return {
        "html": html,
        "text": msg.body_text or "",
        "links": links,
    }


class FetchPdfFromUrlPayload(BaseModel):
    url: str


@app.post("/api/messages/{msg_id}/fetch-pdf-from-url")
def fetch_pdf_from_url_for_message(
    msg_id: int,
    payload: FetchPdfFromUrlPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Hämtar PDF från en URL som användaren klickat i Drawer-previewen.

    Samma SSRF-skydd som /fetch-pdf. Ersätter pending_link med URL:en
    (om raden hade en) och lyfter raden till status='saved'."""
    row = db.query(ProcessedMessage).filter(ProcessedMessage.id == msg_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Meddelandet finns inte")

    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Saknar URL")

    try:
        pdf_bytes = _fetch_pdf_helper(url)
    except LinkFetchError as exc:
        logger.warning(
            "fetch-pdf-from-url msg_id=%s url=%r misslyckades: %s",
            msg_id, url, exc.message,
        )
        status_code = 400 if "blockerad" in (exc.message or "").lower() else 502
        # Icke-PDF (HTML, bild) → 422 (klientens val av URL är fel)
        if "pdf" in (exc.message or "").lower() and "saknas" in (exc.message or "").lower():
            status_code = 422
        if "istället för pdf" in (exc.message or "").lower():
            status_code = 422
        raise HTTPException(status_code=status_code, detail=exc.message) from exc

    drive = _get_drive_or_401()

    analyzer = ReceiptAnalyzer()
    filename = (row.file_name
                or f"{row.vendor or 'Okand'} {row.subject or ''}".strip()
                or f"Kvitto-{msg_id}")
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    filename = filename.replace("/", "-").replace("\\", "-")

    analysis = None
    if analyzer.enabled:
        try:
            analysis = analyzer.analyze(
                attachment_bytes=pdf_bytes,
                mime_type="application/pdf",
                original_filename=filename,
                sender=row.sender or "",
                subject=row.subject or "",
                snippet="",
                received_at=row.received_at,
            )
            filename = analysis.filename or filename
        except AnalyzerError:
            logger.exception("AI-analys misslyckades för fetch-pdf-from-url %s", msg_id)

    try:
        upload = drive.upload_pdf(filename, pdf_bytes)
    except Exception as exc:
        logger.exception("Drive-upload misslyckades för fetch-pdf-from-url %s", msg_id)
        raise HTTPException(
            status_code=502, detail=f"Drive-upload: {exc}",
        ) from exc

    row.status = "saved"
    row.file_name = filename
    row.drive_file_id = upload.file_id
    row.drive_link = upload.web_view_link
    row.pending_link = None
    if analysis:
        row.vendor = analysis.vendor
        row.amount = analysis.amount
        row.currency = analysis.currency
        row.receipt_date = analysis.date
        row.category = analysis.category
        row.summary = analysis.summary
        row.ai_confidence = analysis.confidence
    row.bezala_upload_status = row.bezala_upload_status or "pending"
    db.commit()

    # Gmail-etikett best-effort
    if row.message_id:
        try:
            gmail = GmailClient()
            gmail.mark_done(row.message_id)
        except Exception:
            logger.exception(
                "Kunde inte sätta Bezala-Klar efter fetch-pdf-from-url för %s", msg_id,
            )

    db.refresh(row)
    logger.info(
        "fetch-pdf-from-url: msg_id=%s sparade %s från %s",
        msg_id, filename, url,
    )
    return _serialize_message(row)


@app.post("/api/messages/{msg_id}/reprocess")
def reprocess_message(
    msg_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Lägg tillbaka en hoppad rad för scanning.

    Bara rader vars status börjar med 'skipped:' eller är 'needs_manual_download'
    får återprocessas. Vi:
      1. Tar bort DB-raden (så pipelinens _message_already_processed-check
         inte blockerar nästa scan)
      2. Tar bort Bezala-Klar-etiketten i Gmail (best-effort — pipelinens
         Gmail-query exkluderar etiketten)
      3. Triggar en bakgrundsscan (max_results=10) så användaren ser
         resultatet inom sekunder istället för att vänta på schemalagd scan
    """
    row = db.query(ProcessedMessage).filter(ProcessedMessage.id == msg_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Meddelandet finns inte")

    status = (row.status or "").lower()
    if not (status.startswith("skipped:") or status == "needs_manual_download"):
        raise HTTPException(
            status_code=400,
            detail=f"Raden har status {row.status!r} — bara 'skipped:*'/'needs_manual_download' kan återprocessas.",
        )

    gmail_message_id = row.message_id
    prior_status = row.status
    db.delete(row)
    db.commit()

    if gmail_message_id:
        gmail = _get_gmail_client_safe()
        if gmail:
            try:
                gmail.remove_done(gmail_message_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Kunde inte ta bort Bezala-Klar-etiketten för %s",
                    gmail_message_id,
                )

    # Trigga en liten scan direkt så raden dyker upp igen snabbt.
    background.add_task(run_scan, max_results=10)

    logger.info(
        "Reprocess msg_id=%s (prior_status=%r) — rad borttagen, bakgrundsscan startad",
        msg_id, prior_status,
    )
    return {
        "status": "reprocessing",
        "id": msg_id,
        "prior_status": prior_status,
    }


@app.post("/api/messages/{msg_id}/reprocess-full")
def reprocess_message_full(
    msg_id: int,
    background: BackgroundTasks,
    force: bool = False,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """FAS Cleanup-PR — bearbeta om ett enskilt meddelande från början.

    Skiljer sig från /reprocess (som bara accepterar skipped/needs_manual_download):
    - Tillåter REPROCESS av saved/coupled rader (med force=true).
    - Raderar Drive-fil best-effort (om den finns).
    - Rensar trip_messages-koppling.
    - Triggar bakgrunds-scan så pipelinen tar in mailet på nytt.

    Vid bezala_transaction_id satt och force=False returneras en
    warning så frontend kan visa bekräftelsemodal innan andra anropet
    med force=true. Bezala-kopplingen rensas INTE automatiskt — användaren
    får koppla om manuellt om hen vill.
    """
    from app.models import TripMessage

    row = db.query(ProcessedMessage).filter(ProcessedMessage.id == msg_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Meddelandet finns inte")

    if row.bezala_transaction_id and not force:
        return {
            "warning": True,
            "is_coupled": True,
            "bezala_transaction_id": row.bezala_transaction_id,
            "message": (
                "Detta kvitto är kopplat till Bezala. Bekräfta för att fortsätta."
            ),
        }

    gmail_message_id = row.message_id
    old_drive_id = row.drive_file_id
    old_coupling = row.bezala_transaction_id
    prior_status = row.status

    # 1. Drive-fil best-effort
    if old_drive_id:
        drive = _get_drive_client_safe()
        if drive is not None:
            try:
                drive.delete_file(old_drive_id)
                logger.info("Reprocess-full: raderade Drive-fil %s", old_drive_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Reprocess-full: kunde inte radera Drive-fil %s — fortsätter",
                    old_drive_id,
                )

    # 2. Trip-koppling (best-effort, men i samma transaktion som radering)
    try:
        if gmail_message_id:
            db.query(TripMessage).filter(
                TripMessage.message_id == gmail_message_id
            ).delete(synchronize_session=False)

        # 3. Radera ProcessedMessage så pipelinens dubblett-check tillåter
        #    nytt processande
        db.delete(row)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Reprocess-full: DB-radering misslyckades")
        raise HTTPException(
            status_code=500, detail=f"Bearbetning misslyckades: {exc}",
        ) from exc

    # 4. Ta bort Bezala-Klar-etiketten i Gmail (best-effort — annars
    #    filtreras mailet bort av default-querien)
    if gmail_message_id:
        gmail = _get_gmail_client_safe()
        if gmail is not None:
            try:
                gmail.remove_done(gmail_message_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Reprocess-full: kunde inte ta bort Bezala-Klar för %s",
                    gmail_message_id,
                )

    # 5. Trigga bakgrunds-scan så pipelinen tar in mailet på nytt
    background.add_task(run_scan, max_results=10)

    logger.info(
        "Reprocess-full: msg_id=%s gmail=%s prior_status=%r had_coupling=%s "
        "had_drive=%s force=%s",
        msg_id, gmail_message_id, prior_status,
        bool(old_coupling), bool(old_drive_id), force,
    )
    return {
        "success": True,
        "id": msg_id,
        "gmail_message_id": gmail_message_id,
        "had_coupling": bool(old_coupling),
        "had_drive": bool(old_drive_id),
        "prior_status": prior_status,
    }


class ReprocessSkippedPayload(BaseModel):
    senders: list[str] = Field(default_factory=list)


@app.post("/api/messages/reprocess-skipped")
def reprocess_skipped_by_sender(
    payload: ReprocessSkippedPayload,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Bulk-reprocess: hoppade (status='skipped:*') rader som matchar en
    avsändare-substring tas bort så de scannas om.

    Body: {"senders": ["skanetrafiken", "moovy"]} — matchas case-
    insensitive som substring mot sender-kolumnen. För varje träff:
      1. Ta bort Bezala-Klar-etiketten i Gmail (best-effort)
      2. Radera DB-raden
      3. Efter loop: trigga bakgrundsscan (max_results=50)
    """
    senders = [s.strip().lower() for s in (payload.senders or []) if s and s.strip()]
    if not senders:
        raise HTTPException(status_code=400, detail="senders saknas")

    like_filters = [
        func.lower(ProcessedMessage.sender).like(f"%{s}%") for s in senders
    ]
    rows = (
        db.query(ProcessedMessage)
        .filter(ProcessedMessage.status.like("skipped:%"))
        .filter(or_(*like_filters))
        .all()
    )

    if not rows:
        return {
            "deleted": 0,
            "labels_removed": 0,
            "senders": senders,
            "triggered_scan": False,
        }

    gmail = _get_gmail_client_safe()
    labels_removed = 0
    for row in rows:
        if gmail and row.message_id:
            try:
                gmail.remove_done(row.message_id)
                labels_removed += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "reprocess-skipped: kunde inte ta bort Bezala-Klar för %s",
                    row.message_id,
                )
        db.delete(row)
    db.commit()

    background.add_task(run_scan, max_results=50)

    logger.info(
        "reprocess-skipped: senders=%s deleted=%d labels_removed=%d — scan triggad",
        senders, len(rows), labels_removed,
    )
    return {
        "deleted": len(rows),
        "labels_removed": labels_removed,
        "senders": senders,
        "triggered_scan": True,
    }


class ReprocessErrorsPayload(BaseModel):
    """Filter för reprocess-errors. error_contains = substring i
    error_message, message_ids = explicit Gmail-id-lista. Om båda är
    tomma raderas ALLA error-rader (fortfarande max 500 som säkerhet)."""
    error_contains: str | None = None
    message_ids: list[str] = Field(default_factory=list)


@app.post("/api/messages/reprocess-errors")
def reprocess_errors(
    payload: ReprocessErrorsPayload,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Bulk-reprocess för error-rader. Används när HTML→PDF-pipelinen
    har sparat rader med status='error' + sender=NULL (tidigare
    _log_error-beteende innan msg-param lades till) — då kan
    reprocess-skipped inte matcha dem på sender-substring.

    Body (alla valfria):
      - error_contains: substring i error_message (case-insensitive)
      - message_ids: explicit lista av Gmail-message_id
    Om båda är tomma: matchar ALLA status='error' (max 500).
    """
    q = db.query(ProcessedMessage).filter(ProcessedMessage.status == "error")
    needle = (payload.error_contains or "").strip().lower()
    if needle:
        q = q.filter(func.lower(ProcessedMessage.error_message).like(f"%{needle}%"))
    ids = [m.strip() for m in (payload.message_ids or []) if m and m.strip()]
    if ids:
        q = q.filter(ProcessedMessage.message_id.in_(ids))

    rows = q.limit(500).all()
    if not rows:
        return {
            "deleted": 0,
            "labels_removed": 0,
            "triggered_scan": False,
            "filter": {"error_contains": needle or None, "message_ids": ids},
        }

    gmail = _get_gmail_client_safe()
    labels_removed = 0
    deleted_ids: list[str] = []
    for row in rows:
        if gmail and row.message_id:
            try:
                gmail.remove_done(row.message_id)
                labels_removed += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "reprocess-errors: kunde inte ta bort Bezala-Klar för %s",
                    row.message_id,
                )
        if row.message_id:
            deleted_ids.append(row.message_id)
        db.delete(row)
    db.commit()

    background.add_task(run_scan, max_results=50)

    logger.info(
        "reprocess-errors: deleted=%d labels_removed=%d filter=%s — scan triggad",
        len(rows), labels_removed,
        {"error_contains": needle or None, "message_ids": ids},
    )
    return {
        "deleted": len(rows),
        "labels_removed": labels_removed,
        "deleted_message_ids": deleted_ids,
        "triggered_scan": True,
        "filter": {"error_contains": needle or None, "message_ids": ids},
    }


class GmailReprocessPayload(BaseModel):
    """Body för POST /api/gmail/reprocess.

    days: hur många dagar bakåt vi söker i Gmail (1–365). Default 30.
    vendor_filter: valfri substring som matchas mot from: + subject:
      (case-insensitive). Användbart när Match Health visar att en
      specifik vendor har många EJ processade mail.
    max_results: tak för Gmail-sökningen (skydd mot oavsiktliga
      gigant-körningar). Default 100, max 500.
    """
    days: int = 30
    vendor_filter: str | None = None
    max_results: int = 100


@app.post("/api/gmail/reprocess")
def reprocess_gmail_unprocessed(
    payload: GmailReprocessPayload,
    _: None = Depends(require_auth),
):
    """Återprocessa Gmail-mail som hittas i ett datum-fönster men aldrig
    har fått en ProcessedMessage-rad (EJ processad i Match Health).

    Skiljer sig från andra reprocess-endpoints på en avgörande punkt:
    den letar i Gmail (inte i DB) och kör pipelinens orkestrering direkt
    — så vi fångar mail som filtrerades bort av gamla buggar eller av
    has:attachment-kravet innan html_to_pdf-stödet kom på plats.

    Body: {"days": 30, "vendor_filter": "lovable", "max_results": 100}.
    Svar: {"found": N, "processed": M, "failed": K, "skipped": S,
           "details": [...], "query": "..."}.
    """
    days = payload.days if payload.days is not None else 30
    if days < 1 or days > 365:
        raise HTTPException(
            status_code=400,
            detail="days måste vara 1–365",
        )
    max_results = (
        payload.max_results if payload.max_results is not None else 100
    )
    if max_results < 1 or max_results > 500:
        raise HTTPException(
            status_code=400,
            detail="max_results måste vara 1–500",
        )
    vendor_filter = (payload.vendor_filter or "").strip() or None

    logger.info(
        "Gmail reprocess startad: days=%d vendor_filter=%r max_results=%d",
        days, vendor_filter, max_results,
    )
    result = reprocess_gmail_window(
        days=days,
        vendor_filter=vendor_filter,
        max_results=max_results,
    )
    logger.info(
        "Gmail reprocess klar: found=%s processed=%s failed=%s skipped=%s",
        result.get("found"), result.get("processed"),
        result.get("failed"), result.get("skipped"),
    )
    return result


class ForceProcessPayload(BaseModel):
    """Body för POST /api/messages/force-process.

    message_ids: Gmail-message_id som ska tvångsprocessas. För varje id:
      1. Bezala-Klar-etiketten tas bort (best-effort).
      2. Ev. befintlig ProcessedMessage-rad raderas så pipeline-dedupen
         släpper igenom körningen.
      3. _process_one_message körs med html_to_pdf-grenen påslagen.

    remove_done_label / delete_existing_row: stäng av om man manuellt
    vill behålla nuvarande state (default båda true).
    """
    message_ids: list[str] = Field(default_factory=list)
    remove_done_label: bool = True
    delete_existing_row: bool = True


@app.post("/api/messages/force-process")
def force_process_messages(
    payload: ForceProcessPayload,
    _: None = Depends(require_auth),
):
    """Tvinga pipeline-körning på explicita Gmail-message_id, helt förbi
    Gmail-queryns filter (has:attachment / Bezala-Klar / Promotions).

    Används som maintenance-utväg för mail som matchar en konfigurerad
    html_only_sender men ändå inte fångas av scan — t.ex. om Bezala-Klar
    redan satts av tidigare buggad scan-runda, eller om Gmail kategoriserar
    debit-notiserna som Promotions.

    Body: {"message_ids": ["19e1...", "..."], "remove_done_label": true,
           "delete_existing_row": true}.
    Svar: {"found": N, "processed": M, "failed": K, "skipped": S,
           "details": [...]}.
    """
    ids = [m.strip() for m in (payload.message_ids or []) if m and m.strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="message_ids saknas")
    if len(ids) > 50:
        raise HTTPException(
            status_code=400,
            detail="max 50 message_ids per anrop",
        )
    logger.info(
        "force-process startad: ids=%s remove_label=%s delete_row=%s",
        ids, payload.remove_done_label, payload.delete_existing_row,
    )
    result = force_process_message_ids(
        ids,
        remove_done_label=payload.remove_done_label,
        delete_existing_row=payload.delete_existing_row,
    )
    logger.info(
        "force-process klar: found=%s processed=%s failed=%s skipped=%s",
        result.get("found"), result.get("processed"),
        result.get("failed"), result.get("skipped"),
    )
    return result


@app.get("/api/gmail/peek")
def gmail_peek(
    vendor_filter: str | None = None,
    days: int = 14,
    query: str | None = None,
    _: None = Depends(require_auth),
):
    """Debug-endpoint: returnerar de råa message_ids som Gmail-sökningen
    ger för samma query som /api/gmail/reprocess bygger, INNAN dedup-
    filtrering mot ProcessedMessage. Read-only, inga sidoeffekter.

    Query-params:
      - vendor_filter: substring (from:/subject:), samma semantik som
        reprocess-endpointet.
      - days: hur många dagar bakåt (default 14, samma som reprocess
        använder för Match Health-knapparna).
      - query: rå Gmail-query som overrider allt annat (vendor_filter
        och days ignoreras om query ges).
    """
    if query is not None and query.strip():
        gmail_query = query.strip()
    else:
        if days < 1 or days > 365:
            raise HTTPException(
                status_code=400,
                detail="days måste vara 1–365",
            )
        vendor = (vendor_filter or "").strip() or None
        gmail_query = _build_reprocess_query(days, vendor)

    try:
        gmail = GmailClient()
    except Exception as exc:
        logger.exception("gmail/peek: Gmail-init misslyckades")
        raise HTTPException(
            status_code=500, detail=f"Gmail-init: {exc}",
        ) from exc

    try:
        message_ids = gmail.list_candidate_message_ids(
            query=gmail_query, max_results=500,
        )
    except Exception as exc:
        logger.exception("gmail/peek: Gmail-sökning misslyckades")
        raise HTTPException(
            status_code=500, detail=f"Gmail list: {exc}",
        ) from exc

    return {
        "query": gmail_query,
        "message_ids": list(message_ids),
        "total": len(message_ids),
    }


@app.post("/api/messages/{msg_id}/fetch-pdf")
def fetch_pdf_for_message(
    msg_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Hämtar PDF från pending_link och lyfter raden till status='saved'.
    Kör AI-analys, laddar upp till Drive, sätter Bezala-Klar i Gmail."""
    row = db.query(ProcessedMessage).filter(ProcessedMessage.id == msg_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Meddelandet finns inte")
    if row.status != "needs_manual_download" or not row.pending_link:
        raise HTTPException(
            status_code=400,
            detail="Raden har ingen väntande länk",
        )

    try:
        pdf_bytes = _fetch_pdf_helper(row.pending_link)
    except LinkFetchError as exc:
        logger.warning("fetch-pdf: %s misslyckades: %s", msg_id, exc.message)
        raise HTTPException(status_code=502, detail=exc.message) from exc

    # Drive + AI + Gmail
    drive = _get_drive_or_401()

    analyzer = ReceiptAnalyzer()
    analysis = None
    filename = (row.file_name
                or f"{row.vendor or 'Okand'} {row.subject or ''}".strip()
                or f"Kvitto-{msg_id}")
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    filename = filename.replace("/", "-").replace("\\", "-")

    if analyzer.enabled:
        try:
            analysis = analyzer.analyze(
                attachment_bytes=pdf_bytes,
                mime_type="application/pdf",
                original_filename=filename,
                sender=row.sender or "",
                subject=row.subject or "",
                snippet="",
                received_at=row.received_at,
            )
            filename = analysis.filename or filename
        except AnalyzerError as exc:
            logger.exception("AI-analys misslyckades för fetch-pdf %s", msg_id)
            # Vi fortsätter ändå — PDF sparas utan AI-data.

    try:
        upload = drive.upload_pdf(filename, pdf_bytes)
    except Exception as exc:
        logger.exception("Drive-upload misslyckades för fetch-pdf %s", msg_id)
        raise HTTPException(
            status_code=502, detail=f"Drive-upload: {exc}"
        ) from exc

    row.status = "saved"
    row.file_name = filename
    row.drive_file_id = upload.file_id
    row.drive_link = upload.web_view_link
    row.pending_link = None
    if analysis:
        row.vendor = analysis.vendor
        row.amount = analysis.amount
        row.currency = analysis.currency
        row.receipt_date = analysis.date
        row.category = analysis.category
        row.summary = analysis.summary
        row.ai_confidence = analysis.confidence
    row.bezala_upload_status = row.bezala_upload_status or "pending"
    db.commit()

    # Gmail-etikett best-effort
    if row.message_id:
        try:
            gmail = GmailClient()
            gmail.mark_done(row.message_id)
        except Exception:
            logger.exception(
                "Kunde inte sätta Bezala-Klar efter fetch-pdf för %s", msg_id,
            )

    db.refresh(row)
    logger.info("fetch-pdf: msg_id=%s sparades till Drive (%s)", msg_id, filename)
    return _serialize_message(row)


@app.post("/api/messages/bulk-delete")
def bulk_delete_messages(
    payload: BulkDeletePayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    ids = [int(i) for i in (payload.ids or []) if i]
    if not ids:
        return {"deleted": 0, "ids": []}

    reason = normalise_reason(payload.reason)
    rows = (
        db.query(ProcessedMessage).filter(ProcessedMessage.id.in_(ids)).all()
    )
    if not rows:
        return {"deleted": 0, "ids": []}

    drive_file_ids: list[str] = []
    gmail_ids: list[str] = []

    if payload.permanent:
        for row in rows:
            if row.drive_file_id:
                drive_file_ids.append(row.drive_file_id)
            db.delete(row)
        db.commit()
        if payload.purge_drive:
            drive = _get_drive_client_safe()
            if drive:
                for file_id in drive_file_ids:
                    drive_delete_safe(drive, file_id)
        logger.info("Bulk hard-delete av %d rader.", len(rows))
        return {"deleted": len(rows), "ids": [r.id for r in rows], "permanent": True}

    for row in rows:
        soft_delete_row(row, reason)
        if row.message_id:
            gmail_ids.append(row.message_id)
    db.commit()
    gmail = _get_gmail_client_safe()
    if gmail:
        for gid in gmail_ids:
            gmail_remove_label_safe(gmail, gid)
    logger.info("Bulk soft-delete av %d rader (reason=%s).", len(rows), reason)
    return {
        "deleted": len(rows),
        "ids": [r.id for r in rows],
        "permanent": False,
    }


class BackfillDescriptionsPayload(BaseModel):
    """Body för POST /api/messages/backfill-descriptions.

    Exakt EN av fälten måste anges:
      - message_ids: explicit lista (heltal) — backfilla just dessa rader.
      - all_missing: True → alla rader där ai_description_en IS NULL
        AND status='saved' AND deleted_at IS NULL.
    """
    message_ids: list[int] | None = None
    all_missing: bool | None = None


@app.post("/api/messages/backfill-descriptions")
def backfill_descriptions(
    payload: BackfillDescriptionsPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """FAS 5.22 — backfilla ai_description_en för rader som processades
    innan kolumnen fanns (pre-PR30) eller där AI:n inte producerade
    en engelsk beskrivning.

    Använder existerande Claude-klient + samma modell som extraction-
    pipelinen. Skippar rader som redan har värde. Sekvenstellt med 200ms
    delay mellan API-anrop (skydd mot rate-limit)."""
    from app.services.description_backfill import (
        DescriptionBackfiller,
        backfill_rows,
    )

    has_ids = bool(payload.message_ids)
    has_all = bool(payload.all_missing)
    if has_ids == has_all:
        raise HTTPException(
            status_code=400,
            detail=(
                "Ange exakt EN av 'message_ids' (lista) eller "
                "'all_missing' (true)."
            ),
        )

    q = db.query(ProcessedMessage)
    if has_ids:
        ids = [int(i) for i in (payload.message_ids or []) if i]
        if not ids:
            raise HTTPException(
                status_code=400,
                detail="message_ids får inte vara tom",
            )
        q = q.filter(ProcessedMessage.id.in_(ids))
    else:
        q = q.filter(
            ProcessedMessage.ai_description_en.is_(None),
            ProcessedMessage.status == "saved",
            ProcessedMessage.deleted_at.is_(None),
        )

    rows = q.order_by(ProcessedMessage.id.asc()).all()
    if not rows:
        return {"processed": 0, "failed": 0, "skipped": 0, "details": []}

    backfiller = DescriptionBackfiller()
    if not backfiller.enabled:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY saknas — backfill kan inte köras",
        )

    results = backfill_rows(rows, backfiller)
    db.commit()

    processed = sum(1 for r in results if r.status == "ok")
    failed = sum(1 for r in results if r.status == "failed")
    skipped = sum(1 for r in results if r.status == "skipped")
    logger.info(
        "Backfill ai_description_en klar: processed=%d failed=%d skipped=%d",
        processed, failed, skipped,
    )
    return {
        "processed": processed,
        "failed": failed,
        "skipped": skipped,
        "details": [r.to_dict() for r in results],
    }


def _serialize_message(r: ProcessedMessage) -> dict:
    return {
        "id": r.id,
        "message_id": r.message_id,
        "sender": r.sender,
        "subject": r.subject,
        "received_at": r.received_at.isoformat() if r.received_at else None,
        "processed_at": r.processed_at.isoformat() if r.processed_at else None,
        "file_name": r.file_name,
        "drive_file_id": r.drive_file_id,
        "drive_link": r.drive_link,
        "status": r.status,
        "error_message": r.error_message,
        "vendor": r.vendor,
        "amount": r.amount,
        "currency": r.currency,
        "receipt_date": r.receipt_date,
        "category": r.category,
        "summary": r.summary,
        "ai_description_en": r.ai_description_en,
        "ai_confidence": r.ai_confidence,
        "bezala_transaction_id": r.bezala_transaction_id,
        "bezala_upload_status": r.bezala_upload_status,
        "bezala_error_message": r.bezala_error_message,
        "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
        "delete_reason": r.delete_reason,
        "pending_link": r.pending_link,
    }


@app.get("/api/bezala/metadata")
def get_bezala_metadata(_: None = Depends(require_auth)):
    """Returnerar råa listor från Bezala: accounts, cost_centers, vat_rates.

    Används för att inspektera exakt respons-struktur (fältnamn, IDs) från
    live Bezala och verifiera att field-mapper matchar rätt konton."""
    try:
        bezala = BezalaClient()
    except BezalaError as exc:
        logger.exception("Bezala-klient kunde inte initialiseras")
        raise HTTPException(status_code=500, detail=f"Bezala-init: {exc}") from exc

    try:
        payload = {
            "accounts": _safe_bezala_list(bezala.list_accounts, "accounts"),
            "cost_centers": _safe_bezala_list(bezala.list_cost_centers, "cost_centers"),
            "vat_rates": _safe_bezala_list(bezala.list_vat_rates, "vat_rates"),
        }
        return payload
    finally:
        bezala.close()


def _safe_bezala_list(fn, label: str) -> dict:
    """Wrapa en list_*-funktion: returnerar {rows: [...], error: None} eller
    {rows: [], error: '...'} så UI kan se vilken endpoint som fallerade."""
    try:
        rows = fn()
        return {"count": len(rows), "rows": rows, "error": None}
    except BezalaError as exc:
        logger.warning("Bezala metadata %s misslyckades: %s", label, exc)
        return {"count": 0, "rows": [], "error": f"{exc.status_code}: {exc.body or str(exc)}"}


# === DELETE-MIG-EFTER-VERIFIERING ===========================================
# Tillfälliga diagnostik-endpoints (Moovy-dubbletter + mapping-trace).
# Båda är read-only och rör ALDRIG Bezala-skrivande operationer — de bara
# läser DB + Bezala-metadata för att verifiera hypoteser i C5-diagnos-
# rapporten. Tas bort när vi har data och kan rikta fixarna.

_MOOVY_RECEIPT_DATES = ("2026-04-15", "2026-04-13")


def _serialize_processed_message_full(row: ProcessedMessage) -> dict:
    """Plocka ut samtliga kolumner från en ProcessedMessage som JSON-vänligt
    dict. Inkluderar fält som vanliga `_serialize_message` döljer (raw
    error, deleted_at etc) — eftersom det här är ett diagnostik-svar."""
    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v
    return {
        "id": row.id,
        "message_id": row.message_id,
        "thread_id": row.thread_id,
        "sender": row.sender,
        "subject": row.subject,
        "received_at": _iso(row.received_at),
        "processed_at": _iso(row.processed_at),
        "file_name": row.file_name,
        "drive_file_id": row.drive_file_id,
        "drive_link": row.drive_link,
        "status": row.status,
        "error_message": row.error_message,
        "vendor": row.vendor,
        "amount": row.amount,
        "currency": row.currency,
        "receipt_date": row.receipt_date,
        "category": row.category,
        "summary": row.summary,
        "ai_description_en": row.ai_description_en,
        "ai_confidence": row.ai_confidence,
        "bezala_transaction_id": row.bezala_transaction_id,
        "bezala_upload_status": row.bezala_upload_status,
        "bezala_error_message": row.bezala_error_message,
        "matched_at": _iso(row.matched_at),
        "bezala_payment_merchant": row.bezala_payment_merchant,
        "bezala_payment_amount": row.bezala_payment_amount,
        "bezala_payment_currency": row.bezala_payment_currency,
        "bezala_payment_date": row.bezala_payment_date,
        "deleted_at": _iso(row.deleted_at),
        "delete_reason": row.delete_reason,
        "pending_link": row.pending_link,
    }


@app.get("/api/debug/moovy-diagnostics")
def debug_moovy_diagnostics(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """**TILLFÄLLIG** — verifierar C5-diagnos om 4 Moovy-dubbletter 15/4.

    Returnerar:
      - processed_messages: alla rader där vendor ILIKE '%moovy%' och
        receipt_date är 2026-04-15 eller 2026-04-13
      - saved_files: alla rader där file_name ILIKE '%moovy%'
      - duplicates_per_pair: räknat per (vendor, receipt_date)
      - duplicates_per_filename_filedate: räknat per (file_name, file_date)
        i saved_files — visar om lager-3-dedupen faktiskt utlöste eller
        släppte alla rader igenom

    Read-only. Tas bort när data är inhämtad.
    """
    moovy_msgs = (
        db.query(ProcessedMessage)
        .filter(ProcessedMessage.vendor.ilike("%moovy%"))
        .filter(ProcessedMessage.receipt_date.in_(_MOOVY_RECEIPT_DATES))
        .order_by(ProcessedMessage.receipt_date, ProcessedMessage.received_at)
        .all()
    )
    moovy_files = (
        db.query(SavedFile)
        .filter(SavedFile.file_name.ilike("%moovy%"))
        .order_by(SavedFile.file_date, SavedFile.created_at)
        .all()
    )

    from collections import Counter
    pair_counts: Counter = Counter()
    for r in moovy_msgs:
        pair_counts[(r.vendor or "", r.receipt_date or "")] += 1
    file_pair_counts: Counter = Counter()
    for f in moovy_files:
        file_pair_counts[(f.file_name or "", f.file_date or "")] += 1

    logger.info(
        "moovy-diagnostics: processed_messages=%d saved_files=%d "
        "unique_(vendor,date)-pairs=%d",
        len(moovy_msgs), len(moovy_files), len(pair_counts),
    )

    return {
        "processed_messages": [
            _serialize_processed_message_full(r) for r in moovy_msgs
        ],
        "saved_files": [
            {
                "id": f.id,
                "file_name": f.file_name,
                "file_date": f.file_date,
                "drive_file_id": f.drive_file_id,
                "created_at": (
                    f.created_at.isoformat()
                    if hasattr(f.created_at, "isoformat") else f.created_at
                ),
            }
            for f in moovy_files
        ],
        "duplicates_per_pair": [
            {"vendor": v, "receipt_date": d, "count": c}
            for (v, d), c in sorted(pair_counts.items())
        ],
        "duplicates_per_filename_filedate": [
            {"file_name": n, "file_date": d, "count": c}
            for (n, d), c in sorted(file_pair_counts.items())
        ],
        "filter": {
            "vendor_ilike": "%moovy%",
            "receipt_dates": list(_MOOVY_RECEIPT_DATES),
        },
    }


@app.get("/api/debug/bezala-mapping-trace")
def debug_bezala_mapping_trace(
    message_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """**TILLFÄLLIG** — kör HELA mapping-kedjan för en specifik
    ProcessedMessage UTAN att skriva något till Bezala. Returnerar
    JSON-spår med varje mellansteg så vi kan se exakt var rätt eller
    fel hamnar.

    `message_id` accepteras som antingen:
      - numeriskt sträng → tolkat som processed_messages.id (PK)
      - icke-numerisk sträng → tolkat som processed_messages.message_id
        (Gmail-ID)

    Spårar:
      1. Raden själv (category, vendor, sender, amount, currency, …)
      2. get_account_id_for_category(category) → konto-ID + (om mappning
         saknas) varför fallback användes
      3. sender_to_country(sender, vendor) → 'fi' | 'eu' | 'non-eu'
      4. Bezala-metadata: list_accounts + list_cost_centers + list_vat_rates
         (read-only). Om Bezala inte kan kontaktas: error i svaret men
         själva spårningen fortsätter.
      5. select_account(accounts, category) → vilket konto valdes,
         default_vat_id på det
      6. select_default_cost_center(cost_centers) → vilken VIS valdes
      7. build_vat_lines_attributes(...) → list (kan vara tom)
      8. COUNTRY_DEFAULT_VAT[country] → vilken procent fallback skulle gett
      9. Sammanfattande verdict: "would_send_vat_lines" true/false

    Hela kedjan är side-effect-fri. Tas bort när data är inhämtad.
    """
    from app.services.bezala_field_mapper import (
        COUNTRY_DEFAULT_VAT,
        DEFAULT_ACCOUNT_ID,
        DEFAULT_CREDIT_ACCOUNT_ID,
        build_vat_lines_attributes,
        get_account_id_for_category,
        get_default_vat_for_country,
        select_account,
        select_default_cost_center,
        sender_to_country,
        tax_percentage_for_vat_code,
    )

    # 1. Slå upp raden — stöd både PK-int och Gmail-message_id-sträng.
    q = db.query(ProcessedMessage)
    if message_id.isdigit():
        row = q.filter(ProcessedMessage.id == int(message_id)).first()
        lookup_mode = "id (PK)"
    else:
        row = q.filter(ProcessedMessage.message_id == message_id).first()
        lookup_mode = "message_id (Gmail)"
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(f"Hittade ingen ProcessedMessage för {lookup_mode}="
                    f"{message_id!r}"),
        )

    trace: dict = {
        "lookup": {
            "input": message_id,
            "mode": lookup_mode,
            "matched_row_pk": row.id,
        },
        "row": {
            "id": row.id,
            "message_id": row.message_id,
            "sender": row.sender,
            "vendor": row.vendor,
            "category": row.category,
            "amount": row.amount,
            "currency": row.currency,
            "receipt_date": row.receipt_date,
            "file_name": row.file_name,
            "bezala_upload_status": row.bezala_upload_status,
            "bezala_transaction_id": row.bezala_transaction_id,
            "bezala_error_message": row.bezala_error_message,
        },
    }

    # 2. Kategori → konto-ID via tabellen (utan Bezala-anrop)
    resolved_account_id = get_account_id_for_category(row.category)
    trace["category_lookup"] = {
        "input_category": row.category,
        "resolved_account_id": resolved_account_id,
        "default_account_id_constant": DEFAULT_ACCOUNT_ID,
        "fell_back_to_default": resolved_account_id == DEFAULT_ACCOUNT_ID,
    }

    # 3. Country-detection
    country = sender_to_country(row.sender, row.vendor)
    trace["country_detection"] = {
        "sender": row.sender,
        "vendor": row.vendor,
        "resolved_country": country,
    }

    # 4. Bezala-metadata (read-only; behandla nätverksfel mjukt)
    accounts: list[dict] = []
    cost_centers: list[dict] = []
    vat_rates: list[dict] = []
    bezala_errors: dict = {}
    try:
        bezala = BezalaClient()
    except BezalaError as exc:
        bezala_errors["client_init"] = str(exc)
        bezala = None
    if bezala is not None:
        try:
            accounts = bezala.list_accounts()
        except BezalaError as exc:
            bezala_errors["accounts"] = f"{exc.status_code}: {exc.body or exc}"
        try:
            cost_centers = bezala.list_cost_centers()
        except BezalaError as exc:
            bezala_errors["cost_centers"] = f"{exc.status_code}: {exc.body or exc}"
        try:
            vat_rates = bezala.list_vat_rates()
        except BezalaError as exc:
            bezala_errors["vat_rates"] = f"{exc.status_code}: {exc.body or exc}"
        finally:
            bezala.close()

    trace["bezala_metadata"] = {
        "accounts_count": len(accounts),
        "cost_centers_count": len(cost_centers),
        "vat_rates_count": len(vat_rates),
        "errors": bezala_errors,
        "resolved_account_id_in_accounts_list": any(
            (a.get("id") == resolved_account_id
             or a.get("account_id") == resolved_account_id)
            for a in accounts
        ),
    }

    # 5. select_account mot live-metadata
    account = select_account(accounts, row.category)
    trace["selected_account"] = (
        {
            "id": account.get("id") or account.get("account_id"),
            "name": account.get("name") or account.get("title")
                    or account.get("label"),
            "default_vat_id": account.get("default_vat_id"),
            "matched_resolved_id": (
                (account.get("id") or account.get("account_id"))
                == resolved_account_id
            ),
        }
        if account else None
    )

    # 6. cost_center-default
    cost_center = select_default_cost_center(cost_centers)
    trace["selected_cost_center"] = (
        {
            "id": cost_center.get("id") or cost_center.get("cost_center_id"),
            "name": cost_center.get("name") or cost_center.get("title")
                    or cost_center.get("label"),
            "is_default": (
                cost_center.get("default") is True
                or cost_center.get("is_default") is True
            ),
        }
        if cost_center else None
    )

    # 7. build_vat_lines_attributes — den centrala funktionen
    vat_lines = build_vat_lines_attributes(
        amount=row.amount,
        currency=row.currency,
        account=account,
        cost_center=cost_center,
        vat_rate=None,  # speglar upload-flödet när account.default_vat_id finns
    )
    trace["vat_lines_attributes"] = {
        "result": vat_lines,
        "is_empty": len(vat_lines) == 0,
        "would_be_sent_in_PUT": bool(vat_lines),
        "explanation": (
            "Bezala får INGA vat_lines (defaultar konto/moms-själv)"
            if not vat_lines
            else "Bezala får full payload"
        ),
    }

    # 8. Country-default-fallback (vad vi SKULLE använda om vi körde
    # tax_percentage_for_vat_code med None)
    country_default_pct = get_default_vat_for_country(country)
    trace["country_default_vat"] = {
        "country": country,
        "fallback_tax_percentage": country_default_pct,
        "fi_default": COUNTRY_DEFAULT_VAT["fi"],
        "tax_pct_for_None_vat_code": tax_percentage_for_vat_code(
            None, country=country,
        ),
    }

    # 9. Sammanfattande verdict
    trace["verdict"] = {
        "would_send_vat_lines": bool(vat_lines),
        "credit_account_id_hardcoded": DEFAULT_CREDIT_ACCOUNT_ID,
        "likely_root_cause": (
            "Account hittades men default_vat_id är None och vat_rates "
            "är tom → vat_lines = [] → Bezala fyller egna defaults"
            if account is not None
            and account.get("default_vat_id") is None
            and not vat_rates
            else (
                "Account saknas (varken ID-match eller namnmatch i Bezalas "
                "/accounts-svar) → vat_lines = []"
                if account is None and accounts
                else (
                    "Bezala-metadata kunde inte hämtas → kan inte avgöra "
                    "rotorsak utan att kunna se accounts-listan"
                    if bezala_errors
                    else None
                )
            )
        ),
    }

    logger.info(
        "bezala-mapping-trace: msg_pk=%s category=%r resolved_account_id=%s "
        "country=%s vat_lines_count=%d account_default_vat_id=%s",
        row.id, row.category, resolved_account_id, country,
        len(vat_lines),
        (account or {}).get("default_vat_id"),
    )
    return trace


# === SLUT DELETE-MIG-EFTER-VERIFIERING ======================================


_MISSING_AMOUNT_RE = __import__("re").compile(
    r"(\d+(?:[.,]\d{1,2})?)\s+([A-Z]{3})\s*$"
)


def _parse_amount_from_description(desc: str | None) -> tuple[float | None, str | None]:
    """Bezala bill_lines returnerar description som fri text i formatet
    "NAMN: VENDOR, PLATS, LAND 28.54 EUR". Plocka belopp + valuta från
    slutet av strängen när det strukturerade amount-fältet saknas."""
    if not desc:
        return None, None
    match = _MISSING_AMOUNT_RE.search(desc)
    if not match:
        return None, None
    amount_str = match.group(1).replace(",", ".")
    try:
        return float(amount_str), match.group(2)
    except ValueError:
        return None, None


def _normalize_missing_receipt(raw: dict) -> dict:
    """Normalisera Bezalas missing_receipt-format till vår UI-shape.

    `id` är det vi skickar tillbaka som `missing_receipt_id` i match-
    requesten. Bezala UI använder bill_line_id i sin POST /attachments,
    så vi prefererar bill_line_id och faller tillbaka till id.

    amount/currency: Bezala returnerar null för bill_lines — plocka från
    description-strängen ("... 28.54 EUR") om strukturerat fält saknas."""
    bill_line_id = raw.get("bill_line_id") or raw.get("id")
    description = (
        raw.get("description")
        or raw.get("merchant")
        or raw.get("name")
        or ""
    )
    amount = raw.get("amount") or raw.get("sum")
    currency = raw.get("currency")
    if amount is None or not currency:
        parsed_amount, parsed_currency = _parse_amount_from_description(description)
        if amount is None:
            amount = parsed_amount
        if not currency:
            currency = parsed_currency
    return {
        "id": bill_line_id,
        "description": description,
        "amount": amount,
        "currency": currency or "EUR",
        "date": (
            raw.get("date")
            or raw.get("transaction_date")
            or raw.get("purchase_date")
        ),
    }


@app.get("/api/bezala/missing-receipts")
def get_bezala_missing_receipts(_: None = Depends(require_auth)):
    """FAS 5.4 — listar korttransaktioner i Bezala som saknar kvitto."""
    try:
        bezala = BezalaClient()
    except BezalaError as exc:
        raise HTTPException(status_code=500, detail=f"Bezala-init: {exc}") from exc
    try:
        rows = bezala.list_missing_receipts()
    except BezalaError as exc:
        logger.warning("Bezala missing_receipts misslyckades: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Bezala missing_receipts: {exc}",
        ) from exc
    finally:
        bezala.close()
    return [_normalize_missing_receipt(r) for r in rows]


@app.get("/api/bezala/match-suggestions")
def get_match_suggestions(
    include_all_messages: bool = False,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """För varje saknat kvitto i Bezala: hitta matchande
    ProcessedMessage-rader baserat på belopp, datum, vendor.

    När `include_all_messages=true` (FAS 8.5a — Travel Tinder) returneras
    en utökad shape:

        {
          "missing_receipts": [{"missing_receipt": ..., "suggestions": ...}],
          "all_messages": [
            {<serialized ProcessedMessage>, "coupled": bool,
             "matched_bill_line_id": str|null}
          ]
        }

    Default-shapen (utan flaggan) bibehålls för bakåtkompatibilitet med
    Kortmatchning-vyn — rena listan av missing-receipt-objekten.
    """
    try:
        bezala = BezalaClient()
    except BezalaError as exc:
        raise HTTPException(status_code=500, detail=f"Bezala-init: {exc}") from exc
    try:
        missing_rows = bezala.list_missing_receipts()
    except BezalaError as exc:
        raise HTTPException(
            status_code=502, detail=f"Bezala missing_receipts: {exc}",
        ) from exc
    finally:
        bezala.close()

    if include_all_messages:
        # Travel Tinder behöver hela kvittolistan inkl. redan kopplade,
        # så användaren kan se historik och välja bland alla rader.
        all_q = (
            db.query(ProcessedMessage)
            .filter(ProcessedMessage.deleted_at.is_(None))
            .filter(ProcessedMessage.status == "saved")
            .order_by(desc(ProcessedMessage.received_at))
            .limit(1000)
        )
        # Suggestion-matchningen ska bara köra mot okopplade kandidater.
        # Bezala_transaction_id är den definitiva källan för "kopplad"-
        # status (kan vara satt även när bezala_upload_status inte är
        # 'success', t.ex. legacy-rader). Båda filtren tillämpas:
        # belt-and-suspenders.
        candidate_dicts = [
            _serialize_message(r)
            for r in all_q.all()
            if r.bezala_upload_status != "success"
            and r.bezala_transaction_id is None
        ]
    else:
        # Bakåtkompatibel shape: ingen all_messages, candidates filtreras strikt.
        candidates_q = (
            db.query(ProcessedMessage)
            .filter(ProcessedMessage.deleted_at.is_(None))
            .filter(ProcessedMessage.status == "saved")
            .filter(ProcessedMessage.bezala_upload_status != "success")
            .filter(ProcessedMessage.bezala_transaction_id.is_(None))
            .order_by(desc(ProcessedMessage.received_at))
            .limit(500)
        )
        candidate_dicts = [_serialize_message(r) for r in candidates_q.all()]

    rate_provider = make_db_rate_provider(db)

    missing_out: list[dict] = []
    for raw in missing_rows:
        missing = _normalize_missing_receipt(raw)
        suggestions = find_matches(
            missing, candidate_dicts, rate_provider=rate_provider,
        )
        missing_out.append({"missing_receipt": missing, "suggestions": suggestions})

    if not include_all_messages:
        return missing_out

    # Bygg om all_messages med coupled-flagga + matched_bill_line_id.
    all_rows = (
        db.query(ProcessedMessage)
        .filter(ProcessedMessage.deleted_at.is_(None))
        .filter(ProcessedMessage.status == "saved")
        .order_by(desc(ProcessedMessage.received_at))
        .limit(1000)
        .all()
    )
    all_messages: list[dict] = []
    for r in all_rows:
        d = _serialize_message(r)
        d["coupled"] = bool(
            r.bezala_upload_status == "success" or r.bezala_transaction_id
        )
        d["matched_bill_line_id"] = r.bezala_transaction_id
        all_messages.append(d)

    return {
        "missing_receipts": missing_out,
        "all_messages": all_messages,
    }


# === DEBUG (BEHÅLLS) — Match Health-rapport ================================
# Persistent analysverktyg (taggat DEBUG men INTE DELETE-MIG): korsrefererar
# Bezala missing_receipts + våra ProcessedMessages + Gmail-historik och
# klassificerar varför varje korttrans inte är matchad. Cachas per process
# 5 min (CACHE_TTL_SECONDS). Se app/services/match_health.py för logik.


@app.get("/api/debug/match-health")
def get_match_health(
    refresh: bool = False,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Analysrapport: klassificerar varje saknat Bezala-kvitto efter
    sannolik orsak (matched_correctly | gmail_miss | no_receipt_exists |
    ai_extraction_wrong | match_algorithm_failed | gmail_error).

    Resultatet cachas 5 min per process. Skicka ?refresh=true för att
    forcera ny fetch.
    """
    from app.services import match_health as match_health_service

    try:
        bezala = BezalaClient()
    except BezalaError as exc:
        raise HTTPException(
            status_code=500, detail=f"Bezala-init: {exc}",
        ) from exc

    gmail = _get_gmail_client_safe()
    rate_provider = make_db_rate_provider(db)
    try:
        report = match_health_service.build_match_health_report(
            db,
            bezala_client=bezala,
            gmail_client=gmail,
            rate_provider=rate_provider,
            normalize_missing_receipt=_normalize_missing_receipt,
            serialize_message=_serialize_message,
            refresh=refresh,
        )
    except BezalaError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Bezala missing_receipts: {exc}",
        ) from exc
    finally:
        bezala.close()

    return report


# === SLUT Match Health ======================================================


# === DELETE-MIG-EFTER-VERIFIERING ===========================================
# Tillfällig diagnostik: undersöker varför PR #20:s html_only-pipeline
# verkar inte köra i prod. Mikko ser "from:skanetrafiken ... has:attachment"
# i Match Health-rapporten — den ska inte längre vara där om html_only-
# patterns är seedade. Endpointen visar exakt DB-state + vad query-
# byggaren faktiskt producerar.


@app.get("/api/debug/html-only-senders-state")
def debug_html_only_senders_state(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Returnerar full diagnostik om html_only_senders-tabellens prod-state.

    Användning: kalla via DevTools-konsol efter login, eller curl med
    sessionscookien. Loggar också resultatet till stdout så det syns
    i Railway-loggar.
    """
    from sqlalchemy import inspect as sa_inspect

    diag: dict = {
        "table_exists": False,
        "row_count": 0,
        "rows": [],
        "seed_task_run": False,
        "active_patterns": [],
        "build_gmail_query_html_only_result": None,
        "settings_app_settings_id": None,
        "diagnosis": "",
    }

    # 1. Tabellen existerar?
    try:
        engine = db.get_bind()
        inspector = sa_inspect(engine)
        diag["table_exists"] = "html_only_senders" in inspector.get_table_names()
    except Exception as exc:  # noqa: BLE001
        diag["diagnosis"] += f"inspector failed: {exc}; "

    # 2. Rader + patterns
    if diag["table_exists"]:
        try:
            from app.models import HtmlOnlySender, MaintenanceTask
            from app.services.html_only_senders import (
                SEED_TASK_NAME, list_active_patterns,
            )
            rows = (
                db.query(HtmlOnlySender)
                .order_by(HtmlOnlySender.id.asc())
                .all()
            )
            diag["row_count"] = len(rows)
            diag["rows"] = [
                {
                    "id": r.id,
                    "sender_pattern": r.sender_pattern,
                    "description": r.description,
                    "is_active": bool(r.is_active),
                    "created_at": (
                        r.created_at.isoformat()
                        if r.created_at is not None else None
                    ),
                }
                for r in rows
            ]
            diag["seed_task_run"] = (
                db.query(MaintenanceTask)
                .filter(MaintenanceTask.name == SEED_TASK_NAME)
                .count() > 0
            )
            diag["active_patterns"] = list_active_patterns(db)
        except Exception as exc:  # noqa: BLE001
            diag["diagnosis"] += f"row fetch failed: {exc}; "

    # 3. Vad PRODUCERAR build_gmail_query_html_only givet dessa patterns?
    #    Detta är samma kod som pipeline anropar.
    try:
        from app.services.settings_service import (
            build_gmail_query_html_only, load_settings,
        )
        app_settings = load_settings(db)
        diag["settings_app_settings_id"] = getattr(app_settings, "id", None)
        diag["build_gmail_query_html_only_result"] = (
            build_gmail_query_html_only(
                app_settings,
                diag["active_patterns"],
                done_label="Bezala-Klar",
            )
        )
    except Exception as exc:  # noqa: BLE001
        diag["diagnosis"] += f"query build failed: {exc}; "

    # 4. Sammanfattande diagnos
    if not diag["table_exists"]:
        diag["diagnosis"] = (
            "FEL: tabellen html_only_senders existerar INTE i DB. "
            "Migration/init_db har inte skapat den. Kolla lifespan-hook."
        ) + (" " + diag["diagnosis"] if diag["diagnosis"] else "")
    elif diag["row_count"] == 0:
        diag["diagnosis"] = (
            "FEL: tabellen finns men är tom. seed_default_html_only_senders "
            f"verkar inte ha körts (seed_task_run={diag['seed_task_run']}). "
            "Kolla _run_once_seed_html_only_senders i lifespan."
        )
    elif not diag["active_patterns"]:
        diag["diagnosis"] = (
            f"FEL: {diag['row_count']} rader finns men ingen är is_active=True. "
            "Skicka PATCH /api/settings/html-only-senders/{id} {is_active: true}."
        )
    elif diag["build_gmail_query_html_only_result"] is None:
        diag["diagnosis"] = (
            "FEL: build_gmail_query_html_only returnerade None trots att "
            "active_patterns har poster. Bugg i query-byggaren."
        )
    elif "has:attachment" in (diag["build_gmail_query_html_only_result"] or ""):
        diag["diagnosis"] = (
            "FEL: query-byggaren producerar fortfarande has:attachment. "
            "Bugg i build_gmail_query_html_only (PR #20)."
        )
    else:
        diag["diagnosis"] = "OK — state ser rätt ut. Kör manuell scan och kolla logs."

    logger.info("html-only-state diagnosis: %s", diag)
    return diag


# === SLUT DELETE-MIG-EFTER-VERIFIERING ======================================


class MatchToBezalaPayload(BaseModel):
    missing_receipt_id: int | str


class DebugTestDraftStatusPayload(BaseModel):
    """FAS 5.26 — body för /api/debug/test-bezala-draft-flow."""
    message_id: int
    missing_receipt_id: int | str


def _do_match_to_bezala(
    *,
    row: "ProcessedMessage",
    bill_line_id: str,
    db: Session,
    bezala: BezalaClient,
) -> dict:
    """Core flow: snapshot bill_line, ladda PDF, bygg params, kör
    attach_file (POST /attachments + PUT /transactions), uppdatera row
    och commit. Returnerar dict med metadata användbar för båda
    callers (prod-endpoint + debug-endpoint).

    Höjer BezalaError eller HTTPException vid fel. Caller ansvarar för
    att stänga bezala-clienten."""
    # C23 Del A — race-condition-skydd: om någon ANNAN ProcessedMessage redan
    # är kopplad till samma bill_line (= samma bezala_transaction_id), neka
    # med 409 så frontend kan visa felmeddelande och refresha listan istället
    # för att tyst skapa en duplikatkoppling. Re-match på SAMMA msg är
    # idempotent och tillåts.
    existing = (
        db.query(ProcessedMessage)
        .filter(ProcessedMessage.bezala_transaction_id == bill_line_id)
        .filter(ProcessedMessage.id != row.id)
        .filter(ProcessedMessage.deleted_at.is_(None))
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "bill_line_already_coupled",
                "existing_message_id": existing.id,
                "existing_vendor": existing.vendor,
                "existing_amount": existing.amount,
                "existing_currency": existing.currency,
                "bezala_transaction_id": bill_line_id,
                "message": (
                    f"Bill line {bill_line_id} är redan kopplad till "
                    f"meddelande {existing.id} "
                    f"({existing.vendor or '—'} "
                    f"{existing.amount if existing.amount is not None else '—'} "
                    f"{existing.currency or ''})"
                ).strip(),
            },
        )

    drive = _get_drive_or_401()

    # Snapshot bill_line-metadata FÖR attach.
    bill_line_snapshot: dict | None = None
    try:
        for raw in bezala.list_missing_receipts() or []:
            normalized = _normalize_missing_receipt(raw)
            if str(normalized.get("id")) == bill_line_id:
                bill_line_snapshot = normalized
                break
    except BezalaError:
        logger.warning(
            "match-to-bezala: kunde inte snapshot:a bill_line_id=%s "
            "(fortsätter utan snapshot)",
            bill_line_id,
        )

    pdf_bytes = drive.download_pdf(row.drive_file_id)
    logger.info(
        "Match-to-bezala: msg_id=%s bill_line_id=%s drive_file_id=%s pdf_bytes=%d",
        row.id, bill_line_id, row.drive_file_id,
        len(pdf_bytes) if pdf_bytes else 0,
    )
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        raise HTTPException(
            status_code=502,
            detail=f"PDF-nedladdning misslyckades för {row.drive_file_id!r}",
        )

    snap = bill_line_snapshot or {}
    effective_amount = snap.get("amount") if snap.get("amount") is not None else row.amount
    effective_currency = (snap.get("currency") or row.currency or "EUR")
    effective_date = snap.get("date") or row.receipt_date

    # C20 safety net — protect against the Lovable→Finnair race where the
    # frontend references a stranger bill_line. We reject SAME-currency
    # pairs whose amounts differ by both >50 EUR AND >50 % of the bigger
    # value, which catches "100 EUR receipt coupled to 554.50 EUR bank row"
    # without breaking FAS 5.24's intentional Finnair Fare-vs-Grand-Total
    # divergence (333 → 366.32 EUR is ~9 % off — legit).
    # Cross-currency pairs (e.g. 245 SEK ↔ 23.14 EUR) are by design — bank
    # row owns the currency — and pass through untouched.
    if (
        bill_line_snapshot is not None
        and row.amount is not None
        and snap.get("amount") is not None
        and (snap.get("currency") or "").upper()
        == (row.currency or "").upper()
    ):
        row_amt = float(row.amount)
        snap_amt = float(snap["amount"])
        diff = abs(snap_amt - row_amt)
        bigger = max(abs(snap_amt), abs(row_amt), 1.0)
        if diff > 50.0 and diff / bigger > 0.5:
            logger.warning(
                "match-to-bezala: AMOUNT MISMATCH — bill_line %s says %s %s "
                "but receipt msg_id=%s says %s %s (diff=%.2f, %.0f%%). "
                "Refusing.",
                bill_line_id, snap["amount"], snap.get("currency"),
                row.id, row.amount, row.currency,
                diff, 100 * diff / bigger,
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Receipt amount {row_amt:.2f} {row.currency or ''} "
                    f"({row.receipt_date or '—'}) does not match bank row "
                    f"{snap_amt:.2f} {snap.get('currency') or ''} "
                    f"({snap.get('date') or '—'}). Please verify the correct "
                    f"bank row is selected."
                ),
            )

    try:
        metadata = fetch_bezala_metadata(bezala)
    except Exception:  # noqa: BLE001
        logger.exception(
            "match-to-bezala: fetch_bezala_metadata kraschade — "
            "fortsätter utan accounts/cost_centers/vat_rates",
        )
        metadata = {"accounts": [], "cost_centers": [], "vat_rates": []}

    try:
        from app.services.bezala_config import list_mappings as _list_mappings
        vendor_mappings = _list_mappings(db)
    except Exception:  # noqa: BLE001
        logger.exception(
            "match-to-bezala: kunde inte ladda bezala_vendor_mappings",
        )
        vendor_mappings = []

    description_override = row.ai_description_en or row.summary

    params = build_receipt_params(
        file_name=row.file_name,
        sender=row.sender,
        vendor=row.vendor,
        category=row.category,
        amount=effective_amount,
        currency=effective_currency,
        receipt_date=effective_date,
        subject=row.subject,
        accounts=metadata["accounts"],
        cost_centers=metadata["cost_centers"],
        vat_rates=metadata["vat_rates"],
        description_override=description_override,
        vendor_mappings=vendor_mappings,
    )

    logger.info(
        "MATCH-TO-BEZALA payload: message_id=%s bill_line_id=%s "
        "description=%r vendor=%r category=%r "
        "effective_amount=%s effective_currency=%s effective_date=%s "
        "(row.amount=%s row.currency=%s row.receipt_date=%s "
        "snap.amount=%s snap.currency=%s snap.date=%s) "
        "credit_account_id=%s vat_lines_count=%d sender=%r",
        row.id, bill_line_id, params["description"], row.vendor,
        row.category, effective_amount, effective_currency, effective_date,
        row.amount, row.currency, row.receipt_date,
        snap.get("amount"), snap.get("currency"), snap.get("date"),
        params.get("credit_account_id"),
        len(params.get("vat_lines_attributes") or []), row.sender,
    )
    attach_result = bezala.attach_file(
        bill_line_id, row.file_name, pdf_bytes,
        description=params["description"],
        date=params["date"],
        credit_account_id=params.get("credit_account_id"),
        vat_lines_attributes=params.get("vat_lines_attributes") or [],
    )

    # FAS 5.27 — Bezala har TVÅ olika ID-rymder för en kortrad-koppling:
    #   bill_line_id   = 2xxxxxx (kortraden i Bezala)
    #   transaction_id = 5xxxxxx (draft-utlägget som filen knyts till)
    # GET /transactions/{bill_line_id} → 403 Access denied
    # GET /transactions/{tx_id}        → 200 OK med state etc.
    # Tidigare (FAS 5.24/5.25/5.26) sparade vi bill_line_id som
    # bezala_transaction_id, vilket fick alla efterföljande PUT-anrop
    # (draft-guard, set state) att 403:a tyst och drafterna hamnade
    # ändå i "Väntar på andras attestering"-listan.
    #
    # attach_file returnerar nu det riktiga transaction_id som
    # `attachment_id` (faller tillbaka till POST-svarets attachment.id
    # eller bill_line_id om Bezala-svaret saknade det). Vi lagrar det
    # värdet.
    row.bezala_transaction_id = str(attach_result.attachment_id)
    row.bezala_upload_status = "success"
    row.bezala_error_message = None
    from datetime import datetime as _dt
    row.matched_at = _dt.utcnow()
    if bill_line_snapshot:
        merchant = bill_line_snapshot.get("description")
        row.bezala_payment_merchant = (
            str(merchant)[:255] if merchant else None
        )
        amt = bill_line_snapshot.get("amount")
        row.bezala_payment_amount = (
            float(amt) if amt is not None else None
        )
        cur = bill_line_snapshot.get("currency")
        row.bezala_payment_currency = (
            str(cur)[:16] if cur else None
        )
        d = bill_line_snapshot.get("date")
        row.bezala_payment_date = str(d)[:32] if d else None
    db.commit()
    logger.info(
        "Kortmatchning klar: msg_id=%s → bill_line_id=%s "
        "transaction_id=%s",
        row.id, bill_line_id, attach_result.attachment_id,
    )
    return {
        "row": row,
        "bill_line_id": bill_line_id,
        "transaction_id": attach_result.attachment_id,
        "bill_line_snapshot": bill_line_snapshot,
        "params": params,
    }


@app.post("/api/messages/{msg_id}/match-to-bezala")
def match_message_to_bezala(
    msg_id: int,
    payload: MatchToBezalaPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Koppla en Drive-PDF till en befintlig Bezala-kortrad (bill_line).

    FAS 5.24 — robust coupling: efter POST /attachments (som länkar PDF
    till bill_line) PUT:ar vi /transactions/{tx_id} med:
      - amount/currency: ALLTID från bill_line (bank_row), inte ai_extracted.
        Detta fixar cross-currency-rader (245 SEK → 23.14 EUR) och Finnair-
        fall där AI plockade Fare i stället för Grand Total.
      - account_id / vat_id: från vendor-mapping (C12).
      - description: ai_description_en → summary → file_name (C8/C12).
      - date: bill_line.date (bank rows date), fallback row.receipt_date."""
    row = db.query(ProcessedMessage).filter(ProcessedMessage.id == msg_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Meddelandet finns inte")
    if not row.drive_file_id or not row.file_name:
        raise HTTPException(
            status_code=400,
            detail="Meddelandet saknar Drive-fil — kan inte koppla till Bezala",
        )

    bill_line_id = str(payload.missing_receipt_id)

    try:
        bezala = BezalaClient()
    except BezalaError as exc:
        raise HTTPException(status_code=500, detail=f"Bezala-init: {exc}") from exc

    try:
        _do_match_to_bezala(
            row=row, bill_line_id=bill_line_id, db=db, bezala=bezala,
        )
        return _serialize_message(row)
    except BezalaError as exc:
        logger.exception(
            "Kortmatchning misslyckades för msg_id=%s bill_line_id=%s",
            msg_id, bill_line_id,
        )
        row.bezala_upload_status = "failed"
        row.bezala_error_message = (f"{exc} | body={exc.body}" if exc.body else str(exc))[:2000]
        db.commit()
        raise HTTPException(status_code=502, detail=f"Bezala attach_file: {exc}") from exc
    finally:
        bezala.close()


@app.post("/api/debug/test-bezala-draft-flow")
def debug_test_bezala_draft_flow(
    payload: DebugTestDraftStatusPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """FAS 5.26 — diagnos-endpoint för match-to-bezala-flödet.

    Kör samma match-to-bezala-flöde som /api/messages/{msg_id}/match-to-
    bezala men:
      1) Sätter `bezala.record_diagnostics = True` så att samtliga
         request- och response-bodies fångas in.
      2) Efter POST /attachments + PUT /transactions körs en GET
         /transactions/{tx_id} så att vi kan inspektera Bezalas
         faktiska state-fält och bekräfta om draft-flaggorna träffade.
      3) Returnerar fullständigt call_log + post-flight-state.

    Använd för att verifiera fix:en mot live-Bezala när Mikko kör en
    test-koppling. Endpointen ÄR INTE en dry-run — den skapar en
    riktig draft (precis som vanliga endpointet). Skillnaden är bara
    att svaret innehåller diagnostik."""
    row = db.query(ProcessedMessage).filter(
        ProcessedMessage.id == payload.message_id,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Meddelandet finns inte")
    if not row.drive_file_id or not row.file_name:
        raise HTTPException(
            status_code=400,
            detail="Meddelandet saknar Drive-fil — kan inte koppla till Bezala",
        )

    bill_line_id = str(payload.missing_receipt_id)

    try:
        bezala = BezalaClient()
    except BezalaError as exc:
        raise HTTPException(status_code=500, detail=f"Bezala-init: {exc}") from exc

    bezala.record_diagnostics = True
    bezala.call_log = []

    response: dict[str, Any] = {
        "message_id": payload.message_id,
        "bill_line_id": bill_line_id,
        "call_log": [],
        "post_flight": None,
        "error": None,
    }

    try:
        try:
            result = _do_match_to_bezala(
                row=row, bill_line_id=bill_line_id, db=db, bezala=bezala,
            )
            tx_id = result["transaction_id"]
        except BezalaError as exc:
            response["error"] = {
                "message": str(exc),
                "status_code": exc.status_code,
                "body": exc.body,
            }
            response["call_log"] = list(bezala.call_log)
            row.bezala_upload_status = "failed"
            row.bezala_error_message = (
                f"{exc} | body={exc.body}" if exc.body else str(exc)
            )[:2000]
            db.commit()
            return response

        # Post-flight verifiering — läs Bezalas state för transaktionen.
        # Logga errors loud så vi ser tydligt om draft-guard inte träffat.
        try:
            tx_state = bezala.get_transaction(tx_id)
            response["post_flight"] = tx_state
            logger.info(
                "DEBUG draft-flow post-flight: tx_id=%s state-fält=%s",
                tx_id,
                {
                    k: tx_state.get(k)
                    for k in (
                        "draft", "state", "status", "is_draft",
                        "submitted", "submitted_at", "approval_status",
                    )
                    if k in tx_state
                },
            )
        except BezalaError as exc:
            response["post_flight"] = {
                "error": str(exc), "status_code": exc.status_code,
                "body": exc.body,
            }

        response["call_log"] = list(bezala.call_log)
        response["transaction_id"] = tx_id
        return response
    finally:
        bezala.close()


class DebugPutOnlyPayload(BaseModel):
    """FAS 5.27 — body för /api/debug/bezala-transaction-put."""
    transaction_id: str
    fields: dict[str, Any]


# Fält som kan trigga skapande av nya attachments/drafts via PUT.
# Vi vill kunna PUT:a state-fält mot stuck-drafter (Finnair O87UJ3J m.fl.)
# UTAN att av misstag skapa nya rader.
_DEBUG_PUT_BLOCKED_FIELDS = frozenset({
    "attachment", "attachments", "attachments_attributes",
    "attachment_id", "attachment_ids",
    "file", "file_data", "attached_file", "files",
    "bill_line", "bill_line_id", "bill_lines",
    "bill_lines_attributes",
    "create", "new_record",
})

_DEBUG_PUT_STATE_KEYS = (
    "draft", "state", "status", "is_draft",
    "submitted", "submitted_at", "approval_status",
    "approved", "approved_at",
)


def _looks_like_attach_field(key: str) -> bool:
    k = key.lower()
    if k in _DEBUG_PUT_BLOCKED_FIELDS:
        return True
    return "attachment" in k or "bill_line" in k


@app.post("/api/debug/bezala-transaction-put")
def debug_bezala_transaction_put(
    payload: DebugPutOnlyPayload,
    _: None = Depends(require_auth),
):
    """FAS 5.27 — PUT-only debug-endpoint för fält-iteration mot
    befintliga Bezala-drafter UTAN att skapa nya.

    Use case: PR #53 (C17) skickar `{draft, state, status, is_draft}`
    som draft-guard men live-test visade att stuck-drafter ändå
    promotades till "Väntar på andras attestering". För att hitta rätt
    fältnamn vill vi PUT:a olika kombinationer mot en redan stuck
    tx_id (t.ex. Finnair O87UJ3J = bezala_transaction_id för msg
    id=633) — utan att skapa nya drafter som Riikka måste rejecta.

    Flow:
      1. GET /transactions/{tx_id} → `pre_flight`
      2. PUT /transactions/{tx_id} med `{"transaction": fields}` →
         `put_call`
      3. GET /transactions/{tx_id} → `post_flight`
      4. Jämför state-fält i pre vs post → `status_changed`

    Sidoeffekter: BARA PUT:en. Ingen POST /attachments, ingen ny
    draft. Endpointen kan ändra state på befintlig draft (avsiktligt
    — vi vill kunna flippa tillbaka stuck-drafter till "Utkast"), men
    aldrig skapa något nytt. Som extra skydd nekas fält som hintar om
    attachment/file/bill_line/create — om de skickas returneras 400.

    Hela call_log (GET + PUT + GET) returneras också via
    `record_diagnostics`."""
    tx_id = str(payload.transaction_id).strip()
    if not tx_id:
        raise HTTPException(
            status_code=400, detail="transaction_id saknas",
        )
    if not payload.fields:
        raise HTTPException(
            status_code=400,
            detail="fields är tom — inget att PUT:a",
        )

    blocked = sorted({
        k for k in payload.fields if _looks_like_attach_field(k)
    })
    if blocked:
        raise HTTPException(
            status_code=400,
            detail=(
                "attach-/create-kodade fält nekas av PUT-only-endpoint: "
                f"{blocked}"
            ),
        )

    try:
        bezala = BezalaClient()
    except BezalaError as exc:
        raise HTTPException(
            status_code=500, detail=f"Bezala-init: {exc}",
        ) from exc

    bezala.record_diagnostics = True
    bezala.call_log = []

    response: dict[str, Any] = {
        "transaction_id": tx_id,
        "pre_flight": None,
        "put_call": None,
        "post_flight": None,
        "status_changed": False,
        "call_log": [],
    }

    try:
        try:
            response["pre_flight"] = bezala.get_transaction(tx_id)
        except BezalaError as exc:
            response["pre_flight"] = {
                "error": str(exc),
                "status_code": exc.status_code,
                "body": exc.body,
            }
            response["call_log"] = list(bezala.call_log)
            return response

        try:
            put_result = bezala.raw_put_transaction(tx_id, payload.fields)
            response["put_call"] = {
                "request_fields": dict(payload.fields),
                "status_code": put_result["status_code"],
                "body": put_result["body"],
            }
        except BezalaError as exc:
            response["put_call"] = {
                "request_fields": dict(payload.fields),
                "error": str(exc),
                "status_code": exc.status_code,
                "body": exc.body,
            }
            response["call_log"] = list(bezala.call_log)
            return response

        try:
            response["post_flight"] = bezala.get_transaction(tx_id)
        except BezalaError as exc:
            response["post_flight"] = {
                "error": str(exc),
                "status_code": exc.status_code,
                "body": exc.body,
            }

        pre = response["pre_flight"]
        post = response["post_flight"]
        if (
            isinstance(pre, dict) and isinstance(post, dict)
            and "error" not in pre and "error" not in post
        ):
            response["status_changed"] = any(
                pre.get(k) != post.get(k)
                for k in _DEBUG_PUT_STATE_KEYS
                if k in pre or k in post
            )

        response["call_log"] = list(bezala.call_log)
        return response
    finally:
        bezala.close()


# --- FAS 8.5 — Travel Tinder: Matchade-vyn -----------------------------


_MATCH_TIME_SAVED_MINUTES_PER_PAIR = 10


def _matched_pairs_period_cutoff(period: str):
    """Konvertera 'period'-strängen till ett ISO-cutoff-datetime eller
    None när period='all'."""
    from datetime import datetime as _dt, timedelta as _td
    days = {"7d": 7, "30d": 30, "90d": 90}.get(period)
    if days is None:
        return None
    return _dt.utcnow() - _td(days=days)


@app.get("/api/bezala/matched-pairs")
def get_matched_pairs(
    period: str = "30d",
    search: str | None = None,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Listar redan matchade par (kvitto ↔ Bezala-bill_line) sorterat
    på matched_at desc.

    OBS: payment-data hämtas inte från Bezala API i denna FAS — vi
    saknar en endpoint för att hämta info om redan-kopplade bill_lines.
    Pairs returneras med kvitto-data och `bezala_transaction_id`. UI:t
    visar receipt-fälten + tx-id; om Bezala-integration utökas med en
    bill_line-fetch kan payment-objektet fyllas på i en framtida FAS.

    Stats:
      total_all_time      — antal kvitton som någonsin matchats
      this_week           — matchade senaste 7 dagarna
      estimated_minutes_saved — total * 10 min per par (defensiv heuristik)
    """
    from datetime import datetime as _dt, timedelta as _td

    q = (
        db.query(ProcessedMessage)
        .filter(ProcessedMessage.deleted_at.is_(None))
        .filter(ProcessedMessage.bezala_transaction_id.is_not(None))
    )

    period_norm = (period or "30d").strip().lower()
    cutoff = _matched_pairs_period_cutoff(period_norm)
    if cutoff is not None:
        # OBS: matched_at saknas på legacy-rader (matchades innan
        # kolumnen införandes). Sätt period='all' för att se dem.
        q = q.filter(ProcessedMessage.matched_at >= cutoff)

    needle = (search or "").strip().lower()
    if needle:
        like = f"%{needle}%"
        # Sök på både kvitto-vendor OCH bill_line-merchant-snapshot,
        # eftersom de ofta ser olika ut ("Finnair Oyj" vs
        # "MIKKO: FINNAIR HEL-ARN, VANTAA, FI").
        q = q.filter(
            or_(
                func.lower(ProcessedMessage.vendor).like(like),
                func.lower(ProcessedMessage.bezala_payment_merchant).like(like),
            )
        )

    rows = (
        q.order_by(
            desc(ProcessedMessage.matched_at),
            desc(ProcessedMessage.id),
        )
        .limit(500)
        .all()
    )

    pairs: list[dict] = []
    for r in rows:
        pairs.append({
            "message_id": r.message_id,
            "id": r.id,
            "receipt": {
                "vendor": r.vendor,
                "file_name": r.file_name,
                "amount": r.amount,
                "currency": r.currency,
                "receipt_date": r.receipt_date,
                "drive_file_id": r.drive_file_id,
                "drive_link": r.drive_link,
                "subject": r.subject,
                "sender": r.sender,
            },
            "payment": {
                "id": r.bezala_transaction_id,
                "merchant": r.bezala_payment_merchant,
                "amount": r.bezala_payment_amount,
                "currency": r.bezala_payment_currency,
                "date": r.bezala_payment_date,
            },
            "bezala_transaction_id": r.bezala_transaction_id,
            "matched_at": r.matched_at.isoformat() if r.matched_at else None,
        })

    # Stats — alltid mot hela datasetet, inte bara filtrerat
    total_all_time = (
        db.query(func.count(ProcessedMessage.id))
        .filter(ProcessedMessage.deleted_at.is_(None))
        .filter(ProcessedMessage.bezala_transaction_id.is_not(None))
        .scalar() or 0
    )
    week_cutoff = _dt.utcnow() - _td(days=7)
    this_week = (
        db.query(func.count(ProcessedMessage.id))
        .filter(ProcessedMessage.deleted_at.is_(None))
        .filter(ProcessedMessage.bezala_transaction_id.is_not(None))
        .filter(ProcessedMessage.matched_at >= week_cutoff)
        .scalar() or 0
    )

    return {
        "pairs": pairs,
        "total": len(pairs),
        "stats": {
            "total_all_time": int(total_all_time),
            "this_week": int(this_week),
            "estimated_minutes_saved": (
                int(total_all_time) * _MATCH_TIME_SAVED_MINUTES_PER_PAIR
            ),
        },
    }


@app.post("/api/bezala/unmatch/{message_id}")
def unmatch_receipt(
    message_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """FAS 8.5 / C22 — frikoppla ett kvitto från dess Bezala-tx.

    Steg 1: anropa Bezala DELETE /transactions/{id} så att utkastet städas
    bort i Bezala (annars blir det orphan i "Väntar på attestering").
    Steg 2: rensa bezala_transaction_id + matched_at + payment-snapshot
    lokalt så att kvittot dyker upp som AI-förslag igen i Travel Tinder.

    Idempotens: om Bezala svarar 404 (utkastet redan borta) → fortsätt
    ändå med lokal rensning. Vid 5xx eller andra Bezala-fel → behåll
    lokal koppling intakt så användaren kan försöka igen.
    """
    if not message_id or not message_id.strip():
        raise HTTPException(status_code=400, detail="message_id saknas")
    msg = (
        db.query(ProcessedMessage)
        .filter(ProcessedMessage.message_id == message_id)
        .first()
    )
    if msg is None:
        raise HTTPException(status_code=404, detail="Meddelandet finns inte")
    if not msg.bezala_transaction_id:
        raise HTTPException(
            status_code=400, detail="Meddelandet är inte kopplat",
        )
    old_tx = msg.bezala_transaction_id

    # Steg 1 — radera i Bezala FÖRST. Misslyckas detta (≠ 404) avbryter vi
    # innan lokal rensning så användaren kan retry:a.
    try:
        bezala = BezalaClient()
    except BezalaError as exc:
        logger.exception("Bezala-init misslyckades i unmatch (tx=%s)", old_tx)
        raise HTTPException(
            status_code=500, detail=f"Bezala-init: {exc}",
        ) from exc
    try:
        delete_result = bezala.delete_transaction(old_tx)
    except BezalaError as exc:
        logger.error(
            "Unmatch: Bezala DELETE /transactions/%s misslyckades — "
            "behåller lokal koppling: %s | body=%s",
            old_tx, exc, exc.body,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Kunde inte radera utkastet i Bezala",
                "bezala_status": exc.status_code,
                "bezala_body": exc.body,
                "local_state": "unchanged",
            },
        ) from exc
    finally:
        bezala.close()

    bezala_already_gone = bool(delete_result.get("already_gone"))

    # Steg 2 — rensa lokalt nu när Bezala bekräftat (eller redan saknade) utkastet.
    msg.bezala_transaction_id = None
    msg.matched_at = None
    # Återställ status så kvittot dyker upp som okopplat i suggestions
    msg.bezala_upload_status = "pending"
    # Rensa Bezala bill_line-snapshot
    msg.bezala_payment_merchant = None
    msg.bezala_payment_amount = None
    msg.bezala_payment_currency = None
    msg.bezala_payment_date = None
    db.commit()
    logger.info(
        "Unmatched message_id=%s från bezala_transaction_id=%s (bezala_already_gone=%s)",
        message_id, old_tx, bezala_already_gone,
    )
    return {
        "success": True,
        "message_id": message_id,
        "old_bezala_transaction_id": old_tx,
        "bezala_deleted": True,
        "bezala_already_gone": bezala_already_gone,
    }


class UploadToBezalaPayload(BaseModel):
    """Valfri override för Bezala-upload. Granska-vyn skickar redigerade
    värden här så användaren kan fylla i saknat belopp/datum/etc utan
    att behöva ändra DB-raden separat."""
    amount: float | None = None
    vendor: str | None = None
    receipt_date: str | None = None  # 'YYYY-MM-DD'
    currency: str | None = None
    category: str | None = None


@app.post("/api/messages/{msg_id}/upload-to-bezala")
def upload_message_to_bezala(
    msg_id: int,
    payload: UploadToBezalaPayload | None = None,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    row = db.query(ProcessedMessage).filter(ProcessedMessage.id == msg_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Meddelandet finns inte")
    if not row.drive_file_id or not row.file_name:
        raise HTTPException(
            status_code=400,
            detail="Meddelandet saknar Drive-fil — kan inte ladda upp till Bezala",
        )

    # Redigerade värden från Granska-vyn överstyr DB. Vi commit:ar dem
    # INNAN uppladdningen så DB alltid speglar vad som skickats — om
    # Bezala 422:ar kan användaren läsa det från bezala_error_message.
    if payload:
        # FAS 8 — fånga gamla värden FÖRE override så vi kan logga
        # auto-correction för fält som faktiskt ändras.
        old_values = {
            "vendor": row.vendor,
            "amount": row.amount,
            "receipt_date": row.receipt_date,
            "category": row.category,
            "currency": row.currency,
        }
        if payload.amount is not None:
            row.amount = payload.amount
        if payload.vendor is not None:
            row.vendor = payload.vendor
        if payload.receipt_date is not None:
            row.receipt_date = payload.receipt_date
        if payload.currency is not None:
            row.currency = payload.currency
        if payload.category is not None:
            row.category = payload.category
        # Logga rättelser för fält som faktiskt ändrades (defensivt —
        # feedback-loop ska aldrig blockera Bezala-uploaden).
        try:
            from app.services.feedback import save_correction
            for fld, old_val in old_values.items():
                new_val = getattr(row, fld, None)
                if old_val == new_val:
                    continue
                # Hoppa rena None→None eller None→tomma värden
                if new_val is None:
                    continue
                save_correction(
                    db,
                    row.message_id or "",
                    fld,
                    str(old_val) if old_val is not None else None,
                    str(new_val) if new_val is not None else None,
                )
        except Exception:  # noqa: BLE001
            logger.exception("Auto-correction-feedback misslyckades msg_id=%s", msg_id)
        db.commit()
        db.refresh(row)

    # Efter eventuell override: kontroll för fält Bezala kräver
    if not row.receipt_date:
        raise HTTPException(
            status_code=400,
            detail=(
                "Kvittot saknar datum. Fyll i datumet manuellt i Granska-vyn "
                "innan överföring till Bezala."
            ),
        )
    if row.amount is None or row.amount == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "Kvittot saknar belopp. Fyll i beloppet manuellt i Granska-vyn "
                "innan överföring till Bezala."
            ),
        )

    drive = _get_drive_or_401()

    try:
        bezala = BezalaClient()
    except BezalaError as exc:
        logger.exception("Bezala-klient kunde inte initialiseras")
        row.bezala_upload_status = "failed"
        row.bezala_error_message = str(exc)
        db.commit()
        raise HTTPException(status_code=500, detail=f"Bezala-init: {exc}") from exc

    try:
        pdf_bytes = drive.download_pdf(row.drive_file_id)
        logger.info(
            "Bezala-upload: msg_id=%s drive_file_id=%s pdf_bytes=%d",
            msg_id, row.drive_file_id, len(pdf_bytes) if pdf_bytes else 0,
        )
        if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
            # download_pdf har redan strikta kontroller, men failsafe om
            # någon ersätter implementationen framöver.
            msg = (
                f"PDF-nedladdning misslyckades för drive_file_id={row.drive_file_id!r}: "
                f"fick {len(pdf_bytes) if pdf_bytes else 0} bytes"
            )
            row.bezala_upload_status = "failed"
            row.bezala_error_message = msg
            db.commit()
            raise HTTPException(status_code=502, detail=msg)
        metadata = fetch_bezala_metadata(bezala)
        # FAS 5.10 — hämta vendor→account+VAT-overrides från config-tabellen.
        from app.services.bezala_config import list_mappings as _list_mappings
        try:
            vendor_mappings = _list_mappings(db)
        except Exception:  # noqa: BLE001 — config-fel får inte blockera upload
            logger.exception(
                "Kunde inte ladda bezala_vendor_mappings vid manuell upload",
            )
            vendor_mappings = []
        # FAS 5.9 — prioritera engelsk AI-beskrivning, fall tillbaka på
        # svensk summary för legacy-rader (innan ai_description_en fanns).
        description_override = row.ai_description_en or row.summary
        params = build_receipt_params(
            file_name=row.file_name,
            sender=row.sender,
            vendor=row.vendor,
            category=row.category,
            amount=row.amount,
            currency=row.currency,
            receipt_date=row.receipt_date,
            subject=row.subject,
            accounts=metadata["accounts"],
            cost_centers=metadata["cost_centers"],
            vat_rates=metadata["vat_rates"],
            description_override=description_override,
            vendor_mappings=vendor_mappings,
        )
        # Förbereder för PR 2 (mappnings-jämförelse) — logga komplett
        # params-payload så vi kan se exakt vad Bezala får i upload-flödet.
        import json as _json
        try:
            payload_dump = _json.dumps(params, default=str, ensure_ascii=False)
        except Exception:  # noqa: BLE001 — log-bara, aldrig kraschar uploaden
            payload_dump = repr(params)
        logger.info(
            "UPLOAD-TO-BEZALA payload: message_id=%s params=%s",
            msg_id, payload_dump,
        )
        # OBS: vat_lines kan vara [] — Bezala plockar default moms från
        # kontot själv när default_vat_id är null. Vi blockerar INTE här.
        receipt = bezala.upload_receipt(
            filename=row.file_name,
            pdf_bytes=pdf_bytes,
            description=params["description"],
            date=params["date"],
            credit_account_id=params.get("credit_account_id"),
            vat_lines_attributes=params.get("vat_lines_attributes", []),
        )
        row.bezala_transaction_id = receipt.attachment_id
        row.bezala_upload_status = "success"
        row.bezala_error_message = None
        db.commit()
        logger.info(
            "Manuell Bezala-upload klar: msg_id=%s receipt_id=%s",
            msg_id, receipt.attachment_id,
        )
        return _serialize_message(row)
    except BezalaError as exc:
        logger.exception("Manuell Bezala-upload misslyckades för msg_id=%s", msg_id)
        row.bezala_upload_status = "failed"
        # Bevara response body i felmeddelandet så UI kan visa feldetaljer
        detail = f"{exc}"
        if exc.body:
            detail = f"{exc} | body={exc.body}"
        row.bezala_error_message = detail[:2000]
        db.commit()
        raise HTTPException(
            status_code=502, detail=f"Bezala-upload misslyckades: {exc}"
        ) from exc
    finally:
        bezala.close()


@app.get("/api/runs")
def list_runs(
    limit: int = 20,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    rows = db.query(ScanRun).order_by(desc(ScanRun.started_at)).limit(limit).all()
    return [
        {
            "id": r.id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "messages_found": r.messages_found,
            "messages_processed": r.messages_processed,
            "messages_skipped": r.messages_skipped,
            "errors": r.errors,
            "status": r.status,
            "notes": r.notes,
            # NULL i äldre körningar → [] i API-svaret
            "filtered_messages": list(r.filtered_messages or []),
        }
        for r in rows
    ]


@app.get("/api/stats")
def stats(db: Session = Depends(get_db), _: None = Depends(require_auth)):
    total = db.query(func.count(ProcessedMessage.id)).scalar() or 0
    saved = (
        db.query(func.count(ProcessedMessage.id))
        .filter(ProcessedMessage.status == "saved")
        .scalar()
        or 0
    )
    errors = (
        db.query(func.count(ProcessedMessage.id))
        .filter(ProcessedMessage.status == "error")
        .scalar()
        or 0
    )
    last_run = db.query(ScanRun).order_by(desc(ScanRun.started_at)).first()
    return {
        "total": total,
        "saved": saved,
        "errors": errors,
        "last_run": {
            "started_at": last_run.started_at.isoformat() if last_run else None,
            "finished_at": last_run.finished_at.isoformat()
            if last_run and last_run.finished_at
            else None,
            "status": last_run.status if last_run else None,
            "messages_processed": last_run.messages_processed if last_run else 0,
        },
    }


@app.get("/api/debug/scan-sender")
def debug_scan_sender(
    sender: str,
    limit: int = 20,
    _: None = Depends(require_auth),
):
    """Debug-endpoint: bygger en Gmail-query med BARA from:<sender>
    (inga exclusion-filter, ingen done-label, inga category-exkluderingar)
    och returnerar metadata för matchande mail. Används för att
    felsöka varför en avsändare inte plockas upp av ordinarie scan."""
    sender_clean = (sender or "").strip()
    if not sender_clean:
        raise HTTPException(status_code=400, detail="sender saknas")
    limit = max(1, min(int(limit or 20), 100))

    query = f"from:{sender_clean}"
    try:
        gmail = GmailClient()
    except Exception as exc:
        logger.exception("debug/scan-sender: Gmail-init misslyckades")
        raise HTTPException(status_code=500, detail=f"Gmail-init: {exc}") from exc

    try:
        ids = gmail.list_candidate_message_ids(query=query, max_results=limit)
        messages: list[dict] = []
        for mid in ids:
            try:
                meta = gmail.fetch_message_metadata(mid)
            except Exception as exc:
                logger.warning("debug/scan-sender: metadata %s misslyckades: %s", mid, exc)
                continue
            labels = meta.get("labels") or []
            messages.append({
                "message_id": meta["message_id"],
                "sender": meta["sender"],
                "subject": meta["subject"],
                "date": meta["date"],
                "labels": labels,
                "categories": [l for l in labels if l.startswith("CATEGORY_")],
                "snippet": meta["snippet"],
            })
        logger.info(
            "debug/scan-sender: sender=%r query=%r returned=%d",
            sender_clean, query, len(messages),
        )
        return {"query": query, "count": len(messages), "messages": messages}
    except Exception as exc:
        logger.exception("debug/scan-sender misslyckades")
        raise HTTPException(status_code=502, detail=f"Gmail debug: {exc}") from exc


@app.get("/api/debug/html-to-pdf")
def debug_html_to_pdf(
    message_id: str,
    _: None = Depends(require_auth),
):
    """Debug-endpoint: hämtar body_html från Gmail för ett givet
    message_id och kör html_to_pdf på det. Returnerar diagnos + PDF-
    storlek vid framgång, eller stack trace + HTML-struktur vid fel.

    Används för att felsöka varför HTML→PDF misslyckas (Skånetrafiken,
    Moovy) utan att behöva trigga en hel scan.

    Alla fel serialiseras till JSON (aldrig 500 HTML-sida) så browsern
    kan visa dem direkt.
    """
    import traceback
    from app.services.html_pdf_converter import _html_diagnostics

    mid = (message_id or "").strip()
    if not mid:
        return {"ok": False, "stage": "input", "error": "message_id saknas"}

    try:
        gmail = GmailClient()
    except Exception as exc:  # noqa: BLE001
        logger.exception("debug/html-to-pdf: Gmail-init misslyckades")
        return {
            "ok": False,
            "stage": "gmail_init",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        }

    try:
        msg = gmail.fetch_message(mid)
    except Exception as exc:  # noqa: BLE001
        logger.exception("debug/html-to-pdf: fetch_message misslyckades för %s", mid)
        return {
            "ok": False,
            "stage": "gmail_fetch",
            "message_id": mid,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        }

    source = (msg.body_html or "").strip()
    try:
        diag = _html_diagnostics(source) if source else {
            "len": 0, "head": "", "link_rel_stylesheet": 0, "style_tags": 0,
            "img_tags": 0, "external_img_https": 0, "external_img_http": 0,
            "script_tags": 0, "svg_tags": 0, "has_doctype": False,
        }
    except Exception as exc:  # noqa: BLE001
        diag = {"error": f"diagnostics failed: {exc}"}

    base = {
        "message_id": mid,
        "sender": msg.sender,
        "subject": msg.subject,
        "has_html": bool(source),
        "has_text": bool(msg.body_text),
        "body_text_len": len(msg.body_text or ""),
        "diagnostics": diag,
    }

    try:
        pdf = html_to_pdf(msg.body_html or None, plain_text_fallback=msg.body_text or None)
        return {
            **base,
            "ok": True,
            "pdf_bytes": len(pdf),
            "pdf_starts_with": pdf[:8].decode("latin-1", errors="replace"),
        }
    except HtmlToPdfError as exc:
        logger.warning("debug/html-to-pdf misslyckades för %s: %s", mid, exc)
        return {
            **base,
            "ok": False,
            "stage": "html_to_pdf",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("debug/html-to-pdf: oväntat fel för %s", mid)
        return {
            **base,
            "ok": False,
            "stage": "unexpected",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        }


@app.get("/api/debug/weasyprint-env")
def debug_weasyprint_env(_: None = Depends(require_auth)):
    """Debug: returnerar runtime-miljön så vi kan jämföra med vad
    scan-pipelinen ser. Inkluderar pid, thread, LD_LIBRARY_PATH,
    ctypes.util.find_library för gobject/pango/cairo, samt ett
    smoke-test där weasyprint försöker konvertera ett minimalt HTML."""
    import traceback
    from app.services.html_pdf_converter import (
        HtmlToPdfError, _runtime_diagnostics, html_to_pdf,
    )
    runtime = _runtime_diagnostics()
    try:
        pdf = html_to_pdf("<html><body><p>smoke</p></body></html>")
        return {
            "ok": True,
            "runtime": runtime,
            "smoke_pdf_bytes": len(pdf),
        }
    except HtmlToPdfError as exc:
        return {
            "ok": False,
            "runtime": runtime,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "runtime": runtime,
            "stage": "unexpected",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        }


@app.get("/api/debug/error-rows")
def debug_error_rows(
    limit: int = 20,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Debug: listar de senaste error-raderna med deras message_id,
    sender, subject, status, error_message. Bättre än att öppna
    drawer:n per rad för att se vad som krashade."""
    limit = max(1, min(int(limit or 20), 100))
    rows = (
        db.query(ProcessedMessage)
        .filter(ProcessedMessage.status == "error")
        .order_by(desc(ProcessedMessage.processed_at))
        .limit(limit)
        .all()
    )
    return {
        "count": len(rows),
        "rows": [
            {
                "id": r.id,
                "message_id": r.message_id,
                "sender": r.sender,
                "subject": r.subject,
                "received_at": r.received_at.isoformat() if r.received_at else None,
                "processed_at": r.processed_at.isoformat() if r.processed_at else None,
                "error_message": r.error_message,
            }
            for r in rows
        ],
    }


@app.get("/api/debug/sanitized-body")
def debug_sanitized_body(
    msg_id: int,
    _: None = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Debug: returnerar både raw body_html och saniterad output för en
    ProcessedMessage-rad. Används för att verifiera att sanitize_html
    gör rätt (ingen tom data:, inga cid:-URLs, inga <link>-fetches)."""
    import traceback

    try:
        row = db.query(ProcessedMessage).filter(ProcessedMessage.id == msg_id).first()
        if row is None:
            return {"ok": False, "stage": "db", "error": f"msg_id {msg_id} saknas"}
        if not row.message_id:
            return {"ok": False, "stage": "db", "error": "raden saknar Gmail message_id"}

        gmail = GmailClient()
        msg = gmail.fetch_message(row.message_id)
        raw_html = msg.body_html or ""
        raw_text = msg.body_text or ""
        sanitized = sanitize_html(raw_html)
        links = extract_links(raw_html)

        return {
            "ok": True,
            "msg_id": msg_id,
            "gmail_message_id": row.message_id,
            "sender": msg.sender,
            "subject": msg.subject,
            "raw_html_len": len(raw_html),
            "raw_html_head": raw_html[:1000],
            "raw_text_len": len(raw_text),
            "sanitized_len": len(sanitized),
            "sanitized_head": sanitized[:2000],
            "sanitized_full": sanitized if len(sanitized) <= 20000 else None,
            "links_count": len(links),
            "links": links[:20],
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("debug/sanitized-body misslyckades för msg_id=%s", msg_id)
        return {
            "ok": False,
            "stage": "unexpected",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        }


# --- FAS 8: feedback-loop endpoints --------------------------------------


class FeedbackThumbsPayload(BaseModel):
    """Body för POST /api/feedback/thumbs.
    fields är bara meningsfullt när is_positive=False (vilka fält var fel).
    Tomt fields-list för thumbs_down → en generell rad utan field_name."""
    message_id: str
    is_positive: bool
    fields: list[str] = Field(default_factory=list)


class FeedbackCorrectionPayload(BaseModel):
    """Body för POST /api/feedback/correction.
    ai_value är valfritt — om frontend inte skickar det plockas det från
    ProcessedMessage (kräver att POST sker INNAN raden uppdateras)."""
    message_id: str
    field_name: str
    ai_value: str | None = None
    correct_value: str


class FeedbackNotAReceiptPayload(BaseModel):
    """Body för POST /api/feedback/not-a-receipt.
    Användaren markerar att hela mailet inte är ett kvitto. Backend
    1) sparar feedback med feedback_type='not_a_receipt' och
    2) soft-deletar ProcessedMessage med delete_reason='user_marked_not_receipt'."""
    message_id: str


@app.post("/api/feedback/thumbs")
def post_feedback_thumbs(
    payload: FeedbackThumbsPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """FAS 8 — explicit 👍/👎 från användaren."""
    from app.services.feedback import save_thumbs
    rows = save_thumbs(
        db,
        payload.message_id,
        payload.is_positive,
        payload.fields,
    )
    db.commit()
    return {"saved": len(rows)}


@app.post("/api/feedback/correction")
def post_feedback_correction(
    payload: FeedbackCorrectionPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """FAS 8 — implicit feedback när användaren rättar ett fält i Granska.
    Om ai_value saknas i body, läses det från ProcessedMessage (kräver att
    POST sker INNAN raden uppdateras av upload-to-bezala)."""
    from app.services.feedback import save_correction
    ai_value = payload.ai_value
    if ai_value is None:
        row = (
            db.query(ProcessedMessage)
            .filter(ProcessedMessage.message_id == payload.message_id)
            .first()
        )
        if row is not None:
            # Frontend-fältnamn kan vara "date" — backend-kolumnen heter receipt_date
            attr = "receipt_date" if payload.field_name == "date" else payload.field_name
            existing_val = getattr(row, attr, None)
            ai_value = str(existing_val) if existing_val is not None else None
    fb = save_correction(
        db,
        payload.message_id,
        payload.field_name,
        ai_value,
        payload.correct_value,
    )
    db.commit()
    return {"saved": bool(fb)}


@app.post("/api/feedback/not-a-receipt")
def post_feedback_not_a_receipt(
    payload: FeedbackNotAReceiptPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """FAS 8.1 — användaren markerar mailet som icke-kvitto.
    Sparar feedback (så AI:n kan filtrera liknande i framtiden) och
    flyttar raden till papperskorgen i samma operation.
    Returnerar 404 om message_id inte finns."""
    from app.services.feedback import save_not_a_receipt
    result = save_not_a_receipt(db, payload.message_id)
    if not result.get("saved"):
        raise HTTPException(status_code=404, detail="Meddelande hittades inte")
    return result


class FeedbackMatchResultPayload(BaseModel):
    """FAS 8.5c — Match/Skip-feedback från Travel Tinder.

    `result`: 'matched' (Match-knappen / manuell modal-bekräftelse) eller
    'skipped' (Skip-knappen). bill_line_id, ai_score och score_breakdown
    är valfria — vid manuell match utan AI-förslag är de None.
    """
    message_id: str
    result: str
    bill_line_id: int | str | None = None
    ai_score: int | None = None
    score_breakdown: dict | None = None


@app.post("/api/feedback/match-result")
def post_feedback_match_result(
    payload: FeedbackMatchResultPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """FAS 8.5c — registrerar om användaren bekräftade eller skippade
    AI:s match-förslag. Grunddata för FAS 9 (AI-regelgenerering).

    Returnerar {"saved": True/False}. Frontend ignorerar alltid svaret —
    feedback ska aldrig blockera kärnflödet."""
    from app.services.feedback import save_match_result, VALID_MATCH_RESULTS

    if not payload.message_id:
        raise HTTPException(status_code=400, detail="message_id saknas")
    if payload.result not in VALID_MATCH_RESULTS:
        raise HTTPException(
            status_code=400,
            detail=(
                "result måste vara 'matched' eller 'skipped', "
                f"fick {payload.result!r}"
            ),
        )
    result = save_match_result(
        db,
        payload.message_id,
        payload.bill_line_id,
        payload.result,
        ai_score=payload.ai_score,
        score_breakdown=payload.score_breakdown,
    )
    db.commit()
    return result


@app.get("/api/feedback/stats")
def get_feedback_stats(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """FAS 8 — aggregerad statistik. Förbereder framtida statistikflik."""
    from app.services.feedback import feedback_stats
    return feedback_stats(db)


# --- FAS 11.1 — Resor (trip-gruppering) ---------------------------------


class TripEditPayload(BaseModel):
    title: str | None = None
    destination: str | None = None
    start_date: str | None = None  # ISO YYYY-MM-DD
    end_date: str | None = None
    description: str | None = None
    add_message_ids: list[str] | None = None
    remove_message_ids: list[str] | None = None


class TripFeedbackPayload(BaseModel):
    feedback_type: str
    details: dict | None = None


def _parse_iso_date_or_400(raw: str | None, field: str):
    if raw is None:
        return None
    from datetime import date as _date, datetime as _dt
    try:
        return _dt.strptime(raw[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"{field} måste vara YYYY-MM-DD, fick {raw!r}",
        )


def _trip_or_404(db: Session, trip_id: int):
    from app.models import Trip
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    if trip is None:
        raise HTTPException(status_code=404, detail="Resa hittades inte")
    return trip


def _list_trips_filtered(db: Session, status: str) -> list[dict]:
    """Hämta trips med given status, applicera excluded_vendors-filter
    via serialize_trip och dölj resor som blir tomma efter filtreringen.

    Resorna ligger kvar i DB — det är bara list-vyn som skippar dem.
    Cleanup-endpointen är det som permanent tömmer."""
    from app.models import Trip
    from app.services.trip_grouper import serialize_trip
    rows = (
        db.query(Trip)
        .filter(Trip.status == status)
        .order_by(Trip.start_date.desc())
        .all()
    )
    serialized = [serialize_trip(db, t) for t in rows]
    return [s for s in serialized if (s.get("message_count") or 0) > 0]


@app.get("/api/trips/suggestions")
def list_trip_suggestions(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Lista AI-genererade resa-förslag som väntar på användarbeslut."""
    return {"trips": _list_trips_filtered(db, "suggested")}


@app.get("/api/trips/active")
def list_active_trips(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Lista aktiva (accepterade) resor."""
    return {"trips": _list_trips_filtered(db, "active")}


@app.get("/api/trips/stats")
def get_trip_stats(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    from app.services.trip_grouper import trip_stats
    return trip_stats(db)


@app.get("/api/trips/{trip_id}")
def get_trip_detail(
    trip_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    from app.services.trip_grouper import serialize_trip
    trip = _trip_or_404(db, trip_id)
    return serialize_trip(db, trip)


@app.post("/api/trips/{trip_id}/accept")
def post_accept_trip(
    trip_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    from app.services.trip_grouper import accept_trip
    trip = _trip_or_404(db, trip_id)
    return accept_trip(db, trip)


@app.post("/api/trips/{trip_id}/reject")
def post_reject_trip(
    trip_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    from app.services.trip_grouper import reject_trip
    trip = _trip_or_404(db, trip_id)
    return reject_trip(db, trip)


@app.patch("/api/trips/{trip_id}")
def patch_trip(
    trip_id: int,
    payload: TripEditPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    from app.services.trip_grouper import edit_trip
    trip = _trip_or_404(db, trip_id)
    start = _parse_iso_date_or_400(payload.start_date, "start_date")
    end = _parse_iso_date_or_400(payload.end_date, "end_date")
    return edit_trip(
        db, trip,
        title=payload.title,
        destination=payload.destination,
        start_date=start,
        end_date=end,
        description=payload.description,
        add_message_ids=payload.add_message_ids or [],
        remove_message_ids=payload.remove_message_ids or [],
    )


@app.delete("/api/trips/{trip_id}")
def delete_trip(
    trip_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Arkivera resa (soft). Kvitton blir ej bortkopplade — de följer
    med resan in i 'archived'-status."""
    from app.services.trip_grouper import archive_trip
    trip = _trip_or_404(db, trip_id)
    return archive_trip(db, trip)


@app.post("/api/trips/cleanup-excluded-vendors")
def post_cleanup_excluded_vendors(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """FAS 11.1+ — permanent städa befintliga trip_messages för vendors
    som matchar någon ExcludedVendor-rad (substring, case-insensitive).
    Resor som blir tomma raderas också. Idempotent: andra körningen ger
    0/0/0.
    """
    from app.models import ProcessedMessage, Trip, TripMessage
    from app.services.excluded_vendors import (
        is_vendor_excluded, list_excluded_vendor_patterns,
    )

    patterns = list_excluded_vendor_patterns(db)
    if not patterns:
        return {
            "removed_messages": 0,
            "affected_trips": 0,
            "deleted_empty_trips": 0,
        }

    try:
        # Substring-matchning är inte uttryckbar i SQL utan multipla
        # OR LIKE-klausuler — enklare att läsa kandidater och filtrera
        # i Python. trip_messages är litet (≤ tusentals rader) så
        # latency är försumbar.
        candidate_rows = (
            db.query(TripMessage, ProcessedMessage)
            .join(
                ProcessedMessage,
                ProcessedMessage.message_id == TripMessage.message_id,
            )
            .filter(TripMessage.removed_at.is_(None))
            .all()
        )
        rows = [
            tm for tm, msg in candidate_rows
            if is_vendor_excluded(msg.vendor, patterns)
        ]
        affected_trip_ids = {r.trip_id for r in rows}
        removed_count = len(rows)
        for r in rows:
            db.delete(r)
        db.flush()

        # Tomma resor: trip_id som inte längre har några aktiva trip_messages
        empty_trips = (
            db.query(Trip)
            .filter(
                ~Trip.id.in_(
                    db.query(TripMessage.trip_id)
                    .filter(TripMessage.removed_at.is_(None))
                    .distinct()
                )
            )
            .all()
        )
        empty_count = len(empty_trips)
        for t in empty_trips:
            db.delete(t)

        db.commit()
        logger.info(
            "Cleanup excluded_vendors: removed_messages=%d affected_trips=%d "
            "deleted_empty_trips=%d patterns=%s",
            removed_count, len(affected_trip_ids), empty_count, sorted(patterns),
        )
        return {
            "removed_messages": removed_count,
            "affected_trips": len(affected_trip_ids),
            "deleted_empty_trips": empty_count,
        }
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("Cleanup excluded_vendors misslyckades")
        raise HTTPException(
            status_code=500, detail=f"Cleanup misslyckades: {exc}",
        ) from exc


@app.post("/api/trips/refresh-suggestions")
def post_refresh_trip_suggestions(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Triggar manuell omanalys. Kör samma logik som det nattliga
    schedulerade jobbet."""
    from app.services.trip_grouper import persist_suggestions, suggest_trips
    suggestions = suggest_trips(db, lookback_days=90)
    saved = persist_suggestions(db, suggestions)
    return {"generated": len(saved)}


@app.post("/api/trips/{trip_id}/feedback")
def post_trip_feedback(
    trip_id: int,
    payload: TripFeedbackPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    from app.services.trip_grouper import save_trip_feedback
    if not payload.feedback_type:
        raise HTTPException(status_code=400, detail="feedback_type saknas")
    trip = _trip_or_404(db, trip_id)
    fb = save_trip_feedback(db, trip, payload.feedback_type, payload.details)
    return {"saved": True, "id": fb.id}


# --- FAS 11.1.1 — Manuell tagging av kvitton till resor ----------------


class LinkMessageToTripPayload(BaseModel):
    trip_id: int


def _processed_message_or_404(db: Session, message_id: str):
    msg = (
        db.query(ProcessedMessage)
        .filter(ProcessedMessage.message_id == message_id)
        .first()
    )
    if msg is None:
        raise HTTPException(status_code=404, detail="Meddelande hittades inte")
    return msg


@app.post("/api/messages/{message_id}/link-to-trip")
def post_link_message_to_trip(
    message_id: str,
    payload: LinkMessageToTripPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Koppla ett kvitto manuellt till en resa. Idempotent — om
    kopplingen redan finns aktivt returneras already_linked=True."""
    from app.models import TripMessage
    from app.services.trip_grouper import recalculate_trip_total

    _processed_message_or_404(db, message_id)
    trip = _trip_or_404(db, payload.trip_id)

    existing = (
        db.query(TripMessage)
        .filter(TripMessage.trip_id == trip.id)
        .filter(TripMessage.message_id == message_id)
        .first()
    )
    already_linked = False
    if existing is not None:
        if existing.removed_at is None:
            already_linked = True
        else:
            existing.removed_at = None
            existing.added_by = "manual"
    else:
        db.add(TripMessage(
            trip_id=trip.id,
            message_id=message_id,
            added_by="manual",
        ))

    db.flush()
    recalculate_trip_total(db, trip)
    db.commit()
    return {"success": True, "already_linked": already_linked}


@app.delete("/api/messages/{message_id}/unlink-from-trip/{trip_id}")
def delete_unlink_message_from_trip(
    message_id: str,
    trip_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Koppla bort ett kvitto från en resa (soft via removed_at)."""
    from app.models import TripMessage
    from app.services.trip_grouper import recalculate_trip_total
    from datetime import datetime as _dt

    trip = _trip_or_404(db, trip_id)
    tm = (
        db.query(TripMessage)
        .filter(TripMessage.trip_id == trip_id)
        .filter(TripMessage.message_id == message_id)
        .filter(TripMessage.removed_at.is_(None))
        .first()
    )
    if tm is None:
        raise HTTPException(
            status_code=404,
            detail="Kvittot är inte kopplat till resan",
        )
    tm.removed_at = _dt.utcnow()
    db.flush()
    recalculate_trip_total(db, trip)
    db.commit()
    return {"success": True}


@app.get("/api/messages/{message_id}/available-trips")
def get_available_trips_for_message(
    message_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Lista aktiva resor som detta kvitto kan kopplas till — filtrerar
    på datum-fönstret ±14 dagar runt kvittots datum, plus eventuell
    redan länkad resa."""
    from datetime import date as _date, datetime as _dt, timedelta as _td
    from app.models import Trip, TripMessage

    msg = _processed_message_or_404(db, message_id)

    msg_date: _date | None = None
    if msg.receipt_date:
        try:
            msg_date = _dt.strptime(msg.receipt_date[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            pass
    if msg_date is None and msg.received_at:
        try:
            msg_date = msg.received_at.date()
        except Exception:  # noqa: BLE001
            msg_date = None

    candidate_trips: list[Trip] = []
    if msg_date is not None:
        lower = msg_date - _td(days=14)
        upper = msg_date + _td(days=14)
        candidate_trips = (
            db.query(Trip)
            .filter(Trip.status.in_(["active", "suggested"]))
            .filter(Trip.start_date <= upper)
            .filter(Trip.end_date >= lower)
            .order_by(Trip.start_date.desc())
            .all()
        )

    # Inkludera även resor där kvittot redan är länkat (även utanför
    # datum-fönstret), så användaren kan koppla bort.
    linked_rows = (
        db.query(TripMessage)
        .filter(TripMessage.message_id == message_id)
        .filter(TripMessage.removed_at.is_(None))
        .all()
    )
    linked_trip_ids = {tm.trip_id for tm in linked_rows}
    if linked_trip_ids:
        already_in_candidates = {t.id for t in candidate_trips}
        extra_ids = linked_trip_ids - already_in_candidates
        if extra_ids:
            extra_trips = (
                db.query(Trip)
                .filter(Trip.id.in_(extra_ids))
                .all()
            )
            candidate_trips.extend(extra_trips)

    added_by_lookup = {tm.trip_id: tm.added_by for tm in linked_rows}

    return {
        "trips": [
            {
                "id": t.id,
                "title": t.title,
                "destination": t.destination,
                "start_date": t.start_date.isoformat() if t.start_date else None,
                "end_date": t.end_date.isoformat() if t.end_date else None,
                "status": t.status,
                "is_linked": t.id in linked_trip_ids,
                "added_by": added_by_lookup.get(t.id),
            }
            for t in candidate_trips
        ]
    }


# --- FAS 11.1.1 — Exkluderade vendors (SaaS-lista) ---------------------


class AddExcludedVendorPayload(BaseModel):
    pattern: str
    description: str | None = None


@app.get("/api/excluded-vendors")
def list_excluded_vendors_endpoint(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    from app.models import ExcludedVendor
    rows = (
        db.query(ExcludedVendor)
        .order_by(ExcludedVendor.added_by.desc(), ExcludedVendor.vendor_pattern)
        .all()
    )
    return {
        "vendors": [
            {
                "id": v.id,
                "pattern": v.vendor_pattern,
                "description": v.description,
                "added_by": v.added_by,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in rows
        ]
    }


@app.post("/api/excluded-vendors")
def add_excluded_vendor_endpoint(
    payload: AddExcludedVendorPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    from app.services.excluded_vendors import add_user_vendor
    if not payload.pattern.strip():
        raise HTTPException(status_code=400, detail="pattern saknas")
    try:
        row, already = add_user_vendor(
            db, payload.pattern, payload.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "id": row.id,
        "pattern": row.vendor_pattern,
        "description": row.description,
        "added_by": row.added_by,
        "already_exists": already,
    }


@app.delete("/api/excluded-vendors/{vendor_id}")
def remove_excluded_vendor_endpoint(
    vendor_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    from app.services.excluded_vendors import remove_vendor
    if not remove_vendor(db, vendor_id):
        raise HTTPException(status_code=404, detail="Vendor finns inte")
    return {"success": True}


# --- HTML-only senders ------------------------------------------------------
# Avsändare vars kvitto kommer som HTML i mail-bodyn istället för som
# PDF-bilaga. Pipeline kör en separat Gmail-query för dessa utan
# has:attachment-filtret och låter html_to_pdf-konverteraren producera
# en PDF av bodyn.


class HtmlOnlySenderPayload(BaseModel):
    sender_pattern: str
    description: str | None = None


class HtmlOnlySenderTogglePayload(BaseModel):
    is_active: bool


@app.get("/api/settings/html-only-senders")
def list_html_only_senders_endpoint(
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    from app.services.html_only_senders import (
        list_html_only_senders, serialize,
    )
    rows = list_html_only_senders(db)
    return {"senders": [serialize(r) for r in rows]}


@app.post("/api/settings/html-only-senders")
def add_html_only_sender_endpoint(
    payload: HtmlOnlySenderPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    from app.services.html_only_senders import add_sender, serialize
    if not (payload.sender_pattern or "").strip():
        raise HTTPException(status_code=400, detail="sender_pattern saknas")
    try:
        row, already = add_sender(
            db, payload.sender_pattern, payload.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {**serialize(row), "already_exists": already}


@app.delete("/api/settings/html-only-senders/{sender_id}")
def remove_html_only_sender_endpoint(
    sender_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    from app.services.html_only_senders import remove_sender
    if not remove_sender(db, sender_id):
        raise HTTPException(status_code=404, detail="Sender finns inte")
    return {"success": True}


@app.patch("/api/settings/html-only-senders/{sender_id}")
def toggle_html_only_sender_endpoint(
    sender_id: int,
    payload: HtmlOnlySenderTogglePayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    from app.services.html_only_senders import serialize, set_active
    row = set_active(db, sender_id, payload.is_active)
    if row is None:
        raise HTTPException(status_code=404, detail="Sender finns inte")
    return serialize(row)


# --- FAS 11.5.1 — Per Diem Calculator ----------------------------------


def _parse_iso_dt_or_400(raw: str | None, field: str):
    """Parsa ISO 8601 datetime. None om saknas."""
    if raw is None:
        return None
    from datetime import datetime as _dt
    s = raw.strip() if isinstance(raw, str) else raw
    if isinstance(s, str) and s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return _dt.fromisoformat(s)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"{field} måste vara ISO 8601, fick {raw!r}",
        )


class CalculatePerDiemPayload(BaseModel):
    departure_home_at: str | None = None
    return_home_at: str | None = None
    destination_country: str | None = None
    meal_toggles: dict[str, bool] | None = None
    year: int | None = None


class UpdatePerDiemPayload(BaseModel):
    meal_toggles: dict[str, bool] | None = None
    destination_country: str | None = None
    departure_home_at: str | None = None
    return_home_at: str | None = None


def _persist_per_diem(trip, calculation: dict) -> None:
    """Skriv beräkning + summa till trip-objektet (kallaren commit:ar)."""
    from decimal import Decimal
    if "error" in calculation:
        return
    trip.per_diem_calculation = calculation
    total = calculation.get("total_amount")
    if total is not None:
        try:
            trip.per_diem_amount = Decimal(str(total))
        except Exception:  # noqa: BLE001
            trip.per_diem_amount = None
    trip.per_diem_currency = calculation.get("currency")


@app.post("/api/trips/{trip_id}/extract-flight-times")
def post_extract_flight_times(
    trip_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Extrahera AI-förslag för avgångs/hemkomst-tider + destinationsland."""
    from app.services.flight_time_extractor import (
        extract_flight_times_from_trip,
    )
    trip = _trip_or_404(db, trip_id)
    return extract_flight_times_from_trip(trip, db)


@app.post("/api/trips/{trip_id}/calculate-per-diem")
def post_calculate_per_diem(
    trip_id: int,
    payload: CalculatePerDiemPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Beräkna traktamente för en resa och spara resultatet på trip-objektet."""
    from app.services.per_diem_calculator import calculate_per_diem
    trip = _trip_or_404(db, trip_id)

    departure = _parse_iso_dt_or_400(payload.departure_home_at, "departure_home_at")
    ret = _parse_iso_dt_or_400(payload.return_home_at, "return_home_at")

    if departure is not None:
        trip.departure_home_at = departure
    if ret is not None:
        trip.return_home_at = ret
    if payload.destination_country is not None:
        cc = payload.destination_country.strip().upper()
        if len(cc) != 2:
            raise HTTPException(
                status_code=400,
                detail=f"destination_country måste vara 2 bokstäver, fick {payload.destination_country!r}",
            )
        trip.destination_country = cc

    if not trip.departure_home_at or not trip.return_home_at:
        raise HTTPException(
            status_code=400,
            detail="Saknar departure_home_at eller return_home_at — skicka i body eller spara först",
        )

    result = calculate_per_diem(
        trip, db,
        year=payload.year,
        meal_toggles=payload.meal_toggles,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    _persist_per_diem(trip, result)
    db.commit()
    return result


@app.get("/api/trips/{trip_id}/per-diem")
def get_per_diem(
    trip_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Hämta sparad beräkning för en resa."""
    trip = _trip_or_404(db, trip_id)
    return {
        "trip_id": trip.id,
        "destination_country": trip.destination_country,
        "departure_home_at": (
            trip.departure_home_at.isoformat() if trip.departure_home_at else None
        ),
        "return_home_at": (
            trip.return_home_at.isoformat() if trip.return_home_at else None
        ),
        "trip_route": trip.trip_route,
        "per_diem_amount": (
            float(trip.per_diem_amount) if trip.per_diem_amount is not None else None
        ),
        "per_diem_currency": trip.per_diem_currency,
        "calculation": trip.per_diem_calculation,
    }


@app.patch("/api/trips/{trip_id}/per-diem")
def patch_per_diem(
    trip_id: int,
    payload: UpdatePerDiemPayload,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Uppdatera trip:s per-diem (mat-toggles, country eller tider).
    Kör om beräkningen och persistera."""
    from app.services.per_diem_calculator import calculate_per_diem
    trip = _trip_or_404(db, trip_id)

    if payload.destination_country is not None:
        cc = payload.destination_country.strip().upper()
        if len(cc) != 2:
            raise HTTPException(
                status_code=400, detail="destination_country måste vara 2 bokstäver",
            )
        trip.destination_country = cc

    departure = _parse_iso_dt_or_400(payload.departure_home_at, "departure_home_at")
    ret = _parse_iso_dt_or_400(payload.return_home_at, "return_home_at")
    if departure is not None:
        trip.departure_home_at = departure
    if ret is not None:
        trip.return_home_at = ret

    if not trip.departure_home_at or not trip.return_home_at:
        raise HTTPException(
            status_code=400,
            detail="Saknar tider på trip — beräkna först eller skicka i payload",
        )

    result = calculate_per_diem(
        trip, db, meal_toggles=payload.meal_toggles,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    _persist_per_diem(trip, result)
    db.commit()
    return result


@app.get("/api/per-diem-rates")
def get_per_diem_rates(
    year: int | None = None,
    db: Session = Depends(get_db),
    _: None = Depends(require_auth),
):
    """Lista alla per-diem-rates. ?year=2026 filtrerar på år."""
    from app.services.per_diem_calculator import list_supported_countries
    from app.models import PerDiemRate
    from datetime import datetime as _dt
    if year is not None:
        return {"year": year, "rates": list_supported_countries(db, year)}
    rows = db.query(PerDiemRate).order_by(
        PerDiemRate.year.desc(), PerDiemRate.country_name.asc(),
    ).all()
    return {
        "rates": [
            {
                "year": r.year,
                "country_code": r.country_code,
                "country_name": r.country_name,
                "full_day_amount": float(r.full_day_amount),
                "half_day_amount": float(r.half_day_amount),
                "currency": r.currency,
                "source": r.source,
            }
            for r in rows
        ]
    }


# SPA-fallback — måste ligga sist så specifika routes (/, /settings, /login,
# /api/*, /health) matchas före. Returnerar index.html för alla client-side
# routes (/review, /log m.fl.) så browser-reload och deep-linking fungerar.
# Okända /api/*- och /assets/*-paths 404-ar som vanligt.
@app.get("/{spa_path:path}")
def spa_fallback(spa_path: str):
    if spa_path.startswith("api/") or spa_path.startswith("assets/"):
        raise HTTPException(status_code=404, detail="Not Found")
    if not FRONTEND_DIST.exists():
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(FRONTEND_DIST / "index.html")
