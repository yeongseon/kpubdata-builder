"""stdlib http.server 기반 HTTP 어댑터 (#36).

새 의존성 없이 BuilderService를 HTTP로 노출한다. 요청 파싱/응답 직렬화만
담당하고 실제 로직은 app.dispatch에 위임한다.

주요 구성:
    - make_handler: BuilderService에 바인딩된 요청 핸들러 클래스 생성
    - serve: 블로킹 HTTP 서버 실행
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast
from urllib.parse import urlsplit

from ..spec import JsonValue
from .app import BuilderService, dispatch

# 단일 요청이 메모리를 고갈시키거나 단일 스레드 서버를 멈추게 하지 않도록 body 크기를
# 제한한다. spec YAML 요청에 충분하면서도 남용을 막는 보수적 상한 (#186).
_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB


def make_handler(service: BuilderService) -> type[BaseHTTPRequestHandler]:
    """주어진 BuilderService에 바인딩된 요청 핸들러 클래스를 생성한다."""

    class _Handler(BaseHTTPRequestHandler):
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
            raw = self.rfile.read(length) if length else b""
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
            response = dispatch(service, method, path, body)
            self._write(response.status_code, response.body)

        def _write(self, status_code: int, body: dict[str, JsonValue]) -> None:
            payload = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            _ = self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - http.server 규약
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - http.server 규약
            self._dispatch("POST")

        def log_message(self, format: str, *args: object) -> None:
            # 기본 stderr 접근 로그를 억제한다.
            return

    return _Handler


def serve(service: BuilderService, *, host: str = "127.0.0.1", port: int = 8000) -> None:
    """BuilderService를 블로킹 HTTP 서버로 제공한다.

    매개변수:
        service: 노출할 BuilderService.
        host: 바인딩 호스트.
        port: 바인딩 포트.
    """
    server = HTTPServer((host, port), make_handler(service))
    try:
        server.serve_forever()
    finally:
        server.server_close()


__all__ = ["make_handler", "serve"]
