"""SSRF defences for user-supplied links."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from app.providers import web
from app.providers.web import (
    MAX_TEXT_CHARS,
    SafeWebFetcher,
    UnsafeUrlError,
    _build_client,
    _PinnedBackend,
    first_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[::1]/admin",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/router",
        "http://172.16.0.1/internal",
        "http://2130706433/admin",  # decimal-encoded 127.0.0.1
        "http://0177.0.0.1/admin",  # octal-encoded
        "http://0x7f.0.0.1/admin",  # hex-encoded
        "https://example.com:22/ssh",  # non-web port
        "https://example.com:6379/redis",
        "http://user:secret@example.com/",  # embedded credentials
        "ftp://example.com/file",
        "file:///etc/passwd",
        "gopher://example.com/",
    ],
)
def test_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        SafeWebFetcher(max_bytes=1024)._validate_public_url(url)


def test_the_connection_guard_rejects_private_addresses() -> None:
    """The rebinding defence: checked at the moment of connection, not before."""
    with pytest.raises(UnsafeUrlError, match="Private or local"):
        _PinnedBackend().connect_tcp("localhost", 80)


def test_the_connection_guard_rejects_disallowed_ports() -> None:
    with pytest.raises(UnsafeUrlError, match="not allowed"):
        _PinnedBackend().connect_tcp("example.com", 22)


def test_the_client_always_carries_the_guard() -> None:
    """If this ever stops holding, fetching must fail rather than go unprotected."""
    with _build_client(5.0) as client:
        assert isinstance(client._transport._pool._network_backend, _PinnedBackend)


class _StubResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
        self.status_code = status
        self.headers = headers
        self._body = body
        self.encoding = "utf-8"

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308)

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self):
        yield self._body


class _StubClient:
    def __init__(self, responses: list[_StubResponse]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    @contextmanager
    def stream(self, _method: str, url: str):
        self.requested.append(url)
        yield self.responses.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_a_redirect_into_the_private_network_is_blocked(monkeypatch) -> None:
    """Following a redirect must re-run validation, not trust the first hop."""
    client = _StubClient([_StubResponse(302, {"location": "http://127.0.0.1/admin"})])
    monkeypatch.setattr(web, "_build_client", lambda _timeout: client)

    with pytest.raises(UnsafeUrlError, match="Private or local"):
        SafeWebFetcher(max_bytes=1024).fetch("https://example.com/start")

    assert client.requested == ["https://example.com/start"]


def test_a_redirect_loop_terminates(monkeypatch) -> None:
    client = _StubClient([_StubResponse(302, {"location": "https://example.com/next"}) for _ in range(6)])
    monkeypatch.setattr(web, "_build_client", lambda _timeout: client)

    with pytest.raises(UnsafeUrlError, match="Too many redirects"):
        SafeWebFetcher(max_bytes=1024).fetch("https://example.com/start")


def test_oversized_bodies_are_refused(monkeypatch) -> None:
    client = _StubClient([_StubResponse(200, {"content-type": "text/plain"}, b"x" * 5000)])
    monkeypatch.setattr(web, "_build_client", lambda _timeout: client)

    with pytest.raises(UnsafeUrlError, match="too large"):
        SafeWebFetcher(max_bytes=100).fetch("https://example.com/big")


def test_unsupported_content_types_are_refused(monkeypatch) -> None:
    client = _StubClient([_StubResponse(200, {"content-type": "application/zip"}, b"PK")])
    monkeypatch.setattr(web, "_build_client", lambda _timeout: client)

    with pytest.raises(UnsafeUrlError, match="Unsupported link content type"):
        SafeWebFetcher(max_bytes=10_000).fetch("https://example.com/archive")


def test_extracted_text_is_capped(monkeypatch) -> None:
    """Bytes and characters are separate limits: HTML compresses a lot of text."""
    html = b"<html><body>" + b"<p>word</p>" * 20000 + b"</body></html>"
    client = _StubClient([_StubResponse(200, {"content-type": "text/html"}, html)])
    monkeypatch.setattr(web, "_build_client", lambda _timeout: client)

    page = SafeWebFetcher(max_bytes=len(html) + 10).fetch("https://example.com/long")

    assert len(page.content.decode()) <= MAX_TEXT_CHARS


def test_scripts_and_styles_are_stripped(monkeypatch) -> None:
    html = b"<html><body><script>evil()</script><p>Hello</p><style>a{}</style></body></html>"
    client = _StubClient([_StubResponse(200, {"content-type": "text/html"}, html)])
    monkeypatch.setattr(web, "_build_client", lambda _timeout: client)

    page = SafeWebFetcher(max_bytes=10_000).fetch("https://example.com/page")

    assert page.content.decode() == "Hello"


def test_first_url_extraction() -> None:
    assert first_url("Read https://example.com/page please") == "https://example.com/page"
    assert first_url("see https://example.com/page.") == "https://example.com/page"
    assert first_url("no link") is None
