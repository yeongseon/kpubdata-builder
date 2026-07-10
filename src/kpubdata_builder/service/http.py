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
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urlsplit

from ..spec import JsonValue
from .app import BuilderService, dispatch

# 단일 요청이 메모리를 고갈시키거나 단일 스레드 서버를 멈추게 하지 않도록 body 크기를
# 제한한다. spec YAML 요청에 충분하면서도 남용을 막는 보수적 상한 (#186).
_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB

# 소켓 읽기 타임아웃(초). 느린 클라이언트/slowloris 공격이 스레드를 무한 점거하지 않도록
# 한다 (#219). ThreadingHTTPServer를 사용하더라도 각 연결 스레드가 여기서 해제된다.
_SOCKET_TIMEOUT_SECONDS = 30.0

# CORS 허용 Origin. 기본값은 로컬 개발 편의를 위해 모든 오리진(`*`)이지만, 환경변수로
# 특정 오리진(예: http://localhost:5173)만 허용하도록 제한할 수 있다 (#254 보안 강화).
_CORS_ALLOW_ORIGIN_ENV = "KPUBDATA_BUILDER_CORS_ALLOW_ORIGIN"
_DEFAULT_CORS_ALLOW_ORIGIN = "*"

_logger = logging.getLogger(__name__)


def make_handler(service: BuilderService) -> type[BaseHTTPRequestHandler]:
    """주어진 BuilderService에 바인딩된 요청 핸들러 클래스를 생성한다."""

    class _Handler(BaseHTTPRequestHandler):
        # BaseHTTPRequestHandler.timeout이 설정되면 소켓에 적용된다 (#219).
        timeout = _SOCKET_TIMEOUT_SECONDS

        def _dispatch(self, method: str) -> None:
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                self._write(400, {"error": "invalid Content-Length header"})
                return
            if length < 0:
                self._write(400, {"error": "invalid Content-Length header"})
                return
            # 선언된 길이가 상한을 넘으면 body를 읽지 않고 413으로 거부한다 (#186).
            if length > _MAX_BODY_BYTES:
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
            if raw:
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
            # 쿼리 스트링이 경로/run_id로 새지 않도록 path 컴포넌트만으로 라우팅한다 (#184).
            path = urlsplit(self.path).path
            # dispatch()에서 예상치 못한 예외가 발생하면 연결을 끊는 대신 JSON 500을
            # 반환한다. 상세 정보는 서버 로그에만 기록하고 클라이언트에는 누설하지 않는다 (#218).
            try:
                response = dispatch(service, method, path, body)
            except Exception:
                _logger.error(
                    "Unhandled exception in dispatch: %s %s\n%s",
                    method,
                    path,
                    traceback.format_exc(),
                )
                self._write(500, {"error": "internal server error"})
                return
            self._write(response.status_code, response.body)

        def _send_cors_headers(self) -> None:
            # 로컬 개발 도구로서 Studio 등 브라우저 클라이언트의 크로스오리진 요청을
            # 허용한다 (#254). 기본값은 `*`이며, 환경변수로 특정 오리진만
            # 허용하도록 제한해 공격 표면을 줄일 수 있다.
            allow_origin = os.environ.get(_CORS_ALLOW_ORIGIN_ENV, _DEFAULT_CORS_ALLOW_ORIGIN)
            self.send_header("Access-Control-Allow-Origin", allow_origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")

        def _write(self, status_code: int, body: dict[str, JsonValue]) -> None:
            payload = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self._send_cors_headers()
            self.end_headers()
            _ = self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - http.server 규약
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - http.server 규약
            self._dispatch("POST")

        def do_OPTIONS(self) -> None:  # noqa: N802 - http.server 규약
            # CORS preflight 요청에 응답한다 (#254). body 없이 204로 허용 헤더만 반환한다.
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._send_cors_headers()
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            # 기본 stderr 접근 로그를 억제한다.
            return

    return _Handler


def serve(service: BuilderService, *, host: str = "127.0.0.1", port: int = 8000) -> None:
    """BuilderService를 블로킹 HTTP 서버로 제공한다.

    단일 스레드 HTTPServer 대신 ThreadingHTTPServer를 사용하여 느린 클라이언트가
    서버 전체를 멈추지 않도록 한다 (#219).

    매개변수:
        service: 노출할 BuilderService.
        host: 바인딩 호스트.
        port: 바인딩 포트.
    """
    server = ThreadingHTTPServer((host, port), make_handler(service))
    try:
        server.serve_forever()
    finally:
        server.server_close()


__all__ = ["make_handler", "serve"]
