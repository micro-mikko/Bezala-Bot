"""Google Drive-klient via OAuth2.

Drive autentiseras med ett SEPARAT konto från Gmail — Visma Workspace blockerar
extern delning av Drive-mappar till tredje-parts OAuth-klienter, så Drive-
uppladdningarna går mot ett privat Gmail-konto där OAuth-klienten är skapad
och där Drive-mappen ligger.

OAuth-klienten är dock samma (GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET) —
bara användaren som godkänner är en annan. Drive-kontots refresh-token
lagras som DRIVE_REFRESH_TOKEN.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from app.config import get_settings
from app.services.oauth_token_store import (
    OAuthAuthError,
    get_refresh_token,
    is_invalid_grant,
    set_auth_required,
)

logger = logging.getLogger(__name__)

# Drive-kontot är ett annat än användarens inloggade Google-konto (se
# dokstring). Default: vi lägger till "anyone with link"-läsrätt på varje
# uppladdad fil så webbläsar-iframen (/preview) kan rendera den utan att
# användaren får "You need access". Kan stängas av via env om policy kräver.
PUBLIC_READ_ON_UPLOAD = os.environ.get(
    "BEZALA_DRIVE_PUBLIC_READ", "true"
).lower() in ("1", "true", "yes", "on")

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
]


@dataclass
class DriveUploadResult:
    file_id: str
    web_view_link: str
    name: str


class DriveClient:
    def __init__(self) -> None:
        settings = get_settings()
        refresh_token = get_refresh_token("drive")
        if not (
            settings.gmail_client_id
            and settings.gmail_client_secret
            and refresh_token
        ):
            set_auth_required("drive", True)
            raise OAuthAuthError(
                "drive",
                "Drive OAuth saknar konfiguration. Klicka Återanslut Drive "
                "i Inställningar.",
            )

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=settings.gmail_client_id,
            client_secret=settings.gmail_client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            if is_invalid_grant(exc):
                set_auth_required("drive", True)
                logger.warning("Drive invalid_grant — kräver återanslutning: %s", exc)
                raise OAuthAuthError("drive", str(exc)) from exc
            raise
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._folder_id = settings.google_drive_folder_id

    def upload_pdf(self, filename: str, data: bytes) -> DriveUploadResult:
        return self.upload_attachment(filename, data, "application/pdf")

    def upload_attachment(
        self, filename: str, data: bytes, mimetype: str
    ) -> DriveUploadResult:
        """Generell variant av upload_pdf — accepterar valfri mimetype.
        Används av FAS 7a-manuell-upload som tar emot PDF/JPG/PNG."""
        media = MediaIoBaseUpload(
            io.BytesIO(data), mimetype=mimetype, resumable=False
        )
        metadata = {"name": filename, "parents": [self._folder_id]}
        created = (
            self._service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        file_id = created["id"]
        logger.info(
            "Laddade upp %s (%s) till Drive (id=%s)", filename, mimetype, file_id,
        )

        # Lägg till "anyone with link"-läsrätt best-effort. Misslyckas det
        # (t.ex. Workspace-policy blockar) — fortsätt ändå, filen finns kvar
        # och web_view_link funkar för kontots ägare.
        if PUBLIC_READ_ON_UPLOAD:
            self._grant_anyone_reader_safe(file_id, filename)

        return DriveUploadResult(
            file_id=file_id,
            web_view_link=created.get("webViewLink", ""),
            name=created.get("name", filename),
        )

    def _grant_anyone_reader_safe(self, file_id: str, filename: str) -> None:
        try:
            self._service.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
                fields="id",
                supportsAllDrives=True,
                sendNotificationEmail=False,
            ).execute()
            logger.info("Drive: gav anyone-reader på %s (id=%s)", filename, file_id)
        except HttpError as exc:
            logger.warning(
                "Drive: kunde inte sätta anyone-reader på %s (id=%s): %s",
                filename, file_id, exc,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Drive: permission-grant misslyckades oväntat för %s (id=%s)",
                filename, file_id,
            )

    def download_pdf(self, file_id: str) -> bytes:
        """Hämta PDF-bytes från Drive.

        Hårdnar resultatet: tom respons (b'') eller None räknas som fel,
        inte som tom fil. Googles client returnerar normalt bytes — men om
        get_media misslyckas tyst vill vi upptäcka det här istället för att
        skicka noll bytes till Bezala och få oklara nedströmsfel."""
        if not file_id:
            raise ValueError("download_pdf: file_id saknas")
        data = self._service.files().get_media(fileId=file_id).execute()
        if not data:
            raise RuntimeError(
                f"download_pdf: tom respons från Drive för file_id={file_id!r}"
            )
        if not isinstance(data, (bytes, bytearray)):
            raise RuntimeError(
                f"download_pdf: oväntad typ {type(data).__name__} från Drive "
                f"(file_id={file_id!r})"
            )
        return bytes(data)

    def delete_file(self, file_id: str) -> None:
        """Radera en fil permanent från Drive. Används bara vid hard-delete
        när user explicit satt purge_drive=true."""
        self._service.files().delete(
            fileId=file_id, supportsAllDrives=True
        ).execute()

    def filename_exists(self, filename: str) -> bool:
        safe = filename.replace("'", "\\'")
        query = (
            f"name = '{safe}' and '{self._folder_id}' in parents and trashed = false"
        )
        resp = (
            self._service.files()
            .list(q=query, fields="files(id, name)", pageSize=1, supportsAllDrives=True)
            .execute()
        )
        return bool(resp.get("files"))
