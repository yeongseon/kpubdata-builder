"""HTTP 서비스 façade(#36): validate/preview/build/artifacts 로직과 라우팅 검증."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterable
from http.server import HTTPServer
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.http import make_handler
from kpubdata_builder.spec import JsonValue

VALID_SPEC_YAML = (
    """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
    + "\n"
)

INVALID_SPEC_YAML = (
    # 파싱은 통과하지만 validate_spec에서 미지원 exporter kind로 실패하는 명세.
    """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: unsupported_format
    output_path: out/data.jsonl
""".strip()
    + "\n"
)


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda: client)


class TestVersion:
    def test_version_reports_api_contract_version(self, tmp_path: Path) -> None:
        # #209: 계약 버전을 알리는 메타 엔드포인트.
        from kpubdata_builder.service import API_CONTRACT_VERSION

        resp = _service(tmp_path).version()
        assert resp.status_code == 200
        assert resp.body["api_version"] == API_CONTRACT_VERSION
        assert resp.body["service"] == "kpubdata-builder"

    def test_version_route(self, tmp_path: Path) -> None:
        from kpubdata_builder.service import API_CONTRACT_VERSION

        resp = dispatch(_service(tmp_path), "GET", "/version", None)
        assert resp.status_code == 200
        assert resp.body["api_version"] == API_CONTRACT_VERSION


class TestValidate:
    def test_valid_spec_returns_200(self, tmp_path: Path) -> None:
        from kpubdata_builder.service import API_CONTRACT_VERSION

        resp = _service(tmp_path).validate(VALID_SPEC_YAML)
        assert resp.status_code == 200
        assert resp.body["status"] == "valid"
        assert resp.body["dataset_id"] == "dataset.sample"
        # #209: 응답에 계약 버전을 실어 소비자가 호환성을 확인할 수 있다.
        assert resp.body["api_version"] == API_CONTRACT_VERSION

    def test_invalid_spec_returns_400(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).validate(INVALID_SPEC_YAML)
        assert resp.status_code == 400
        assert resp.body["status"] == "invalid"


class TestPreview:
    def test_returns_schema_and_sample(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=1)
        assert resp.status_code == 200
        previews = resp.body["previews"]
        assert isinstance(previews, list)
        assert previews[0]["source_key"] == "datago.air_quality"

    def test_preview_writes_no_files(self, tmp_path: Path) -> None:
        _service(tmp_path).preview(VALID_SPEC_YAML)
        assert list(tmp_path.iterdir()) == []


class TestPreviewLimitGuard:
    def test_preview_direct_call_rejects_zero_limit(self, tmp_path: Path) -> None:
        # #225: BuilderService.preview()를 직접 호출할 때도 limit<1이면 400을 반환한다.
        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=0)
        assert resp.status_code == 400
        assert "limit" in str(resp.body.get("error", ""))

    def test_preview_direct_call_rejects_negative_limit(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=-5)
        assert resp.status_code == 400


class TestBuild:
    def test_build_runs_and_reports_manifest(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).build(VALID_SPEC_YAML, run_id="run1")
        assert resp.status_code == 200
        assert resp.body["status"] == "ok"
        assert resp.body["run_id"] == "run1"
        assert (tmp_path / "run1" / "manifest.json").exists()


class TestArtifacts:
    def test_lists_artifacts_after_build(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1")

        resp = service.artifacts("run1")
        assert resp.status_code == 200
        files = resp.body["files"]
        assert isinstance(files, list)
        assert any("manifest.json" in f for f in files)

    def test_missing_run_returns_404(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).artifacts("nope")
        assert resp.status_code == 404


class TestListBuilds:
    def test_empty_when_no_runs(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).list_builds()
        assert resp.status_code == 200
        assert resp.body["builds"] == []

    def test_lists_run_after_build(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1")

        resp = service.list_builds()
        assert resp.status_code == 200
        builds = resp.body["builds"]
        assert isinstance(builds, list)
        assert len(builds) == 1
        assert builds[0]["run_id"] == "run1"  # type: ignore[index]
        assert builds[0]["status"] == "ok"  # type: ignore[index]

    def test_skips_dirs_without_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "no-manifest-dir").mkdir()
        resp = _service(tmp_path).list_builds()
        assert resp.status_code == 200
        assert resp.body["builds"] == []

    def test_dispatch_get_builds_returns_200(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run2")
        resp = dispatch(service, "GET", "/builds", None)
        assert resp.status_code == 200
        builds = resp.body["builds"]
        assert isinstance(builds, list)
        assert any(b["run_id"] == "run2" for b in builds)  # type: ignore[index,union-attr]

    def test_dispatch_limit_guard(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/builds", {"limit": 0})
        assert resp.status_code == 400
        assert "limit" in str(resp.body.get("error", ""))

    def test_dispatch_get_builds_query_limit(self, tmp_path: Path) -> None:
        # ?limit=N 쿼리 파라미터를 지원해야 한다 (#252).
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run_q1")
        service.build(VALID_SPEC_YAML, run_id="run_q2")
        resp = dispatch(service, "GET", "/builds", None, query="limit=1")
        assert resp.status_code == 200
        builds = resp.body["builds"]
        assert isinstance(builds, list)
        assert len(builds) == 1

    def test_dispatch_get_builds_query_limit_guard(self, tmp_path: Path) -> None:
        # 쿼리 limit이 양의 정수가 아니면 400 (#252).
        resp = dispatch(_service(tmp_path), "GET", "/builds", None, query="limit=0")
        assert resp.status_code == 400
        resp = dispatch(_service(tmp_path), "GET", "/builds", None, query="limit=abc")
        assert resp.status_code == 400


class TestDispatch:
    def test_routes_post_validate(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "POST", "/validate", {"spec": VALID_SPEC_YAML})
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 200

    def test_unknown_route_returns_404(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/nope", None)
        assert resp.status_code == 404

    def test_build_route(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path), "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "r2"}
        )
        assert resp.status_code == 200
        assert resp.body["run_id"] == "r2"

    def test_artifacts_route(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "r3"})
        resp = dispatch(service, "GET", "/artifacts/r3", None)
        assert resp.status_code == 200

    def test_preview_rejects_non_integer_limit(self, tmp_path: Path) -> None:
        # 클라이언트가 limit을 잘못된 타입으로 보내면 조용히 기본값으로 떨어뜨리지 않고 400.
        resp = dispatch(
            _service(tmp_path), "POST", "/preview", {"spec": VALID_SPEC_YAML, "limit": "5"}
        )
        assert resp.status_code == 400
        assert "limit" in str(resp.body.get("error", ""))

    def test_preview_rejects_non_positive_limit(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path), "POST", "/preview", {"spec": VALID_SPEC_YAML, "limit": 0}
        )
        assert resp.status_code == 400

    def test_build_rejects_non_string_run_id(self, tmp_path: Path) -> None:
        # run_id가 문자열이 아니면 조용히 자동 생성 id로 떨어뜨리지 않고 400 (#185).
        resp = dispatch(
            _service(tmp_path), "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": 123}
        )
        assert resp.status_code == 400
        assert "run_id" in str(resp.body.get("error", ""))

    def test_build_rejects_blank_run_id(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path), "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "   "}
        )
        assert resp.status_code == 400

    def test_build_rejects_unsafe_run_id_with_400(self, tmp_path: Path) -> None:
        # 경로 안전하지 않은 run_id는 500/연결 끊김이 아니라 구조화된 400을 반환한다 (#200).
        resp = dispatch(
            _service(tmp_path), "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "../bad"}
        )
        assert resp.status_code == 400
        assert "run_id" in str(resp.body.get("error", ""))


class TestBuildFailureResponseCode:
    def test_failed_build_returns_502(self, tmp_path: Path) -> None:
        # 소스 fetch가 실패하면 status=failed + 502 — 매니페스트는 partial 정책으로 남는다.
        missing_source_yaml = VALID_SPEC_YAML.replace("air_quality", "missing")
        resp = _service(tmp_path).build(missing_source_yaml, run_id="run1")

        assert resp.status_code == 502
        assert resp.body["status"] == "failed"
        assert (tmp_path / "run1" / "manifest.json").exists()


@pytest.fixture()
def http_server(
    tmp_path: Path,
) -> Iterable[tuple[str, HTTPServer, threading.Thread]]:
    """실제 HTTPServer를 임의 포트에 띄워서 어댑터 레벨 동작을 검증한다."""
    service = _service(tmp_path)
    server = HTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        yield base_url, server, thread
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


class TestHttpAdapter:
    def test_unsafe_run_id_returns_400_not_500(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # 경로 안전하지 않은 run_id가 어댑터에서 500/연결 끊김이 아니라 400이어야 한다 (#200).
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/build",
            data=json.dumps({"spec": VALID_SPEC_YAML, "run_id": "../bad"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2.0)
        assert exc_info.value.code == 400

    def test_malformed_json_body_returns_400(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/validate",
            data=b"not-json{{",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2.0)
        assert exc_info.value.code == 400
        body = cast(dict[str, object], json.loads(exc_info.value.read()))
        assert "invalid JSON body" in str(body.get("error", ""))

    def test_unknown_path_returns_404(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        base_url, _, _ = http_server
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/nope", timeout=2.0)
        assert exc_info.value.code == 404

    def test_non_object_json_body_returns_400(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # 유효하지만 객체가 아닌 JSON(스칼라)은 TypeError로 중단되지 않고 400 (#183).
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/validate",
            data=b"1",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2.0)
        assert exc_info.value.code == 400
        body = cast(dict[str, object], json.loads(exc_info.value.read()))
        assert "object" in str(body.get("error", ""))

    def test_query_string_is_ignored_in_routing(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # 쿼리 스트링이 붙어도 경로 컴포넌트로만 라우팅된다 (#184).
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/validate?x=1",
            data=json.dumps({"spec": VALID_SPEC_YAML}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200

    def test_query_string_does_not_corrupt_run_id(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # /artifacts/<run_id>?download=1 의 쿼리가 run_id로 새지 않아야 한다 (#184).
        base_url, _, _ = http_server
        build_req = urllib.request.Request(
            f"{base_url}/build",
            data=json.dumps({"spec": VALID_SPEC_YAML, "run_id": "run1"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(build_req, timeout=2.0) as response:
            assert response.status == 200
        with urllib.request.urlopen(f"{base_url}/artifacts/run1?download=1", timeout=2.0) as resp:
            assert resp.status == 200
            body = cast(dict[str, object], json.loads(resp.read()))
        assert body["run_id"] == "run1"

    def test_oversized_body_returns_413(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # 선언된 Content-Length가 상한을 넘으면 body를 읽지 않고 413으로 거부 (#186).
        import http.client

        base_url, _, _ = http_server
        host_port = base_url.removeprefix("http://")
        host, port = host_port.split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=2.0)
        try:
            conn.putrequest("POST", "/validate")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(100 * 1024 * 1024))
            conn.endheaders()  # body는 보내지 않는다 — 핸들러가 헤더만 보고 거부.
            response = conn.getresponse()
            assert response.status == 413
        finally:
            conn.close()

    def test_valid_post_validate_round_trips(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # 어댑터가 정상 요청을 dispatch에 전달하고 JSON 응답을 직렬화하는지 확인.
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/validate",
            data=json.dumps({"spec": VALID_SPEC_YAML}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200
            body = cast(dict[str, object], json.loads(response.read()))
        assert body["status"] == "valid"


class TestHttpRobustness:
    """#218 (JSON 500 handler) 과 #219 (DoS hardening) 검증."""

    def test_dispatch_exception_returns_json_500(
        self, tmp_path: Path, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # dispatch()에서 예외가 발생해도 연결이 끊기지 않고 JSON 500이 반환돼야 한다 (#218).
        # 패치로 dispatch를 교체해 인위적으로 예외를 발생시킨다.
        import unittest.mock

        base_url, _, _ = http_server

        with unittest.mock.patch(
            "kpubdata_builder.service.http.dispatch",
            side_effect=RuntimeError("boom"),
        ):
            req = urllib.request.Request(
                f"{base_url}/validate",
                data=json.dumps({"spec": VALID_SPEC_YAML}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req, timeout=2.0)
        assert exc_info.value.code == 500
        body = cast(dict[str, object], json.loads(exc_info.value.read()))
        assert body.get("error") == "internal server error"
        # 내부 예외 메시지("boom")가 클라이언트에 누설되지 않아야 한다.
        assert "boom" not in json.dumps(body)

    def test_make_handler_has_socket_timeout(self, tmp_path: Path) -> None:
        # 핸들러 클래스에 timeout이 설정돼 있어야 느린 클라이언트가 스레드를 무한 점거하지
        # 않는다 (#219). BaseHTTPRequestHandler.timeout 이 None이면 무제한이다.
        from kpubdata_builder.service.http import _SOCKET_TIMEOUT_SECONDS, make_handler

        handler_cls = make_handler(_service(tmp_path))
        assert handler_cls.timeout is not None
        assert handler_cls.timeout == _SOCKET_TIMEOUT_SECONDS
        assert handler_cls.timeout > 0

    def test_serve_uses_threading_http_server(self, tmp_path: Path) -> None:
        # serve()가 ThreadingHTTPServer를 사용해야 느린 클라이언트가 서버 전체를
        # 멈추지 않는다 (#219).
        import contextlib
        import unittest.mock
        from http.server import ThreadingHTTPServer

        from kpubdata_builder.service.http import serve

        created_servers: list[object] = []
        original_init = ThreadingHTTPServer.__init__

        def capturing_init(self: object, *args: object, **kwargs: object) -> None:
            created_servers.append(self)
            original_init(self, *args, **kwargs)  # type: ignore[misc]

        with (
            unittest.mock.patch.object(ThreadingHTTPServer, "__init__", capturing_init),
            unittest.mock.patch.object(
                ThreadingHTTPServer, "serve_forever", side_effect=KeyboardInterrupt
            ),
            unittest.mock.patch.object(ThreadingHTTPServer, "server_close"),
            contextlib.suppress(KeyboardInterrupt),
        ):
            serve(_service(tmp_path), host="127.0.0.1", port=0)

        assert len(created_servers) == 1
        assert isinstance(created_servers[0], ThreadingHTTPServer)

    def test_oversized_body_content_length_returns_413_http(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # Content-Length가 _MAX_BODY_BYTES를 넘으면 body를 읽지 않고 413으로 거부 (#219).
        import http.client

        base_url, _, _ = http_server
        host_port = base_url.removeprefix("http://")
        host, port = host_port.split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=2.0)
        try:
            conn.putrequest("POST", "/validate")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(20 * 1024 * 1024))  # 20 MiB > 10 MiB 상한
            conn.endheaders()  # body는 보내지 않는다 — 핸들러가 헤더만 보고 거부.
            response = conn.getresponse()
            assert response.status == 413
            resp_body = cast(dict[str, object], json.loads(response.read()))
            assert "too large" in str(resp_body.get("error", ""))
        finally:
            conn.close()

    def test_body_read_timeout_returns_json_400(self, tmp_path: Path) -> None:
        # rfile.read()가 TimeoutError를 던지면 연결 끊김이 아닌 JSON 400이어야 한다 (#219).
        import io

        handler_cls = make_handler(_service(tmp_path))

        captured: list[tuple[int, dict[str, object]]] = []

        class _PatchedHandler(handler_cls):  # type: ignore[valid-type]
            def _write(self, status_code: int, body: dict[str, object]) -> None:  # type: ignore[override]
                captured.append((status_code, body))

        slow_rfile = io.BytesIO(b"")

        def _timeout_read(n: int) -> bytes:
            raise TimeoutError("timed out")

        slow_rfile.read = _timeout_read  # type: ignore[method-assign]

        h = object.__new__(_PatchedHandler)
        h.rfile = slow_rfile
        h.headers = {"Content-Length": "10"}  # type: ignore[assignment]
        h._dispatch("POST")

        assert len(captured) == 1
        status, body = captured[0]
        assert status == 400
        assert "timed out" in str(body.get("error", ""))

    def test_truncated_body_returns_json_400(self, tmp_path: Path) -> None:
        # Content-Length보다 짧은 body(EOF)는 연결 끊김이 아닌 JSON 400이어야 한다 (#219).
        import io

        handler_cls = make_handler(_service(tmp_path))

        captured: list[tuple[int, dict[str, object]]] = []

        class _PatchedHandler(handler_cls):  # type: ignore[valid-type]
            def _write(self, status_code: int, body: dict[str, object]) -> None:  # type: ignore[override]
                captured.append((status_code, body))

        # Content-Length는 10이지만 실제로는 5바이트만 전달.
        truncated_rfile = io.BytesIO(b"hello")

        h = object.__new__(_PatchedHandler)
        h.rfile = truncated_rfile
        h.headers = {"Content-Length": "10"}  # type: ignore[assignment]
        h._dispatch("POST")

        assert len(captured) == 1
        status, body = captured[0]
        assert status == 400
        assert "incomplete" in str(body.get("error", ""))
