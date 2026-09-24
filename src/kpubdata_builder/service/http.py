"""stdlib http.server 기반 HTTP 어댑터 (#36).

새 의존성 없이 BuilderService를 HTTP로 노출한다. 요청 파싱/응답 직렬화만
담당하고 실제 로직은 app.dispatch에 위임한다.

주요 구성:
    - make_handler: BuilderService에 바인딩된 요청 핸들러 클래스 생성
    - serve: 블로킹 HTTP 서버 실행
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket as _socket
from threading import Lock
from typing import Any, cast
from urllib.parse import urlsplit

from ..spec import JsonValue
from ..store.backend import validate_storage_config
from ..uploads import resolve_max_upload_bytes
from .app import BuilderService, FileResponse, dispatch
from .auth import validate_dev_mode, validate_oidc_config

# 단일 요청이 메모리를 고갈시키거나 단일 스레드 서버를 멈추게 하지 않도록 body 크기를
# 제한한다. spec YAML 요청에 충분하면서도 남용을 막는 보수적 상한 (#186).
_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB

# POST /uploads(#498)는 JSON이 아니라 파일 binary를 담으므로 일반 JSON 상한보다
# 크게 잡는다 — 실제 상한은 uploads.resolve_max_upload_bytes()(환경변수로 조정)를
# 그대로 재사용해 두 곳에 서로 다른 숫자를 두지 않는다.
_UPLOADS_PATH = "/uploads"

# 소켓 읽기 타임아웃(초). 느린 클라이언트/slowloris 공격이 스레드를 무한 점거하지 않도록
# 한다 (#219). ThreadingHTTPServer를 사용하더라도 각 연결 스레드가 여기서 해제된다.
_SOCKET_TIMEOUT_SECONDS = 30.0

# 동시에 요청을 처리할 수 있는 최대 스레드 수. ThreadingHTTPServer는 연결마다 새
# 스레드(스레드당 스택 ~8MB)를 무제한으로 만들기 때문에, 수백~수천 개의 동시 연결만으로
# 메모리가 고갈될 수 있다 (#253). 고정 크기 스레드 풀로 상한을 둔다.
_DEFAULT_MAX_WORKERS = 10

# worker 가 모두 찼을 때 소켓을 열어 둔 채 순서를 기다릴 수 있는 연결 수.
# ThreadPoolExecutor 의 작업 큐에는 상한이 없어서, 이 값이 없으면 접속하는 족족
# 큐에 쌓인다 — 스레드 수는 제한돼 있어도 소켓(파일 디스크립터)과 큐 항목은
# 무한히 늘어난다. 넘치는 연결은 기다리게 두지 않고 503 으로 즉시 돌려보낸다.
_DEFAULT_MAX_PENDING_REQUESTS = 100


def _overloaded_response() -> bytes:
    """대기 한도를 넘긴 연결에 그대로 써 보내는 최소 HTTP 응답.

    핸들러를 거치지 않고(그러려고 거절하는 것이다) 소켓에 직접 쓴다.
    ``Connection: close`` 로 클라이언트가 이 연결을 재사용하지 않게 한다.
    """
    body = b'{"error": "server overloaded"}'
    return (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: application/json; charset=utf-8\r\n"
        b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
        b"Retry-After: 1\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )


_OVERLOADED_RESPONSE = _overloaded_response()

# CORS 허용 Origin 목록 (#322). default-deny 정책: 환경변수 미설정 시 모든 크로스오리진
# 요청을 거부한다. 콤마로 구분된 오리진 목록을 받는다 (예:
# KPUBDATA_BUILDER_ALLOWED_ORIGINS=http://localhost:5173,https://studio.example.com).
_ALLOWED_ORIGINS_ENV = "KPUBDATA_BUILDER_ALLOWED_ORIGINS"

# 프리플라이트가 허가하는 요청 헤더. Bearer 인증(ADR 0009)을 위해 Authorization 포함 (#382).
_CORS_ALLOWED_HEADERS = "Content-Type, X-API-Key, Authorization"

# MIME 타입 기본값 (#323). mimetypes.guess_type이 None을 반환할 때 사용.
_DEFAULT_MIME_TYPE = "application/octet-stream"

# 일반적인 데이터셋 파일 확장자에 대한 명시적 MIME 타입 매핑.
# mimetypes 라이브러리가 부정확하거나 누락된 경우에 대비해 보수적인 기본값을 둔다.
_EXPLICIT_MIME_TYPES: dict[str, str] = {
    ".parquet": "application/vnd.apache.parquet",
    ".csv": "text/csv",
    ".json": "application/json",
    ".geojson": "application/geo+json",
    ".geojsonl": "application/geo+jsonl",
    ".ndjson": "application/x-ndjson",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".xml": "application/xml",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
}

# 파일 응답을 소켓으로 흘려보낼 때의 한 번 읽기 크기.
_FILE_CHUNK_BYTES = 64 * 1024

# charset 을 붙여야 하는 형식. text/* 와 여기 끝나는 것들만 텍스트로 본다.
_TEXTUAL_MIME_SUFFIXES = ("json", "xml", "yaml", "javascript")

_logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_allowed_origins() -> frozenset[str]:
    """환경변수에서 허용된 오리진 목록을 파싱한다 (#322).

    Returns:
        허용된 오리진의 frozenset. 환경변수 미설정 시 빈 frozenset (default-deny).
    """
    env_value = os.environ.get(_ALLOWED_ORIGINS_ENV, "")
    if not env_value:
        return frozenset()
    # 콤마로 구분된 오리진 목록을 파싱하고 공백을 제거한다.
    origins = [origin.strip() for origin in env_value.split(",") if origin.strip()]
    return frozenset(origins)


def _clear_cors_cache() -> None:
    """테스트용: CORS 허용 오리진 캐시를 비운다 (#322)."""
    _get_allowed_origins.cache_clear()


def _is_origin_allowed(request_origin: str | None, allowed: frozenset[str]) -> bool:
    """요청 오리진이 허용 목록에 있는지 확인한다 (#322).

    Same-origin 요청(오리진이 None인 경우)은 항상 허용한다.

    Args:
        request_origin: 요청의 Origin 헤더값.
        allowed: 허용된 오리진 목록.

    Returns:
        오리진이 허용되면 True, 아니면 False.
    """
    if request_origin is None:
        # Same-origin 요청 (브라우저가 Origin 헤더를 보내지 않는 경우)
        return True
    return request_origin in allowed


def _get_mime_type(file_path: Path) -> str:
    """파일 경로에서 MIME 타입을 추론한다 (#323).

    명시적 매핑을 우선 확인하고, 없으면 mimetypes 라이브러리를 사용한다.
    그래도 None이 반환되면 기본값(application/octet-stream)을 사용한다.

    매개변수:
        file_path: MIME 타입을 추론할 파일 경로.

    반환값:
        MIME 타입 문자열.
    """
    suffix = file_path.suffix.lower()
    if suffix in _EXPLICIT_MIME_TYPES:
        return _EXPLICIT_MIME_TYPES[suffix]
    guessed = mimetypes.guess_type(file_path.name)[0]
    return guessed if guessed else _DEFAULT_MIME_TYPE


def _content_type_header(mime_type: str) -> str:
    """Content-Type 헤더 값. 텍스트 형식에만 charset 을 붙인다.

    예전에는 파일 응답에 무조건 ``; charset=utf-8`` 을 붙였다 — parquet 에도
    붙어서 ``application/vnd.apache.parquet; charset=utf-8`` 같은 값이 나갔다.
    바이너리에 문자 인코딩을 선언하는 것은 그냥 틀린 말이다.
    """
    if mime_type.startswith("text/") or mime_type.endswith(_TEXTUAL_MIME_SUFFIXES):
        return f"{mime_type}; charset=utf-8"
    return mime_type


def make_handler(service: BuilderService) -> type[BaseHTTPRequestHandler]:
    """주어진 BuilderService에 바인딩된 요청 핸들러 클래스를 생성한다."""

    class _Handler(BaseHTTPRequestHandler):
        # BaseHTTPRequestHandler.timeout이 설정되면 소켓에 적용된다 (#219).
        timeout = _SOCKET_TIMEOUT_SECONDS

        def _dispatch(self, method: str) -> None:
            self._request_id = uuid.uuid4().hex[:12]
            # 쿼리 스트링이 경로/run_id로 새지 않도록 path 컴포넌트만으로 라우팅하고,
            # 쿼리는 별도로 dispatch에 전달한다 (#252). body 크기 상한을 고르려면
            # method+path를 먼저 알아야 하므로 body를 읽기 전에 파싱한다.
            # getattr 기본값은 실제 서빙 경로에서는 절대 쓰이지 않는다 —
            # BaseHTTPRequestHandler.parse_request()가 do_*() 호출 전에 항상
            # self.path를 채운다. 오직 body-read 실패만 다루는 단위 테스트가
            # object.__new__()로 handler를 만들어 self.path를 생략하는 경우에만
            # 쓰인다 (#498 이전부터의 기존 테스트 fixture, TestHttpRobustness).
            split = urlsplit(getattr(self, "path", ""))
            path = split.path
            # POST /uploads(#498)만 JSON이 아니라 파일 binary body를 받는다 — 다른
            # 모든 endpoint는 지금까지와 동일하게 JSON body만 받는다.
            is_binary_upload = method == "POST" and path == _UPLOADS_PATH
            max_body_bytes = resolve_max_upload_bytes() if is_binary_upload else _MAX_BODY_BYTES
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                self._write(400, {"error": "invalid Content-Length header"})
                return
            if length < 0:
                self._write(400, {"error": "invalid Content-Length header"})
                return
            # 선언된 길이가 상한을 넘으면 body를 읽지 않고 413으로 거부한다 (#186).
            if length > max_body_bytes:
                self._write(413, {"error": "request body too large"})
                return
            # body 읽기: 타임아웃이나 불완전한 읽기는 dropped connection 대신
            # JSON 400으로 처리한다 (#219).
            if length:
                try:
                    raw = self.rfile.read(length)
                except TimeoutError:
                    self._write(400, {"error": "request body read timed out"})
                    return
                if len(raw) < length:
                    self._write(400, {"error": "incomplete request body"})
                    return
            else:
                raw = b""
            body: dict[str, JsonValue] | None = None
            raw_body: bytes | None = None
            if is_binary_upload:
                raw_body = raw
            elif raw:
                try:
                    parsed = cast(object, json.loads(raw))
                except json.JSONDecodeError:
                    self._write(400, {"error": "invalid JSON body"})
                    return
                # HTTP 어댑터는 임의의 JSON 최상위 타입을 받을 수 있지만, 서비스는
                # 매핑(객체)만 다룬다. 스칼라/배열 body는 TypeError 대신 400으로 거부 (#183).
                if not isinstance(parsed, dict):
                    self._write(400, {"error": "JSON body must be an object"})
                    return
                body = cast(dict[str, JsonValue], parsed)
            # dispatch()에서 예상치 못한 예외가 발생하면 연결을 끊는 대신 JSON 500을
            # 반환한다. 상세 정보는 서버 로그에만 기록하고 클라이언트에는 누설하지 않는다 (#218).
            try:
                response = dispatch(
                    service,
                    method,
                    path,
                    body,
                    query=split.query,
                    api_key=self.headers.get("X-API-Key"),
                    bearer_token=self.headers.get("Authorization"),
                    raw_body=raw_body,
                    # 인증 실패 스로틀의 클라이언트 식별자. TCP peer 주소만 쓰고
                    # X-Forwarded-For는 읽지 않는다 — 헤더는 위조 가능하다.
                    client_id=self.client_address[0] if self.client_address else None,
                )
            except Exception:
                _logger.error(
                    "Unhandled exception in dispatch: %s %s\n%s",
                    method,
                    path,
                    traceback.format_exc(),
                    extra={"request_id": getattr(self, "_request_id", None)},
                )
                self._write(500, {"error": "internal server error"})
                return
            # FileResponse인지 ServiceResponse인지 확인하여 분기 처리 (#323)
            if isinstance(response, FileResponse):
                self._write_file(response)
            else:
                self._write(response.status_code, response.body)

        def _send_cors_headers(self, origin: str | None = None) -> None:
            """CORS 헤더를 전송한다 (#322).

            Args:
                origin: 요청의 Origin 헤더값. None이면 same-origin으로 간주한다.
            """
            allowed = _get_allowed_origins()
            # 응답의 CORS 헤더가 요청 Origin에 따라 달라지므로, 허용 여부와 무관하게
            # 항상 Vary: Origin을 보낸다. 이게 없으면 캐싱 프록시/CDN이 한 오리진용
            # Access-Control-Allow-Origin 응답을 다른 오리진에 그대로 돌려줄 수 있다.
            self.send_header("Vary", "Origin")
            # Same-origin이거나 허용 목록에 있으면 CORS 헤더를 보낸다.
            if _is_origin_allowed(origin, allowed):
                if origin is not None:
                    # 크로스오리진 요청이면 허용된 오리진을 명시한다.
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Access-Control-Allow-Credentials", "true")
                else:
                    # Same-origin 요청이면 특정 오리진 제한 없이 허용한다.
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", _CORS_ALLOWED_HEADERS)
                self.send_header("Access-Control-Max-Age", "86400")

        def _write(self, status_code: int, body: dict[str, JsonValue]) -> None:
            payload = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            if hasattr(self, "_request_id"):
                self.send_header("X-Request-ID", self._request_id)
            self._send_cors_headers(origin=self.headers.get("Origin"))
            self.end_headers()
            _ = self.wfile.write(payload)

        def _write_file(self, response: FileResponse) -> None:
            """파일 응답을 작성한다 (#323).

            파일을 통째로 메모리에 올리지 않고 조각으로 흘려보낸다. 예전에는
            ``read_bytes()`` 로 한 번에 읽었다 — 응답 하나가 파일 크기만큼
            메모리를 썼고, 서빙하는 것은 build artifact(parquet/jsonl)라 크기에
            상한이 없다. 동시 다운로드 몇 개로 프로세스가 죽을 수 있었다.

            열기/stat 실패는 헤더를 보내기 전이라 500 으로 답할 수 있지만, 읽기
            중간의 실패는 그럴 수 없다 — 이미 상태줄과 Content-Length 를 보냈다.
            그 경우 연결을 끊어서 클라이언트가 잘린 파일을 온전한 것으로 받지
            않게 한다.
            """
            try:
                size = response.file_path.stat().st_size
                handle = response.file_path.open("rb")
            except OSError as exc:
                _logger.error("Failed to read file %s: %s", response.file_path, exc)
                self._write(500, {"error": "failed to read file"})
                return

            self.send_response(response.status_code)
            self.send_header(
                "Content-Type", _content_type_header(_get_mime_type(response.file_path))
            )
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{response.filename}"')
            self._send_cors_headers(origin=self.headers.get("Origin"))
            self.end_headers()

            remaining = size
            try:
                with handle:
                    while remaining > 0:
                        chunk = handle.read(min(_FILE_CHUNK_BYTES, remaining))
                        if not chunk:
                            break
                        _ = self.wfile.write(chunk)
                        remaining -= len(chunk)
            except OSError as exc:
                _logger.error("Failed while streaming %s: %s", response.file_path, exc)
                self.close_connection = True
                return
            if remaining:
                # 약속한 Content-Length 만큼 보내지 못했다(서빙 도중 파일이 줄었다).
                _logger.error(
                    "File %s shrank while streaming: %d bytes short",
                    response.file_path,
                    remaining,
                )
                self.close_connection = True

        def do_GET(self) -> None:  # noqa: N802 - http.server 규약
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - http.server 규약
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802 - http.server 규약
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802 - http.server 규약
            self._dispatch("DELETE")

        def do_OPTIONS(self) -> None:  # noqa: N802 - http.server 규약
            # CORS preflight 요청에 응답한다 (#322). body 없이 204로 허용 헤더만 반환한다.
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._send_cors_headers(origin=self.headers.get("Origin"))
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            # 기본 stderr 접근 로그를 억제한다.
            return

    return _Handler


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """동시 처리 스레드 수가 제한된 ThreadingHTTPServer (#253).

    ThreadingHTTPServer는 연결마다 새 스레드를 무제한으로 생성해, 악의적이거나
    비정상적인 클라이언트가 다수의 동시 연결을 열면 스레드/메모리 고갈로 서비스가
    중단될 수 있다. 요청 처리를 고정 크기 ThreadPoolExecutor에 위임해 동시
    처리량에 상한을 둔다.

    스레드 수만 제한하는 것으로는 부족하다. ``ThreadPoolExecutor`` 의 작업 큐는
    무한이라, 풀이 가득 찬 뒤에 들어오는 연결은 소켓을 열어 둔 채 큐에 그대로
    쌓였다 — 스레드는 열 개여도 열린 파일 디스크립터와 큐 항목은 접속하는 만큼
    늘어난다. slowloris 처럼 연결만 잔뜩 여는 쪽에는 이게 실제 한도였다.

    그래서 "처리 중 + 대기 중" 연결 수에 상한(``max_workers`` +
    ``max_pending_requests``)을 두고, 넘어온 연결은 기다리게 두지 않고 503 을
    쓴 뒤 바로 닫는다. 대기열에 넣어 놓고 30초 뒤 타임아웃시키는 것보다
    즉시 거절이 정직하고, 클라이언트도 재시도 판단을 바로 할 수 있다.
    """

    daemon_threads = True

    def __init__(
        self,
        *args: Any,
        max_workers: int = _DEFAULT_MAX_WORKERS,
        max_pending_requests: int = _DEFAULT_MAX_PENDING_REQUESTS,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="kpubdata-http"
        )
        self._admission_lock = Lock()
        self._inflight = 0
        self._max_inflight = max_workers + max(0, max_pending_requests)

    def process_request(
        self, request: _socket | tuple[bytes, _socket], client_address: Any
    ) -> None:
        # 새 스레드를 직접 만드는 대신 고정 크기 풀에 위임한다. 풀이 가득 차면
        # 초과 요청은 스레드를 점유하지 않고 풀 큐에서 대기하되, 그 대기열
        # 자체에도 상한이 있다.
        with self._admission_lock:
            admitted = self._inflight < self._max_inflight
            if admitted:
                self._inflight += 1
        if not admitted:
            self._reject(request, client_address)
            return
        try:
            future = self._executor.submit(self.process_request_thread, request, client_address)
        except RuntimeError:
            # server_close() 이후의 연결. 큐잉되지 않았으니 카운터를 되돌린다 —
            # done callback 이 붙지 않아 아무도 대신 줄여 주지 않는다.
            with self._admission_lock:
                self._inflight -= 1
            self.shutdown_request(request)
            return
        future.add_done_callback(self._release)

    def _release(self, _future: object) -> None:
        with self._admission_lock:
            self._inflight -= 1

    def _reject(self, request: _socket | tuple[bytes, _socket], client_address: Any) -> None:
        """대기 한도를 넘긴 연결을 503 으로 즉시 돌려보낸다."""
        _logger.warning(
            "rejecting connection from %s: %d requests already in flight",
            client_address,
            self._max_inflight,
        )
        try:
            cast(_socket, request).sendall(_OVERLOADED_RESPONSE)
        except OSError:
            # 상대가 이미 끊었다. 닫기만 하면 된다.
            pass
        finally:
            self.shutdown_request(request)

    def server_close(self) -> None:
        super().server_close()
        self._executor.shutdown(wait=False, cancel_futures=True)


def serve(
    service: BuilderService,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    max_workers: int = _DEFAULT_MAX_WORKERS,
) -> None:
    """BuilderService를 블로킹 HTTP 서버로 제공한다.

    단일 스레드 HTTPServer 대신 BoundedThreadingHTTPServer를 사용하여 느린
    클라이언트가 서버 전체를 멈추지 않으면서도 (#219), 동시 처리 스레드 수에
    상한을 두어 DoS를 방지한다 (#253).

    SIGTERM을 받으면 serve_forever를 중단하고 진행 중 요청이 끝나도록
    우아한 종료(graceful shutdown)를 수행한다 (#374). ACA/K8s 롤링 업데이트가
    컨테이너에 SIGTERM을 보낼 때 작업이 강제로 절단되지 않는다.
    SIGINT(Ctrl-C)는 건드리지 않아 KeyboardInterrupt가 자연히 전파된다.

    매개변수:
        service: 노출할 BuilderService.
        host: 바인딩 호스트.
        port: 바인딩 포트.
        max_workers: 동시에 요청을 처리할 최대 스레드 수.
    """
    # 기동 시 OIDC 설정 검증 (fail-closed, #385). OIDC 비활성 시 no-op.
    validate_oidc_config()
    # dev-mode로 기동하면 인증이 통째로 꺼진다 — 운영 배포에서 사고가 나지 않도록
    # 경고를 남기고, 모순된 조합(OIDC 구성 + dev-mode)은 기동을 거부한다.
    validate_dev_mode()
    # 기동 시 상태 백엔드 설정 검증 (fail-closed, ADR 0016). sqlite 기본 시 no-op;
    # cubrid 이면 URL·드라이버를 조기에 확인한다.
    validate_storage_config()
    server = BoundedThreadingHTTPServer(
        (host, port), make_handler(service), max_workers=max_workers
    )

    def _shutdown(_signum: int, _frame: object) -> None:
        # 별도 스레드에서 shutdown해야 serve_forever 블록이 풀린다 (http.server 권장 패턴).
        import threading

        threading.Thread(target=server.shutdown, daemon=True).start()

    import signal

    # SIGTERM만 커스텀 핸들러로 연결 (컨테이너 오케스트레이터).
    # SIGINT는 건드리지 않아 Ctrl-C의 KeyboardInterrupt가 자연히 전파된다.
    previous_term = signal.signal(signal.SIGTERM, _shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        signal.signal(signal.SIGTERM, previous_term)


__all__ = ["BoundedThreadingHTTPServer", "make_handler", "serve"]
