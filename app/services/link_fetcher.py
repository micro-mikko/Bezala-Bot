"""Hämta PDF från en externt hostad länk.

- Följer redirect-kedjor (max 5)
- Timeout 15 sek
- Blockerar localhost och privata IP-adresser (SSRF-skydd)
- Validerar att response är PDF (content-type + magic bytes)
- Raiser LinkFetchError vid fel — kallare hanterar.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 15.0
DEFAULT_MAX_REDIRECTS = 5
PDF_MAGIC = b"%PDF"

# Max storlek vi är beredda att ladda ner (skydd mot zip-bomb / stor fil).
MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB


class LinkFetchError(RuntimeError):
    """Raises vid SSRF-blockering, timeout, non-PDF, HTML, etc.
    Bär user-visible `message`."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _is_blocked_host(host: str) -> bool:
    """True om hosten pekar på en privat/lokal IP-adress.
    Löser DNS först för att skydda mot namngivna proxies.
    Block-list: loopback, link-local, privata RFC1918-ranges, unikast cloud-
    metadata-adresser (169.254.0.0/16)."""
    if not host:
        return True
    host_lower = host.lower().strip("[]")

    # Literal IP
    try:
        ip = ipaddress.ip_address(host_lower)
        return _ip_blocked(ip)
    except ValueError:
        pass

    # DNS-lookup
    try:
        infos = socket.getaddrinfo(host_lower, None)
    except socket.gaierror:
        # Kan inte lösa → vi blockerar för säkerhets skull
        return True
    for family, _type, _proto, _canon, sockaddr in infos:
        try:
            addr_str = sockaddr[0]
            ip = ipaddress.ip_address(addr_str)
            if _ip_blocked(ip):
                return True
        except (ValueError, IndexError):
            continue
    return False


def _ip_blocked(ip) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def fetch_pdf_from_link(
    url: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    client: httpx.Client | None = None,
) -> tuple[bytes, str]:
    """Hämta + validera PDF från `url`. Returnerar (pdf_bytes, final_url)
    eller raiser LinkFetchError. `final_url` är den URL httpx landade på
    efter följda redirects — kallaren kan validera att destinationen är
    den förväntade (t.ex. för SendGrid-wrappade kvitto-länkar där entry-
    URLen är en click-tracker och final-URLen är den faktiska vendor-
    domänen). `client` kan injiceras i tester."""
    if not url or not isinstance(url, str):
        raise LinkFetchError("Tom URL")

    try:
        parsed = urlparse(url)
    except Exception as exc:  # noqa: BLE001
        raise LinkFetchError(f"Ogiltig URL: {exc}") from exc

    if parsed.scheme not in ("http", "https"):
        raise LinkFetchError(f"Endast http/https tillåtet (fick {parsed.scheme})")
    if not parsed.hostname:
        raise LinkFetchError("URL saknar host")

    if _is_blocked_host(parsed.hostname):
        raise LinkFetchError(f"Host {parsed.hostname} är blockerad (SSRF-skydd)")

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=timeout_s,
            follow_redirects=True,
            max_redirects=max_redirects,
        )
    try:
        try:
            resp = client.get(url)
        except httpx.TimeoutException as exc:
            raise LinkFetchError(f"Timeout efter {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise LinkFetchError(f"Nätverksfel: {exc}") from exc

        if resp.status_code >= 400:
            raise LinkFetchError(
                f"HTTP {resp.status_code} från {parsed.hostname}",
                status_code=resp.status_code,
            )

        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        data = resp.content

        if len(data) > MAX_PDF_BYTES:
            raise LinkFetchError(
                f"Filen är för stor ({len(data)} byte, max {MAX_PDF_BYTES})"
            )

        if content_type != "application/pdf":
            # Tillåt om magic bytes ändå är PDF (vissa servrar skickar
            # fel content-type).
            if not data.startswith(PDF_MAGIC):
                snippet = data[:120].decode("utf-8", errors="replace")
                raise LinkFetchError(
                    f"Länken gav {content_type or 'okänd typ'} istället för PDF. "
                    f"Öppna länken manuellt och ladda ner själv. "
                    f"(början: {snippet!r})"
                )

        if not data.startswith(PDF_MAGIC):
            raise LinkFetchError("Svaret är inte en giltig PDF (magic bytes saknas)")

        return data, str(resp.url)
    finally:
        if owns_client:
            client.close()
