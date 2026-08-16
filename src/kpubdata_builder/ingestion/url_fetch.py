"""URL source를 위한 SSRF-safe GET fetch (#498 P0).

Public API/File과 달리 URL source는 사용자가 임의의 endpoint를 지정할 수 있다.
서버가 그 endpoint로 직접 요청을 보내므로, 검증 없이 접속하면 서버를 프록시
삼아 내부망(loopback/private/link-local/클라우드 metadata IP 등)을 스캔·접근하는
SSRF 공격이 가능하다. 이 모듈은 다음을 강제한다(#498 체크리스트):

    - GET만 허용, Authorization 등 임의 header를 보내지 않는다(Auth=None, P0).
    - scheme은 https만 허용한다("HTTPS 기본 허용" — file/ftp 등은 이미 스킴
      자체가 거부된다).
    - URL에 userinfo(``user:pass@host``)가 있으면 거부한다.
    - hostname을 DNS로 직접 resolve하고, 모든 resolve 결과가 공인
      (global-routable) 주소일 때만 진행한다 — 하나라도 private/loopback/
      link-local/reserved면 전체를 거부한다(fail-closed).
    - 실제 TCP 연결은 검증한 IP에 직접 연결한다(Host header는 원본 hostname
      유지) — hostname으로 다시 connect하면 확인 시점과 연결 시점 사이에 DNS가
      바뀌는 DNS rebinding으로 검증을 우회당할 수 있다.
    - redirect를 자동으로 따라가지 않고 매 hop마다 같은 검증을 반복한다.
    - connect/read timeout과 응답 크기 상한을 강제한다.

이 모듈은 순수 네트워크 계층이다 — 응답 bytes를 레코드로 파싱하는 책임은
``tabular_ingest``에 있다.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from .errors import IngestionError

# 허용하는 scheme. http/file/ftp 등은 여기 없으므로 구조적으로 거부된다.
_ALLOWED_SCHEMES = frozenset({"https"})

_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 15.0
_DEFAULT_MAX_REDIRECTS = 5
_READ_CHUNK_BYTES = 65536

# 응답 크기 상한 (#498). 환경변수로 override 가능하되 기본값은 보수적으로 둔다.
_MAX_FETCH_BYTES_ENV = "KPUBDATA_BUILDER_URL_FETCH_MAX_BYTES"
_DEFAULT_MAX_FETCH_BYTES = 20 * 1024 * 1024  # 20 MiB

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def default_max_fetch_bytes() -> int:
    """응답 크기 상한을 환경변수에서 읽는다 (없으면 기본값)."""
    raw = os.environ.get(_MAX_FETCH_BYTES_ENV, "").strip()
    if not raw:
        return _DEFAULT_MAX_FETCH_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_FETCH_BYTES
    return value if value > 0 else _DEFAULT_MAX_FETCH_BYTES


@dataclass(frozen=True)
class FetchResult:
    """fetch 성공 결과.

    속성:
        content: 응답 본문 원시 bytes.
        content_type: 응답 ``Content-Type`` 헤더값 (없으면 빈 문자열).
        final_url: redirect를 모두 따라간 뒤 실제로 요청한 URL. provenance에는
            이 값이 아니라 sanitize된 identity만 남긴다 — 호출자 책임.
    """

    content: bytes
    content_type: str
    final_url: str


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """DNS로 검증한 IP에만 직접 연결하는 HTTPSConnection.

    ``http.client.HTTPSConnection`` 은 기본적으로 ``self.host`` 를 다시
    resolve해 연결한다 — 검증(``_resolve_and_validate``) 시점과 실제 connect
    시점 사이에 DNS 응답이 바뀌면(DNS rebinding) private IP로 연결될 수 있다.
    이 클래스는 미리 검증한 ``pinned_ip`` 로만 소켓을 열고, TLS SNI/인증서
    검증에는 원본 ``host`` 를 그대로 쓴다(가상 호스팅·인증서 검증 정합성 유지).
    """

    def __init__(self, host: str, pinned_ip: str, port: int, *, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:  # noqa: D102 - http.client 시그니처 오버라이드
        sock = socket.create_connection((self._pinned_ip, self.port), timeout=self.timeout)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self.host)


def _validate_url_shape(url: str) -> tuple[str, str, int, str]:
    """scheme/userinfo/host 구조를 검증하고 (scheme, host, port, path+query)를 반환한다."""
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise IngestionError(
            f"refusing to fetch url with scheme {parts.scheme or 'none'!r} "
            "(only https is allowed, SSRF policy #498)"
        )
    if parts.username or parts.password:
        raise IngestionError("refusing to fetch url containing userinfo (SSRF policy #498)")
    host = parts.hostname
    if not host:
        raise IngestionError("url must include a host")
    port = parts.port or 443
    path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    return parts.scheme, host, port, path


def _resolve_and_validate(host: str, port: int) -> str:
    """host를 DNS resolve하고, 모든 결과가 공인 IP일 때만 첫 IP를 반환한다.

    하나라도 private/loopback/link-local/reserved/multicast/unspecified면
    전체를 거부한다(fail-closed) — 일부 응답만 사설이어도 어떤 스택이 그 주소를
    고를지 보장할 수 없기 때문이다.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise IngestionError(f"failed to resolve host: {host}") from exc
    if not infos:
        raise IngestionError(f"no addresses resolved for host: {host}")

    resolved_ips: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        # sockaddr는 IPv4(host, port)/IPv6(host, port, flowinfo, scope_id) 두 형태를
        # 아우르는 튜플이라 index 0의 정적 타입이 str|int로 넓어진다 — 실제 값은
        # 항상 주소 문자열이므로 str()로 명시한다.
        ip_text = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise IngestionError(f"resolved a non-IP address for host: {host}") from exc
        if not ip.is_global:
            raise IngestionError(
                f"refusing to fetch from non-public address for host {host!r} (SSRF policy #498)"
            )
        resolved_ips.append(ip_text)
    return resolved_ips[0]


def _read_bounded(response: http.client.HTTPResponse, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise IngestionError(
                f"response exceeds max size ({max_bytes} bytes, SSRF/DoS policy #498)"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def safe_fetch_get(
    url: str,
    *,
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = _DEFAULT_READ_TIMEOUT_SECONDS,
    max_bytes: int | None = None,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
) -> FetchResult:
    """검증된 GET(Auth=None) 요청 하나를 안전하게 수행한다 (#498).

    매개변수:
        url: fetch할 절대 https URL.
        connect_timeout: TCP+TLS 연결 timeout(초).
        read_timeout: 소켓 읽기 timeout(초).
        max_bytes: 응답 본문 크기 상한. 생략 시 환경변수/기본값을 사용.
        max_redirects: 따라갈 최대 redirect 횟수. 매 hop마다 scheme/userinfo/DNS
            검증을 처음부터 다시 수행한다.

    반환값:
        FetchResult: 본문 bytes, Content-Type, 최종 URL.

    예외:
        IngestionError: SSRF 정책 위반, DNS 실패, timeout, 크기 초과, 비정상
            status, 과도한 redirect.
    """
    resolved_max_bytes = max_bytes if max_bytes is not None else default_max_fetch_bytes()
    current_url = url
    for _ in range(max_redirects + 1):
        _scheme, host, port, path = _validate_url_shape(current_url)
        pinned_ip = _resolve_and_validate(host, port)
        connection = _PinnedHTTPSConnection(host, pinned_ip, port, timeout=connect_timeout)
        try:
            connection.timeout = read_timeout
            # 사용자 정의 header는 절대 전달하지 않는다 — arbitrary header 금지(#498).
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": "application/json, application/x-ndjson, text/csv;q=0.9, */*;q=0.1",
                    "User-Agent": "kpubdata-builder-url-source/1.0",
                },
            )
            response = connection.getresponse()
            if response.status in _REDIRECT_STATUSES:
                location = response.getheader("Location")
                _ = response.read()  # 연결 재사용을 위해 body를 비운다.
                if not location:
                    raise IngestionError("redirect response is missing a Location header")
                current_url = urljoin(current_url, location)
                continue
            if response.status != 200:
                raise IngestionError(f"unexpected HTTP status from url source: {response.status}")
            content_type = response.getheader("Content-Type", "") or ""
            content = _read_bounded(response, resolved_max_bytes)
            return FetchResult(content=content, content_type=content_type, final_url=current_url)
        except (TimeoutError, OSError, ssl.SSLError) as exc:
            raise IngestionError(f"failed to fetch url source: {exc}") from exc
        finally:
            connection.close()
    raise IngestionError(f"too many redirects (max {max_redirects}, SSRF policy #498)")


__all__ = ["FetchResult", "default_max_fetch_bytes", "safe_fetch_get"]
