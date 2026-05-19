"""Bezala API-klient.

Autentisering: POST /auth/token med {email, password} → returnerar
access_token. Token cache:as in-memory med utgångstid och re-hämtas
automatiskt vid 401 eller när den löper ut.

Upload-flöde:
  1. upload_attachment(pdf_bytes, filename) → attachment_id
  2. create_transaction({attachment_ids, vendor, amount, ...}) → transaction_id

Metadata-endpoints (Gate 0-groundwork):
  - list_accounts() → Bezala kontolista (för account_id-mappning)
  - list_cost_centers() → kostnadsställen (för cost_center_id)
  - list_vat_rates() → momssatser (för vat_rate_id)

Retries: 2 försök vid 5xx med exponentiell backoff (1s, 2s).
Timeout: 30 sek per request.

LOGGNING: På varje request loggar vi metod, path och payload-nycklar (inte
värden — de kan innehålla PII). På varje non-2xx loggar vi statuskod +
hela response.text (trunkerat till BODY_LOG_LIMIT). Vid 422 höjer vi
loglevel till ERROR så felet inte drunknar i INFO-strömmen.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30.0
MAX_RETRIES_5XX = 2
TOKEN_SAFETY_MARGIN_SECONDS = 60
# Trunkeringsgräns för loggade/felrapporterade response-bodies. Höjd från
# 500 → 4000 så vi ser hela Bezala-felmeddelandet (viktigt vid 422).
BODY_LOG_LIMIT = 4000

# Multipart-nyckeln där Bezala förväntar sig filen. Default top-level "file"
# eftersom Bezalas controller tycks göra params[:file].tempfile (nested
# 'attachment[file]' gav "undefined method `tempfile' for nil:NilClass" i
# produktion). Overridable via env om vi felar igen och måste experimentera.
FILE_FIELD_NAME = os.environ.get("BEZALA_FILE_FIELD_NAME", "file")


# FAS 5.27 — draft-guard payload för PUT /transactions/{tx_id}.
#
# Bezala-state-fältet är BEKRÄFTAT (via GET /transactions/{id} i C18-
# diagnostiken på live-tx 5005475): värdet för "Utkast / ej skickad" är
# `state: "unapproved"`. INTE `draft`, INTE `status`, INTE `is_draft`,
# INTE `submitted`. Tidigare PR (FAS 5.26 / C17) gissade fyra olika
# Rails-konventioner samtidigt — de var alla fel, vilket är varför
# C15/C16/C17:s fixar inte fungerade.
#
# state="unapproved" är hardcoded — INTE env-styrt. Vi vet nu rätt
# fältnamn och rätt värde; ingen iteration behövs. Om Bezala ändrar
# semantik (osannolikt utan API-versions-bump) gör vi en explicit
# kod-ändring. extra_fields kan inte överstyra detta värde (se
# `update_transaction` nedan).
_DEFAULT_DRAFT_GUARD_FIELDS: dict[str, Any] = {
    "state": "unapproved",
}


def _load_draft_guard_fields() -> dict[str, Any]:
    """FAS 5.27 — returnerar den hårdkodade draft-guard-payloaden.

    Tidigare läste detta `BEZALA_DRAFT_GUARD_FIELDS_JSON` från env, men
    iterationen är klar: vi vet att Bezala vill ha `state: "unapproved"`
    för Utkast. Funktionen behålls (i stället för att inline:as) så att
    debug-endpointen kan introspecta vad guard-payloaden innehåller och
    så att testet `test_default_guard_fields_match_constant` kan
    asserta att vi inte regredierar.
    """
    return dict(_DEFAULT_DRAFT_GUARD_FIELDS)


class BezalaError(RuntimeError):
    """Generic Bezala API error. Bär statuskod och serversvar där tillgängligt."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass
class BezalaAttachment:
    attachment_id: str


@dataclass
class BezalaTransaction:
    transaction_id: str
    url: str | None = None


def _safe_body_snippet(resp: httpx.Response) -> str:
    """Returnerar response.text trunkerad till BODY_LOG_LIMIT tecken.
    Skyddar mot None/binär respons."""
    try:
        text = resp.text or ""
    except Exception:  # noqa: BLE001
        return "<binärt svar — text() kastade>"
    if len(text) > BODY_LOG_LIMIT:
        return text[:BODY_LOG_LIMIT] + f"…<{len(text) - BODY_LOG_LIMIT} tecken kapade>"
    return text


def _log_response(resp: httpx.Response, method: str, path: str, payload_keys: list[str] | None = None) -> None:
    """Strukturerad loggning av Bezala-svar. 4xx/5xx → ERROR, 2xx → DEBUG.
    Payload-nycklar loggas (inte värdena — de kan bära PII)."""
    status = resp.status_code
    keys_str = ",".join(payload_keys) if payload_keys else "—"
    headers_interesting = {
        k: v for k, v in resp.headers.items()
        if k.lower() in ("content-type", "x-request-id", "x-correlation-id", "retry-after")
    }
    if 200 <= status < 300:
        logger.debug(
            "Bezala %s %s → %d | payload_keys=%s | headers=%s",
            method, path, status, keys_str, headers_interesting,
        )
        return
    body = _safe_body_snippet(resp)
    level = logging.ERROR if status in (400, 401, 403, 422, 500, 502, 503) else logging.WARNING
    logger.log(
        level,
        "Bezala %s %s → %d | payload_keys=%s | headers=%s | body=%s",
        method, path, status, keys_str, headers_interesting, body,
    )


class BezalaClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._email = settings.bezala_username
        self._password = settings.bezala_password
        self._base_url = settings.bezala_api_url.rstrip("/")
        self._client = httpx.Client(timeout=TIMEOUT_SECONDS)
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        # FAS 5.26 — diagnostik-capture för /api/debug/test-bezala-draft-flow.
        # När `record_diagnostics` är True loggar varje _request-anrop en
        # entry i `call_log` med request/response-bodies. Default Off så
        # vi inte håller bodies i minnet i normalflödet.
        self.record_diagnostics: bool = False
        self.call_log: list[dict[str, Any]] = []

        if not (self._email and self._password):
            raise BezalaError(
                "BEZALA_USERNAME/BEZALA_PASSWORD saknas — kan inte autentisera mot Bezala."
            )

    # --------- auth ---------

    def _authenticate(self) -> str:
        url = f"{self._base_url}/auth/token"
        logger.info("Autentiserar mot Bezala (%s)", url)
        try:
            resp = self._client.post(
                url,
                json={"email": self._email, "password": self._password},
            )
        except httpx.HTTPError as exc:
            raise BezalaError(f"Bezala-auth: nätverksfel {exc}") from exc

        if resp.status_code >= 400:
            raise BezalaError(
                f"Bezala-auth: {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:500],
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise BezalaError(f"Bezala-auth: icke-JSON svar ({exc})") from exc

        token = data.get("access_token") or data.get("token")
        if not token:
            raise BezalaError(f"Bezala-auth: saknar access_token i svar ({data})")

        expires_in = int(data.get("expires_in") or 3600)
        self._token = token
        self._token_expires_at = time.monotonic() + max(
            60, expires_in - TOKEN_SAFETY_MARGIN_SECONDS
        )
        logger.info("Bezala-token mottagen (giltig ~%ds)", expires_in)
        return token

    def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        return self._authenticate()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}"}

    # --------- requests med retry + 401-refresh ---------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        files: Any | None = None,
        data: Any | None = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None

        # Payload-nycklar för loggning (inte värden — kan bära PII/belopp).
        payload_keys: list[str] = []
        if isinstance(json, dict):
            payload_keys = list(json.keys())
        elif isinstance(data, dict):
            payload_keys = list(data.keys())
        elif files:
            payload_keys = [f"file:{k}" for k in (files.keys() if isinstance(files, dict) else [])]

        for attempt in range(MAX_RETRIES_5XX + 1):
            try:
                headers = self._auth_headers()
                logger.debug(
                    "Bezala → %s %s (försök %d, payload_keys=%s)",
                    method, path, attempt + 1, payload_keys,
                )
                resp = self._client.request(
                    method, url, json=json, files=files, data=data, headers=headers
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "Bezala %s %s: nätverksfel (försök %d): %s",
                    method, path, attempt + 1, exc,
                )
                if attempt >= MAX_RETRIES_5XX:
                    raise BezalaError(f"Bezala {method} {path}: {exc}") from exc
                time.sleep(2 ** attempt)
                continue

            _log_response(resp, method, path, payload_keys)

            # FAS 5.26 — diagnostik-capture (opt-in via record_diagnostics).
            # Sparar request- och response-bodies så att debug-endpointen
            # /api/debug/test-bezala-draft-flow kan returnera dem för
            # live-inspektion av match-to-bezala-flödet. getattr-guard:
            # _make_client i testerna kör inte __init__.
            if getattr(self, "record_diagnostics", False):
                req_body: Any
                if isinstance(json, dict):
                    req_body = json
                elif isinstance(data, dict):
                    req_body = dict(data)
                elif files:
                    req_body = {
                        "files": [
                            k for k in (files.keys() if isinstance(files, dict) else [])
                        ],
                        "data": dict(data) if isinstance(data, dict) else None,
                    }
                else:
                    req_body = None
                try:
                    resp_body_text = resp.text
                except Exception:  # noqa: BLE001
                    resp_body_text = "<binärt svar>"
                self.call_log.append({
                    "method": method,
                    "path": path,
                    "request_body": req_body,
                    "response_status": resp.status_code,
                    "response_body": resp_body_text[:BODY_LOG_LIMIT],
                })

            if resp.status_code == 401:
                # Token kan vara utgången — tvinga ny auth EN gång.
                if attempt == 0:
                    logger.info("Bezala 401 — hämtar ny token och försöker igen.")
                    self._token = None
                    self._token_expires_at = 0.0
                    continue
                raise BezalaError(
                    f"Bezala {method} {path}: 401 efter re-auth",
                    status_code=401,
                    body=_safe_body_snippet(resp),
                )

            if 500 <= resp.status_code < 600 and attempt < MAX_RETRIES_5XX:
                logger.warning(
                    "Bezala %s %s: %d (försök %d, backoff)",
                    method, path, resp.status_code, attempt + 1,
                )
                time.sleep(2 ** attempt)
                continue

            return resp

        # borde inte nås
        raise BezalaError(
            f"Bezala {method} {path}: slut på försök ({last_exc})"
        )

    # --------- public API ---------

    def upload_attachment(self, filename: str, pdf_bytes: bytes) -> BezalaAttachment:
        resp = self._request(
            "POST",
            "/attachments",
            files={"file": (filename, pdf_bytes, "application/pdf")},
        )
        if resp.status_code >= 400:
            raise BezalaError(
                f"Bezala upload_attachment: {resp.status_code}",
                status_code=resp.status_code,
                body=_safe_body_snippet(resp),
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise BezalaError(
                f"Bezala upload_attachment: icke-JSON svar ({exc})"
            ) from exc

        attachment_id = (
            data.get("id")
            or data.get("attachment_id")
            or (data.get("attachment") or {}).get("id")
        )
        if not attachment_id:
            raise BezalaError(
                f"Bezala upload_attachment: saknar id i svar ({data})"
            )
        logger.info("Bezala: laddade upp %s som attachment_id=%s", filename, attachment_id)
        return BezalaAttachment(attachment_id=str(attachment_id))

    def upload_file_as_draft(
        self,
        pdf_bytes: bytes,
        filename: str,
    ) -> tuple[str, str]:
        """Steg 1 i nya two-step-flödet: POST /api/attachments med
        multipart file + draft=1. Bezala skapar automatiskt en draft-
        transaktion och returnerar både attachment_id och transaction_id.

        Returnerar (attachment_id, transaction_id) som strängar."""
        if not filename:
            raise BezalaError("upload_file_as_draft: filename saknas")
        if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
            raise BezalaError(
                "upload_file_as_draft: pdf_bytes är inte en giltig PDF"
            )

        # FAS 5.28 (C21) — skicka state="unapproved" redan i CREATE-formen.
        # Empiriskt (test-session 2026-05-18) räcker det INTE att PUT:a
        # state efter att draften skapats: PUT:en returnerar 500 mot vissa
        # bezala-flöden och drafterna hamnar ändå i "Väntar på andras
        # attestering". Att inkludera state-flaggan i POST /attachments
        # hindrar promotion-by-default redan vid skapelsen.
        logger.info(
            "upload_file_as_draft: POST /attachments filename=%r bytes=%d",
            filename, len(pdf_bytes),
        )
        resp = self._request(
            "POST",
            "/attachments",
            files={"file": (filename, pdf_bytes, "application/pdf")},
            data={"draft": "1", "state": "unapproved"},
        )
        if resp.status_code >= 400:
            raise BezalaError(
                f"Bezala upload_file_as_draft: {resp.status_code}",
                status_code=resp.status_code,
                body=_safe_body_snippet(resp),
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise BezalaError(
                f"Bezala upload_file_as_draft: icke-JSON svar ({exc})"
            ) from exc

        attachment_id = (
            data.get("id")
            or data.get("attachment_id")
            or (data.get("attachment") or {}).get("id")
        )
        transaction_id = data.get("transaction_id")
        if not transaction_id and isinstance(data.get("attachment"), dict):
            transaction_id = data["attachment"].get("transaction_id")
        if not transaction_id and isinstance(data.get("transaction"), dict):
            transaction_id = data["transaction"].get("id")
        if not attachment_id:
            raise BezalaError(
                f"Bezala upload_file_as_draft: saknar attachment id i svar ({data})"
            )
        if not transaction_id:
            raise BezalaError(
                f"Bezala upload_file_as_draft: saknar transaction_id i svar ({data})"
            )
        logger.info(
            "Bezala: draft-upload klar — attachment_id=%s transaction_id=%s",
            attachment_id, transaction_id,
        )
        return (str(attachment_id), str(transaction_id))

    # Submit-coded fältnamn vi alltid skyddar mot — om en caller skickar
    # in dessa via extra_fields ignoreras de tyst (med logg) så vi aldrig
    # auto-submittar en draft från koden. Speglar test_match_to_bezala_
    # payload_does_not_submit-invarianten i tests/backend/test_card_matching.py.
    _BLOCKED_SUBMIT_FIELDS = frozenset({
        "submitted", "is_submitted", "action", "send_for_approval",
        "mark_as_submitted", "submit", "submit_for_approval",
        "approval_status", "approved", "approved_at", "submitted_at",
    })

    def update_transaction(
        self,
        transaction_id: str,
        *,
        description: str,
        date: str,
        credit_account_id: int | str | None = None,
        vat_lines_attributes: list[dict] | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> BezalaTransaction:
        """Steg 2: PUT /api/transactions/{tx_id} med metadata enligt
        API-docs:

          {"transaction": {
            "description": "...",
            "date": "YYYY-MM-DD",
            "credit_account_id": 67100,
            "vat_lines_attributes": [{...}],
            "draft": true,
            "state": "draft",
            "status": "draft",
            "is_draft": true
          }}

        FAS 5.25 (C16) försökte med endast `draft: true` men live-testet
        (Finnair O87UJ3J, 28 april) visade att Bezala fortfarande
        promotade drafter till "Väntar på andras attestering". Fältet
        `draft: true` är troligen inte det Bezala läser på transaction-
        nivå (det är bara form-flaggan som POST /attachments använder).

        FAS 5.26 (C17) — skickar ALLA troliga Rails-konventioner
        samtidigt: `draft`, `state`, `status`, `is_draft`. Operatören
        kan justera via env-var `BEZALA_DRAFT_GUARD_FIELDS_JSON` utan
        kod-deploy. extra_fields kan INTE överstyra guard-flaggorna
        eller smyga in submit-kodande fält (`submitted`, `action` etc).

        Returnerar BezalaTransaction — bekräftat ID från svaret
        (eller tillbaka samma som skickades)."""
        if not transaction_id:
            raise BezalaError("update_transaction: transaction_id saknas")
        if not description:
            raise BezalaError("update_transaction: description saknas")
        if not date:
            raise BezalaError("update_transaction: date saknas (ÅÅÅÅ-MM-DD)")

        # Bygg draft-guard från env (eller default). dict-copy så vi inte
        # muterar default-globalen om operatören vill addera fler fält
        # via env i framtiden.
        draft_guard = _load_draft_guard_fields()

        payload: dict[str, Any] = {
            "description": description,
            "date": date,
        }
        # Lägg guard-fält FÖRST så att senare assignments (credit_account_id,
        # vat_lines, extra_fields) inte råkar överskriva dem.
        payload.update(draft_guard)

        if credit_account_id is not None:
            payload["credit_account_id"] = credit_account_id
        if vat_lines_attributes:
            payload["vat_lines_attributes"] = vat_lines_attributes
        if extra_fields:
            for k, v in extra_fields.items():
                if v is None:
                    continue
                if k in draft_guard:
                    # Skydda guard-fält. Logga och ignorera.
                    logger.warning(
                        "update_transaction: extra_fields försökte sätta "
                        "guard-fält %s=%r — ignoreras, behåller %s=%r",
                        k, v, k, draft_guard[k],
                    )
                    continue
                if k in self._BLOCKED_SUBMIT_FIELDS:
                    # Submit-kodade fält tystas hårt — vi vill aldrig
                    # auto-submitta från koden.
                    logger.warning(
                        "update_transaction: extra_fields försökte sätta "
                        "submit-kodat fält %s=%r — IGNORERAS",
                        k, v,
                    )
                    continue
                payload[k] = v

        wrapped = {"transaction": payload}
        logger.info(
            "update_transaction: PUT /transactions/%s body=%s",
            transaction_id,
            json.dumps(wrapped, ensure_ascii=False, default=str),
        )
        resp = self._request(
            "PUT", f"/transactions/{transaction_id}", json=wrapped,
        )
        # FAS 5.26 — logga ALLTID response-body på PUT (inte bara vid 4xx)
        # så vi kan korrelera draft-guard-fälten mot Bezalas faktiska
        # state-svar.
        logger.info(
            "BEZALA RESPONSE update_transaction: tx_id=%s status=%s body=%s",
            transaction_id, resp.status_code, _safe_body_snippet(resp),
        )
        if resp.status_code >= 400:
            raise BezalaError(
                f"Bezala update_transaction: {resp.status_code}",
                status_code=resp.status_code,
                body=_safe_body_snippet(resp),
            )
        try:
            data = resp.json()
        except ValueError:
            data = {}

        confirmed_id = (
            data.get("id")
            or (data.get("transaction") or {}).get("id")
            or transaction_id
        )
        url = data.get("url") or data.get("web_url") or (data.get("transaction") or {}).get("url")
        logger.info(
            "Bezala: uppdaterade transaction_id=%s description=%r",
            confirmed_id, description,
        )
        return BezalaTransaction(transaction_id=str(confirmed_id), url=url)

    def raw_put_transaction(
        self,
        transaction_id: str | int,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        """FAS 5.27 — debug-only raw PUT /transactions/{tx_id}.

        Skickar `{"transaction": fields}` utan att injicera draft-guard,
        utan att validera description/date, och utan att filtrera mot
        _BLOCKED_SUBMIT_FIELDS. Callern (debug-endpointen) ansvarar för
        att grindvakta input.

        Returnerar `{"status_code": int, "body": <json|text>}`. Höjer
        INTE BezalaError på 4xx/5xx — vi vill kunna inspektera Bezalas
        svar även när det är ett fel (det är hela poängen med
        endpointen). Höjer endast om transaction_id saknas."""
        if not transaction_id:
            raise BezalaError("raw_put_transaction: transaction_id saknas")
        wrapped = {"transaction": dict(fields)}
        logger.info(
            "raw_put_transaction: PUT /transactions/%s body=%s",
            transaction_id,
            json.dumps(wrapped, ensure_ascii=False, default=str),
        )
        resp = self._request(
            "PUT", f"/transactions/{transaction_id}", json=wrapped,
        )
        logger.info(
            "BEZALA RESPONSE raw_put_transaction: tx_id=%s status=%s body=%s",
            transaction_id, resp.status_code, _safe_body_snippet(resp),
        )
        try:
            body: Any = resp.json()
        except ValueError:
            body = _safe_body_snippet(resp)
        return {"status_code": resp.status_code, "body": body}

    def get_transaction(self, transaction_id: str | int) -> dict[str, Any]:
        """GET /transactions/{tx_id} — läser nuvarande state för en
        transaktion. Används av /api/debug/test-bezala-draft-flow för
        post-flight-verifiering av att drafter inte auto-submittas.

        Returnerar rådata-JSON (dict) eller tom dict om svaret inte är
        JSON. Höjer BezalaError vid 4xx/5xx."""
        if not transaction_id:
            raise BezalaError("get_transaction: transaction_id saknas")
        resp = self._request("GET", f"/transactions/{transaction_id}")
        if resp.status_code >= 400:
            raise BezalaError(
                f"Bezala get_transaction: {resp.status_code}",
                status_code=resp.status_code,
                body=_safe_body_snippet(resp),
            )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {"_raw": data}

    def delete_transaction(self, transaction_id: str | int) -> dict[str, Any]:
        """C22 — radera ett utkast/transaktion i Bezala.

        DELETE /api/transactions/{id}. Används från unmatch-endpointen så
        att en frikoppling i Bezala Bot automatiskt städar bort orphan-
        utkast i Bezala (tidigare beteende: lokal rensning, manuell
        radering i Bezala UI).

        Returns:
            {"deleted": True} vid 2xx, eller
            {"deleted": True, "already_gone": True} vid 404 (idempotent —
            utkastet är redan borta, lokal DB kan rensas ändå).

        Raises:
            BezalaError vid andra fel (4xx ≠ 404, 5xx, nätverksfel)."""
        if not transaction_id:
            raise BezalaError("delete_transaction: transaction_id saknas")

        path = f"/transactions/{transaction_id}"
        logger.info("delete_transaction: DELETE %s", path)
        resp = self._request("DELETE", path)

        if resp.status_code == 404:
            logger.warning(
                "Bezala DELETE %s → 404 (redan raderat eller finns inte) — "
                "idempotent, rensar lokalt ändå",
                path,
            )
            return {"deleted": True, "already_gone": True}

        if 200 <= resp.status_code < 300:
            logger.info("Bezala DELETE %s → %d (raderat)", path, resp.status_code)
            return {"deleted": True}

        raise BezalaError(
            f"Bezala delete_transaction: {resp.status_code}",
            status_code=resp.status_code,
            body=_safe_body_snippet(resp),
        )

    # --------- Gate 0 groundwork: metadata-endpoints ---------
    #
    # Dessa läser referensdata från Bezala (konton, kostnadsställen,
    # momssatser) som behövs för att bygga rätt transaction-payload.
    # De används INTE av create_transaction än — först nästa iteration
    # när vi vet exakta fältnamn från Bezala 422-respons.

    def _fetch_list(self, path: str, *, item_keys: tuple[str, ...] = ("items", "data", "results")) -> list[dict]:
        """Generisk GET → list[dict]. Hanterar både toppnivå-lista och
        wrapper-objekt {"items": [...]} / {"data": [...]} / {"results": [...]}.
        Höjer BezalaError på non-2xx."""
        resp = self._request("GET", path)
        if resp.status_code >= 400:
            raise BezalaError(
                f"Bezala GET {path}: {resp.status_code}",
                status_code=resp.status_code,
                body=_safe_body_snippet(resp),
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise BezalaError(
                f"Bezala GET {path}: icke-JSON svar ({exc})"
            ) from exc

        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in item_keys:
                if isinstance(data.get(key), list):
                    return [item for item in data[key] if isinstance(item, dict)]
        logger.warning("Bezala GET %s: oväntad struktur (%s)", path, type(data).__name__)
        return []

    def list_accounts(self) -> list[dict]:
        """Hämtar Bezala-konton. Varje post har typiskt {id, name, code?}."""
        return self._fetch_list("/accounts")

    def list_cost_centers(self) -> list[dict]:
        """Hämtar kostnadsställen. Varje post har typiskt {id, name, default?}."""
        return self._fetch_list("/cost_centers")

    def list_missing_receipts(self) -> list[dict]:
        """Hämtar korttransaktioner utan kvitto från Bezala
        (GET /api/missing_receipts). FAS 5.4 kortmatchning.

        Loggar första rad-objektets nycklar + ID-relaterade fält så vi kan
        verifiera vilket fält som faktiskt heter bill_line_id i svaret."""
        rows = self._fetch_list("/missing_receipts")
        if rows:
            sample = rows[0]
            id_fields = {
                k: sample.get(k)
                for k in ("id", "bill_line_id", "transaction_id")
                if k in sample
            }
            logger.info(
                "missing_receipts: count=%d sample_keys=%s id_fields=%s",
                len(rows), sorted(sample.keys()), id_fields,
            )
        else:
            logger.info("missing_receipts: count=0")
        return rows

    def attach_file(
        self,
        bill_line_id: str | int,
        filename: str,
        pdf_bytes: bytes,
        *,
        description: str | None = None,
        date: str | None = None,
        credit_account_id: int | str | None = None,
        vat_lines_attributes: list[dict] | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> BezalaAttachment:
        """Koppla PDF till befintlig kortrad i Bezala — robust coupling.

        Replikerar UI:s "Koppla till existerande"-flöde:
            POST /api/attachments
            multipart: file=<pdf>, draft=1, bill_line_id=<id>
                       [, description=<text>]
        Bezala skapar en draft-transaction länkad till bill_line och
        returnerar både attachment_id och transaction_id.

        FAS 5.24 — robust coupling: när metadata (date, credit_account_id,
        vat_lines_attributes, extra_fields) skickas in följer vi upp med
        PUT /transactions/{tx_id} så att draft:en får rätt belopp/valuta/
        konto/moms direkt — i stället för att förlita oss på att Bezala
        automatiskt ärver från bill_line (vilket empiriskt inte sker för
        cross-currency-rader och vissa belopp-mismatch-fall)."""
        if not bill_line_id:
            raise BezalaError("attach_file: bill_line_id saknas")
        if not filename:
            raise BezalaError("attach_file: filename saknas")
        if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
            raise BezalaError("attach_file: pdf_bytes är inte en giltig PDF")

        # FAS 5.28 (C21) — sätt state="unapproved" i CREATE-formen så att
        # draften aldrig hamnar i "Väntar på andras attestering" som
        # default. Tidigare (C19) PUT:ade vi state="unapproved" efter att
        # POST /attachments redan skapat draften, men test-sessionen
        # 2026-05-18 visade att PUT:en kan returnera 500 medan POST:en
        # går igenom — drafterna blir då stuck i "reviewing" utan att
        # vi får tillbaka en felmarkering. Att skicka state-flaggan
        # redan i form-datan är "fixen ligger i CREATE-flödet, inte PUT".
        form: dict[str, Any] = {
            "draft": "1",
            "state": "unapproved",
            "bill_line_id": str(bill_line_id),
        }
        if description:
            form["description"] = description

        logger.info(
            "attach_file: POST /attachments bill_line_id=%s filename=%r "
            "bytes=%d form_keys=%s metadata=%s",
            bill_line_id, filename, len(pdf_bytes), sorted(form.keys()),
            bool(date or credit_account_id or vat_lines_attributes or extra_fields),
        )
        resp = self._request(
            "POST",
            "/attachments",
            files={FILE_FIELD_NAME: (filename, pdf_bytes, "application/pdf")},
            data=form,
        )
        # Logga Bezalas svar så vi kan diagnostisera vilka fält som faktiskt
        # sätts på bill_line:n (cost_center, account, vat_code etc).
        logger.info(
            "BEZALA RESPONSE attach_file: status=%s body=%s",
            resp.status_code, _safe_body_snippet(resp),
        )
        if resp.status_code >= 400:
            raise BezalaError(
                f"Bezala attach_file: {resp.status_code}",
                status_code=resp.status_code,
                body=_safe_body_snippet(resp),
            )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        attachment_id = (
            data.get("id")
            or data.get("attachment_id")
            or (data.get("attachment") or {}).get("id")
            or bill_line_id
        )
        # Plocka ut transaction_id — Bezala skapar en draft-tx för varje
        # bill_line-attach. Vi behöver detta för PUT-uppföljningen.
        transaction_id = data.get("transaction_id")
        if not transaction_id and isinstance(data.get("attachment"), dict):
            transaction_id = data["attachment"].get("transaction_id")
        if not transaction_id and isinstance(data.get("transaction"), dict):
            transaction_id = data["transaction"].get("id")

        # Steg 2: om belopp/datum/konto-metadata skickats in OCH vi fick en
        # transaction_id → PUT /transactions/{tx_id} med fullständig payload.
        # Detta sätter belopp/valuta/konto/moms explicit och garanterar att
        # draft:en matchar bill_line:ns siffror även cross-currency.
        #
        # `date` används som tröskel: om caller skickade in datum så är det
        # FAS 5.24:s robusta-coupling-flöde och vi kör PUT. Bara description
        # = legacy ("Koppla till existerande" utan metadata-uppföljning).
        wants_metadata_put = bool(
            date and (
                credit_account_id is not None
                or vat_lines_attributes
                or extra_fields
            )
        )
        if wants_metadata_put and transaction_id:
            # FAS 5.27 — säkerställ att draften är knuten till bill_line
            # på transaction-nivå. C18 GET /transactions/5005475 visade
            # `bill_id: null` och `connected_to_bill_line: false` trots
            # att POST /attachments hade fått `bill_line_id` i form-
            # datan. Det räcker tydligen INTE för Bezala att sätta
            # länken — vi injicerar därför `bill_id` i transaction-
            # payloaden så att kortrad-kopplingen verkligen blir aktiv
            # (= 💳-ikonen blir grön i UI:t).
            #
            # Caller's extra_fields får INTE överskriva detta — vi
            # bygger en ny dict där våra defaultar läggs först och
            # caller-värden kommer på topp (förutom bill_id, som vi
            # hårdsätter sist från bill_line_id-input).
            merged_extra: dict[str, Any] = {}
            if extra_fields:
                merged_extra.update(extra_fields)
            # C19: bill_id är fältet GET /transactions/{id} visar.
            # C21: cross-currency (SEK-kvitto + EUR-bankrad) gav 0/2 i
            # test-sessionen 2026-05-18 — bill_id sätts i PUT-bodyn men
            # `connected_to_bill_line` förblir false och Bezalas UI visar
            # ingen 💳-ikon. Vi sätter därför explicit både bill_id OCH
            # bill_line_id (Rails-konvention för fk-id) samt
            # connected_to_bill_line=True, så att kopplingen aktiveras
            # oavsett vilket fält Bezalas modell faktiskt läser av vid
            # cross-currency.
            merged_extra["bill_id"] = bill_line_id
            merged_extra["bill_line_id"] = bill_line_id
            merged_extra["connected_to_bill_line"] = True
            try:
                self.update_transaction(
                    str(transaction_id),
                    description=description or filename,
                    date=date,
                    credit_account_id=credit_account_id,
                    vat_lines_attributes=vat_lines_attributes,
                    extra_fields=merged_extra,
                )
            except BezalaError as exc:
                # ORPHAN: draft kopplad till bill_line men metadata-PUT
                # misslyckades. Mikko får städa / fylla manuellt — men
                # själva couplingen håller (bill_line_id satt på draft).
                logger.error(
                    "attach_file ORPHAN metadata: bill_line_id=%s "
                    "transaction_id=%s (coupling OK men PUT misslyckades: "
                    "%s | body=%s)",
                    bill_line_id, transaction_id, exc, exc.body,
                )
                raise
        elif wants_metadata_put and not transaction_id:
            logger.warning(
                "attach_file: metadata skickad men Bezala-svaret saknade "
                "transaction_id — kan inte PUT:a metadata. response_keys=%s",
                sorted(data.keys()) if isinstance(data, dict) else "—",
            )

        # Returnera transaction_id som "attachment_id" när vi fick en — det
        # är ID:t som används för deep-link i UI och som lagras på raden.
        # Faller tillbaka på POST-svarets id / bill_line_id för bakåt-
        # kompatibilitet när metadata inte används.
        effective_id = transaction_id or attachment_id
        return BezalaAttachment(attachment_id=str(effective_id))

    def list_vat_rates(self) -> list[dict]:
        """Hämtar momssatser. Bezala-endpointen kan heta /vat_rates eller
        /vat_codes — vi försöker den gängse varianten först och returnerar
        tom lista om inget finns (logger varnar)."""
        try:
            return self._fetch_list("/vat_rates")
        except BezalaError as exc:
            if exc.status_code == 404:
                logger.info(
                    "Bezala /vat_rates saknas (%s) — försöker /vat_codes som fallback",
                    exc.status_code,
                )
                try:
                    return self._fetch_list("/vat_codes")
                except BezalaError:
                    logger.warning("Bezala har varken /vat_rates eller /vat_codes — returnerar tom lista.")
                    return []
            raise

    # --------- receipt upload — TWO-STEP ---------
    #
    # Bezala API-docs (live-verifierad): POST /api/transactions för metadata,
    # sedan POST /api/attachments med filen + transaction_id. /receipts
    # existerar inte. Metadata + fil i samma request (tidigare försök)
    # fungerar inte.

    def upload_receipt(
        self,
        *,
        filename: str,
        pdf_bytes: bytes,
        description: str,
        date: str,
        credit_account_id: int | str | None = None,
        vat_lines_attributes: list[dict] | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> BezalaAttachment:
        """Ladda upp ett kvitto till Bezala via TWO-STEP (omvänd ordning):

          Steg 1: POST /attachments med multipart file + draft=1
                  → Bezala skapar draft-transaktion automatiskt
                  → returnerar (attachment_id, transaction_id)
          Steg 2: PUT /transactions/{tx_id} med metadata-JSON
                  → fyller i description, date, credit_account_id,
                    vat_lines_attributes

        Om steg 2 misslyckas: draft-transaktionen finns i Bezala med
        filen men utan metadata. tx_id loggas som ORPHAN så den kan
        städas eller fyllas i manuellt via Bezala-UI."""
        if not filename:
            raise BezalaError("upload_receipt: filename saknas")
        if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
            raise BezalaError("upload_receipt: pdf_bytes är inte en giltig PDF")
        if not description:
            raise BezalaError("upload_receipt: description saknas")
        if not date:
            raise BezalaError("upload_receipt: date saknas (ÅÅÅÅ-MM-DD)")

        logger.info(
            "upload_receipt: draft-first filename=%r bytes=%d description=%r "
            "date=%r credit_account_id=%s vat_lines_attributes_count=%d",
            filename, len(pdf_bytes), description, date,
            credit_account_id, len(vat_lines_attributes or []),
        )

        # Steg 1: POST /attachments med draft=1 → få tx_id
        attachment_id, transaction_id = self.upload_file_as_draft(
            pdf_bytes, filename,
        )

        # Steg 2: PUT /transactions/{tx_id} med metadata
        try:
            self.update_transaction(
                transaction_id,
                description=description,
                date=date,
                credit_account_id=credit_account_id,
                vat_lines_attributes=vat_lines_attributes,
                extra_fields=extra_fields,
            )
        except BezalaError as exc:
            # ORPHAN: draft-transaktionen finns i Bezala med filen men
            # utan metadata. Användaren måste städa eller fylla i manuellt.
            logger.error(
                "Bezala ORPHAN draft: tx_id=%s attachment_id=%s "
                "(draft skapad men metadata-PUT misslyckades: %s | body=%s)",
                transaction_id, attachment_id, exc, exc.body,
            )
            raise BezalaError(
                f"Draft-transaktion {transaction_id} skapad men metadata-PUT "
                f"misslyckades: {exc}",
                status_code=exc.status_code,
                body=exc.body,
            ) from exc

        logger.info(
            "Bezala: two-step klart — transaction_id=%s attachment_id=%s",
            transaction_id, attachment_id,
        )
        # Returnera transaction_id som "attachment_id" — det är ID:t som
        # lagras i ProcessedMessage.bezala_transaction_id och används för
        # deep-link till Bezala-UI.
        return BezalaAttachment(attachment_id=transaction_id)

    def close(self) -> None:
        self._client.close()
