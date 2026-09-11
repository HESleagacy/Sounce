"""Fetches user-supplied links without letting them reach the private network.

Validating a hostname with ``getaddrinfo`` and *then* letting the HTTP client
resolve it again is a time-of-check/time-of-use hole: an attacker controlling
the DNS response can answer public on the first lookup and ``127.0.0.1`` on the
second. This module closes that by validating at the point of connection — the
address the socket actually dials is the address that was checked — and by
re-validating every redirect hop.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpcore
import httpx

log = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
ALLOWED_CONTENT_TYPES = {"text/html", "text/plain"}
ALLOWED_PORTS = frozenset({80, 443})
MAX_REDIRECTS = 4
# Independent of the byte cap: HTML can compress a lot of text into few bytes,
# and the extracted result is what ends up in a model prompt.
MAX_TEXT_CHARS = 50_000
# A DNS label, not an integer or a hex blob. Blocks 0x7f.1, 2130706433 and
# friends before they ever reach the resolver.
HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    content: bytes
    filename: str


class UnsafeUrlError(ValueError):
    """The URL is not allowed to be fetched."""


def _validated_addresses(host: str, port: int) -> list[str]:
    """Resolve a host and return only the globally routable addresses."""
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve {host}") from exc
    addresses: list[str] = []
    for result in results:
        address = ipaddress.ip_address(result[4][0])
        if not address.is_global:
            raise UnsafeUrlError("Private or local links are not allowed")
        addresses.append(str(address))
    if not addresses:
        raise UnsafeUrlError(f"Could not resolve {host}")
    return addresses


class _PinnedBackend(httpcore.SyncBackend):
    """Connects only to addresses that passed validation.

    This is the actual rebinding defence. httpcore hands us the hostname it is
    about to dial; we resolve it here, reject anything non-global, and hand the
    literal address back to the real backend. TLS is unaffected because httpcore
    does ``start_tls`` with the original hostname, so certificate verification
    still checks the name the user asked for.
    """

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[object] | None = None,
    ) -> httpcore.NetworkStream:
        if port not in ALLOWED_PORTS:
            raise UnsafeUrlError(f"Port {port} is not allowed")
        try:
            ipaddress.ip_address(host)
            address = host
            if not ipaddress.ip_address(host).is_global:
                raise UnsafeUrlError("Private or local links are not allowed")
        except ValueError:
            address = _validated_addresses(host, port)[0]
        return super().connect_tcp(
            address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,  # type: ignore[arg-type]
        )


def _build_client(timeout_seconds: float) -> httpx.Client:
    transport = httpx.HTTPTransport(retries=0)
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        # Fail closed. Silently falling back to the default backend would drop
        # the rebinding protection while still looking like it was applied.
        raise RuntimeError(
            "httpx transport has no reachable connection pool; the SSRF connection guard cannot be installed"
        )
    pool._network_backend = _PinnedBackend()
    return httpx.Client(transport=transport, timeout=timeout_seconds, follow_redirects=False)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored and data.strip():
            self.parts.append(data.strip())


class SafeWebFetcher:
    def __init__(self, max_bytes: int, timeout_seconds: float = 15.0) -> None:
        self._max_bytes = max_bytes
        self._timeout_seconds = timeout_seconds

    def fetch(self, url: str) -> FetchedPage:
        current = url
        with _build_client(self._timeout_seconds) as client:
            for _ in range(MAX_REDIRECTS):
                self._validate_public_url(current)
                with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise UnsafeUrlError("Redirect did not include a location")
                        # Re-validated at the top of the next iteration, and the
                        # pinned backend checks the address again on connect.
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type not in ALLOWED_CONTENT_TYPES:
                        raise UnsafeUrlError(f"Unsupported link content type: {content_type or 'unknown'}")
                    declared = int(response.headers.get("content-length", "0") or 0)
                    if declared > self._max_bytes:
                        raise UnsafeUrlError("Linked page is too large")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > self._max_bytes:
                            raise UnsafeUrlError("Linked page is too large")
                    encoding = response.encoding or "utf-8"
                content = bytes(body)
                if content_type == "text/html":
                    parser = _TextExtractor()
                    parser.feed(content.decode(encoding, errors="replace"))
                    content = "\n".join(parser.parts)[:MAX_TEXT_CHARS].encode("utf-8")
                else:
                    content = content.decode(encoding, errors="replace")[:MAX_TEXT_CHARS].encode("utf-8")
                hostname = urlparse(current).hostname or "link"
                return FetchedPage(url=current, content=content, filename=f"{hostname}.txt")
        raise UnsafeUrlError("Too many redirects")

    @staticmethod
    def _validate_public_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise UnsafeUrlError("Only public HTTP(S) links are supported")
        if parsed.username or parsed.password:
            raise UnsafeUrlError("Links with embedded credentials are not supported")
        hostname = parsed.hostname
        if not hostname:
            raise UnsafeUrlError("Only public HTTP(S) links are supported")
        try:
            port = parsed.port
        except ValueError as exc:
            raise UnsafeUrlError("Invalid port in link") from exc
        port = port or (443 if parsed.scheme == "https" else 80)
        if port not in ALLOWED_PORTS:
            raise UnsafeUrlError(f"Port {port} is not allowed")

        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            # Not a literal address, so it must look like a real DNS name. This
            # rejects decimal/octal/hex encodings of IPs that some resolvers
            # would happily turn back into 127.0.0.1.
            if not HOSTNAME_PATTERN.match(hostname):
                raise UnsafeUrlError("Link host is not a valid public hostname") from None
            _validated_addresses(hostname, port)
            return
        if not address.is_global:
            raise UnsafeUrlError("Private or local links are not allowed")


def first_url(text: str) -> str | None:
    match = URL_PATTERN.search(text)
    return match.group(0).rstrip(".,);]") if match else None
