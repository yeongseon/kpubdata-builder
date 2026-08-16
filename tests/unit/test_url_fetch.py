"""ingestion.url_fetch: url source의 SSRF-safe fetch 검증 (#498).

``safe_fetch_get``은 hostname을 실제 DNS로 resolve해 공인(global-routable) IP인지
확인하고, 검증한 IP에 직접 TLS 연결한다. 이 테스트는 두 계층을 나눠 검증한다.

    - 순수 검증 로직(``_validate_url_shape``/``_resolve_and_validate``)은 IP
      리터럴을 hostname으로 써서 실제 네트워크 호출 없이 빠르게 검증한다(IP
      리터럴은 DNS 조회를 하지 않는다).
    - 실제 fetch 흐름(redirect 추적, 크기 상한, 상태 코드 처리)은 로컬
      ``http.server``를 띄우고 ``_resolve_and_validate``/``_PinnedHTTPSConnection``
      두 seam만 이 로컬 서버를 가리키도록 monkeypatch해 검증한다 — 실제 SSRF
      방어 로직(redirect-loop, 크기 상한)은 그대로 실행된다.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kpubdata_builder.ingestion import IngestionError
from kpubdata_builder.ingestion.url_fetch import (
    _PinnedHTTPSConnection,
    _read_bounded,
    _resolve_and_validate,
    _validate_url_shape,
    safe_fetch_get,
)

# --- 순수 검증 로직 (네트워크 불필요) --------------------------------------------


@pytest.mark.parametrize("scheme_url", ["http://example.org/data", "ftp://example.org/data"])
def test_validate_url_shape_rejects_non_https_scheme(scheme_url: str) -> None:
    with pytest.raises(IngestionError, match="https"):
        _validate_url_shape(scheme_url)


def test_validate_url_shape_rejects_file_scheme() -> None:
    with pytest.raises(IngestionError, match="https"):
        _validate_url_shape("file:///etc/passwd")


def test_validate_url_shape_rejects_userinfo() -> None:
    with pytest.raises(IngestionError, match="userinfo"):
        _validate_url_shape("https://user:pass@example.org/data")


def test_validate_url_shape_rejects_missing_host() -> None:
    with pytest.raises(IngestionError, match="host"):
        _validate_url_shape("https:///data")


def test_validate_url_shape_accepts_valid_https_url() -> None:
    scheme, host, port, path = _validate_url_shape("https://example.org:8443/data?x=1")

    assert scheme == "https"
    assert host == "example.org"
    assert port == 8443
    assert path == "/data?x=1"


def test_validate_url_shape_defaults_to_port_443() -> None:
    _scheme, _host, port, path = _validate_url_shape("https://example.org/data")

    assert port == 443
    assert path == "/data"


@pytest.mark.parametrize(
    "loopback_or_private_ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.1",  # private
        "172.16.0.1",  # private
        "192.168.1.1",  # private
        "169.254.169.254",  # link-local (cloud metadata endpoint)
        "0.0.0.0",  # unspecified
        "::1",  # IPv6 loopback
    ],
)
def test_resolve_and_validate_rejects_non_public_ip_literal(loopback_or_private_ip: str) -> None:
    # IP 리터럴은 실제 DNS 조회를 하지 않으므로(getaddrinfo가 그대로 파싱만 함)
    # 네트워크 접근 없이 검증할 수 있다.
    with pytest.raises(IngestionError, match="SSRF policy"):
        _resolve_and_validate(loopback_or_private_ip, 443)


def test_resolve_and_validate_accepts_public_ip_literal() -> None:
    resolved = _resolve_and_validate("8.8.8.8", 443)

    assert resolved == "8.8.8.8"


def test_resolve_and_validate_raises_for_unresolvable_host() -> None:
    with pytest.raises(IngestionError, match="failed to resolve host"):
        _resolve_and_validate("this-host-does-not-resolve.invalid", 443)


# --- 응답 크기 상한 --------------------------------------------------------------


class _FakeHTTPResponse:
    """``http.client.HTTPResponse``의 최소 read() 계약만 흉내내는 테스트 더블."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def test_read_bounded_returns_full_content_under_limit() -> None:
    response = _FakeHTTPResponse([b"hello", b" ", b"world", b""])

    result = _read_bounded(response, max_bytes=100)  # type: ignore[arg-type]

    assert result == b"hello world"


def test_read_bounded_rejects_content_over_limit() -> None:
    response = _FakeHTTPResponse([b"x" * 10, b"y" * 10, b""])

    with pytest.raises(IngestionError, match="exceeds max size"):
        _read_bounded(response, max_bytes=15)  # type: ignore[arg-type]


# --- 전체 fetch 흐름 (로컬 HTTP 서버 + 검증 seam만 우회) -------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib 시그니처
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib 규약
        if self.path == "/ok":
            body = b'[{"id": 1}]'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect-once":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/redirect-with-body":
            # redirect(3xx)에도 body가 실릴 수 있다 — 이 body를 amt 없이
            # read()하면 max_bytes cap을 우회하는 unbounded read가 된다는
            # BLOCKER(#538 review)를 재현하기 위한 non-empty body.
            body = b"ignored redirect body " * 10
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect-to-private":
            # redirect target이 private 주소면 hop마다 처음부터 SSRF 검증을
            # 다시 해야 한다(회귀 방지, #538 review).
            self.send_response(302)
            self.send_header("Location", "https://10.0.0.1/private")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/redirect-loop":
            self.send_response(302)
            self.send_header("Location", "/redirect-loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/big":
            body = b"x" * 1000
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/not-found":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


@pytest.fixture
def _raw_local_server() -> Iterator[HTTPServer]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


class _LoopbackHTTPConnection:
    """``_PinnedHTTPSConnection`` 을 대체해 평문 HTTP로 로컬 서버에 연결하는 test double.

    실제 프로덕션 경로는 TLS를 강제하지만, 이 fixture 서버는 자체서명 인증서
    설정 없이 순수 HTTP만 제공한다 — url_fetch가 내부적으로 참조하는 connection
    클래스만 이 test double로 바꿔서 redirect/크기 상한/상태 코드 처리 같은
    나머지 로직은 실제 코드 그대로 실행되게 한다.
    """

    def __init__(
        self,
        host: str,
        pinned_ip: str,
        port: int,
        *,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        self._delegate = http.client.HTTPConnection(pinned_ip, port, timeout=connect_timeout)
        self.host = host
        self._read_timeout = read_timeout

    def request(self, method: str, path: str, headers: dict[str, str]) -> None:
        self._delegate.request(method, path, headers=headers)

    def getresponse(self) -> object:
        return self._delegate.getresponse()

    def close(self) -> None:
        self._delegate.close()


@pytest.fixture
def local_server(
    monkeypatch: pytest.MonkeyPatch, _raw_local_server: HTTPServer
) -> Iterator[HTTPServer]:
    """로컬 HTTP 서버를 띄우고, 검증 seam 두 곳만 그 서버를 가리키도록 우회한다.

    실제 SSRF 검증(scheme/userinfo/redirect-loop 방어/크기 상한)은 그대로
    실행되고, "hostname → 실제 소켓 연결" 두 지점(``_resolve_and_validate``,
    ``_PinnedHTTPSConnection``)만 로컬 loopback 서버를 가리키도록 바꾼다. 이
    fixture를 요청하는 테스트에서만 우회가 적용된다 — 순수 검증 로직 테스트는
    영향받지 않는다.
    """
    import kpubdata_builder.ingestion.url_fetch as url_fetch_module

    port = _raw_local_server.server_address[1]
    monkeypatch.setattr(url_fetch_module, "_resolve_and_validate", lambda host, _port: "127.0.0.1")
    monkeypatch.setattr(
        url_fetch_module,
        "_PinnedHTTPSConnection",
        lambda host, pinned_ip, _port, *, connect_timeout, read_timeout: _LoopbackHTTPConnection(
            host, pinned_ip, port, connect_timeout=connect_timeout, read_timeout=read_timeout
        ),
    )
    yield _raw_local_server


def test_safe_fetch_get_returns_content_and_content_type(local_server: HTTPServer) -> None:
    del local_server
    result = safe_fetch_get("https://example.org/ok")

    assert result.content == b'[{"id": 1}]'
    assert result.content_type == "application/json"


def test_safe_fetch_get_follows_redirect(local_server: HTTPServer) -> None:
    del local_server
    result = safe_fetch_get("https://example.org/redirect-once")

    assert result.content == b'[{"id": 1}]'


def test_safe_fetch_get_rejects_redirect_loop(local_server: HTTPServer) -> None:
    del local_server
    with pytest.raises(IngestionError, match="too many redirects"):
        safe_fetch_get("https://example.org/redirect-loop", max_redirects=3)


def test_safe_fetch_get_enforces_max_bytes(local_server: HTTPServer) -> None:
    del local_server
    with pytest.raises(IngestionError, match="exceeds max size"):
        safe_fetch_get("https://example.org/big", max_bytes=100)


def test_safe_fetch_get_rejects_non_200_status(local_server: HTTPServer) -> None:
    del local_server
    with pytest.raises(IngestionError, match="unexpected HTTP status"):
        safe_fetch_get("https://example.org/not-found")


def test_safe_fetch_get_rejects_non_https_before_any_connection() -> None:
    with pytest.raises(IngestionError, match="https"):
        safe_fetch_get("http://example.org/ok")


# --- redirect body read는 절대 unbounded여선 안 된다 (BLOCKER, #538 review) ------


def test_safe_fetch_get_does_not_perform_unbounded_read_on_redirect_body(
    local_server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """redirect(3xx) 응답 body를 amt 없이 read()해 전체를 무제한으로 읽지 않는다.

    무제한 read는 최종 200 응답에만 적용되는 ``_read_bounded()``의 max_bytes
    cap을 redirect hop에서 우회하는 경로가 된다.
    """
    del local_server
    calls: list[int | None] = []
    original_read = http.client.HTTPResponse.read

    def tracking_read(self: http.client.HTTPResponse, amt: int | None = None) -> bytes:
        calls.append(amt)
        return original_read(self, amt)

    monkeypatch.setattr(http.client.HTTPResponse, "read", tracking_read)

    result = safe_fetch_get("https://example.org/redirect-with-body")

    assert result.content == b'[{"id": 1}]'  # 기존 정상 redirect 동작 유지.
    # redirect hop을 포함해 어떤 read() 호출도 amt=None(무제한)이 아니어야
    # 한다 — 마지막 200 응답만 _read_bounded()가 명시적 chunk size로 읽는다.
    assert all(amt is not None for amt in calls)


def test_safe_fetch_get_rejects_redirect_to_private_address(
    _raw_local_server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """redirect target이 private 주소면 hop마다 검증을 처음부터 다시 해 거부한다.

    redirect body 처리 방식을 바꾸는 것과 무관하게 SSRF 방어(hop별 재검증)에
    회귀가 없어야 한다 — ``local_server`` fixture는 모든 host를 강제로
    127.0.0.1로 바꾸므로 여기서는 첫 hop(example.org)만 로컬 서버로 우회하고
    redirect target(10.0.0.1)은 실제 ``_resolve_and_validate``를 그대로
    거치게 한다.
    """
    import kpubdata_builder.ingestion.url_fetch as url_fetch_module

    port = _raw_local_server.server_address[1]
    real_resolve_and_validate = url_fetch_module._resolve_and_validate

    def fake_resolve(host: str, resolve_port: int) -> str:
        if host == "example.org":
            return "127.0.0.1"
        return real_resolve_and_validate(host, resolve_port)

    monkeypatch.setattr(url_fetch_module, "_resolve_and_validate", fake_resolve)
    monkeypatch.setattr(
        url_fetch_module,
        "_PinnedHTTPSConnection",
        lambda host, pinned_ip, _port, *, connect_timeout, read_timeout: _LoopbackHTTPConnection(
            host, pinned_ip, port, connect_timeout=connect_timeout, read_timeout=read_timeout
        ),
    )

    with pytest.raises(IngestionError, match="SSRF policy"):
        safe_fetch_get("https://example.org/redirect-to-private")


# --- connect/read timeout 분리 (SHOULD FIX, #538 review) -------------------------


class _FakeTLSSocket:
    """``ssl.SSLSocket``을 대체해 ``settimeout`` 호출만 기록하는 test double."""

    def __init__(self) -> None:
        self.settimeout_calls: list[float] = []

    def settimeout(self, value: float) -> None:
        self.settimeout_calls.append(value)


def test_pinned_https_connection_uses_connect_timeout_for_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TCP connect에는 connect_timeout이 쓰이고, DNS를 다시 resolve하지 않는다."""
    captured_addresses: list[tuple[str, int]] = []
    captured_timeouts: list[float] = []
    fake_raw_socket = object()
    fake_tls_socket = _FakeTLSSocket()

    def fake_create_connection(address: tuple[str, int], timeout: float) -> object:
        captured_addresses.append(address)
        captured_timeouts.append(timeout)
        return fake_raw_socket

    def fake_wrap_socket(
        self: ssl.SSLContext, sock: object, *, server_hostname: str
    ) -> _FakeTLSSocket:
        assert sock is fake_raw_socket
        assert server_hostname == "example.org"
        return fake_tls_socket

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", fake_wrap_socket)

    connection = _PinnedHTTPSConnection(
        "example.org", "8.8.8.8", 443, connect_timeout=1.5, read_timeout=9.0
    )
    connection.connect()

    # 검증된 IP에만 직접 연결한다 — hostname을 다시 resolve하지 않는다(DNS
    # rebinding 방어 유지).
    assert captured_addresses == [("8.8.8.8", 443)]
    assert captured_timeouts == [1.5]


def test_pinned_https_connection_switches_to_read_timeout_after_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect가 끝난 뒤 socket read timeout은 read_timeout으로 전환된다."""
    fake_tls_socket = _FakeTLSSocket()

    monkeypatch.setattr(socket, "create_connection", lambda address, timeout: object())
    monkeypatch.setattr(
        ssl.SSLContext,
        "wrap_socket",
        lambda self, sock, *, server_hostname: fake_tls_socket,
    )

    connection = _PinnedHTTPSConnection(
        "example.org", "8.8.8.8", 443, connect_timeout=1.5, read_timeout=9.0
    )
    connection.connect()

    assert fake_tls_socket.settimeout_calls == [9.0]


class _TimeoutHTTPConnection:
    """connect 단계에서 항상 timeout을 일으키는 ``_PinnedHTTPSConnection`` test double."""

    def __init__(
        self,
        host: str,
        pinned_ip: str,
        port: int,
        *,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        del host, pinned_ip, port, connect_timeout, read_timeout

    def request(self, method: str, path: str, headers: dict[str, str]) -> None:
        del method, path, headers
        raise TimeoutError("simulated connect timeout")

    def getresponse(self) -> object:
        raise AssertionError("must not be reached after a connect timeout")

    def close(self) -> None:
        return


def test_safe_fetch_get_maps_timeout_to_ingestion_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect/read timeout은 원문 예외가 아니라 IngestionError로 안전하게 mapping된다."""
    import kpubdata_builder.ingestion.url_fetch as url_fetch_module

    monkeypatch.setattr(url_fetch_module, "_resolve_and_validate", lambda host, _port: "8.8.8.8")
    monkeypatch.setattr(url_fetch_module, "_PinnedHTTPSConnection", _TimeoutHTTPConnection)

    with pytest.raises(IngestionError, match="failed to fetch"):
        safe_fetch_get("https://example.org/ok")
