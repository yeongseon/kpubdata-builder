"""HTTP 서비스 façade(#36): validate/preview/build/artifacts 로직과 라우팅 검증."""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterable
from http.server import HTTPServer
from pathlib import Path
from typing import cast

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.http import _clear_cors_cache, make_handler
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
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


class _FakeCatalog:
    """``client.datasets`` 흉내 — DatasetRef 목록을 provider 필터로 반환."""

    def __init__(self, items: list[object] | None = None) -> None:
        self._items = items or []

    def list(self, *, provider: str | None = None) -> list[object]:
        if provider is None:
            return self._items
        return [i for i in self._items if getattr(i, "provider", None) == provider]


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(
        self,
        data: dict[str, list[dict[str, JsonValue]]],
        catalog_items: list[object] | None = None,
        auth_provider_names: tuple[str, ...] = (),
    ) -> None:
        self._data = data
        self.datasets = _FakeCatalog(catalog_items)
        self._auth_provider_names = auth_provider_names

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])

    def iter_authenticated_providers(self) -> tuple[object, ...]:
        return tuple(_FakeProvider(name) for name in self._auth_provider_names)


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name


class _CloseTrackingClient(_FakeClient):
    def __init__(
        self,
        data: dict[str, list[dict[str, JsonValue]]],
        catalog_items: list[object] | None = None,
    ) -> None:
        super().__init__(data, catalog_items=catalog_items)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


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
        # SQLite 인덱스 파일은 제외 (#309, ADR 0003)
        files = [p.name for p in tmp_path.iterdir() if not p.name.startswith("_builds")]
        assert files == []

    def test_preview_closes_request_client(self, tmp_path: Path) -> None:
        client = _CloseTrackingClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = service.preview(VALID_SPEC_YAML)

        assert resp.status_code == 200
        assert client.close_calls == 1


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

    def test_build_closes_request_client_when_source_fails(self, tmp_path: Path) -> None:
        client = _CloseTrackingClient({})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = service.build(VALID_SPEC_YAML, run_id="run1")

        assert resp.status_code == 502
        assert client.close_calls == 1

    def test_manifest_route_returns_written_manifest_json(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1")

        resp = dispatch(service, "GET", "/builds/run1/manifest", None)

        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 200
        assert resp.body["build_id"] == "run1"
        assert resp.body["schema_version"] == "1.0.0"

    def test_manifest_route_returns_404_for_missing_run(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/builds/nope/manifest", None)

        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 404


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


class TestApiKeyAuth:
    """API 키 인증(#248, #321, ADR 0006): X-API-Key 검증, fail-closed 정책."""

    def test_auth_required_when_env_and_dev_mode_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ADR 0006 fail-closed: dev-mode 미설정 + API 키 미설정 시 인증 거부 (401).
        monkeypatch.delenv("KPUBDATA_BUILDER_API_KEY", raising=False)
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        resp = dispatch(_service(tmp_path), "GET", "/version", None)
        assert resp.status_code == 401
        assert resp.body["error"]  # 구체적 reason은 auth 구현에 위임

    def test_auth_skipped_in_dev_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # dev-mode 설정 시 API 키가 없어도 인증 생략 (로컬 개발 편의).
        monkeypatch.delenv("KPUBDATA_BUILDER_API_KEY", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        resp = dispatch(_service(tmp_path), "GET", "/version", None)
        assert resp.status_code == 200

    def test_auth_skipped_in_dev_mode_variant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # dev-mode="1"도 인증 생략.
        monkeypatch.delenv("KPUBDATA_BUILDER_API_KEY", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "1")
        resp = dispatch(_service(tmp_path), "GET", "/version", None)
        assert resp.status_code == 200

    def test_rejects_missing_api_key_when_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        resp = dispatch(_service(tmp_path), "GET", "/version", None)
        assert resp.status_code == 401
        assert resp.body["error"]  # 구체적 reason은 auth 구현에 위임

    def test_rejects_wrong_api_key_when_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        resp = dispatch(_service(tmp_path), "GET", "/version", None, api_key="wrong")
        assert resp.status_code == 401

    def test_accepts_matching_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        resp = dispatch(_service(tmp_path), "GET", "/version", None, api_key="secret")
        assert resp.status_code == 200

    def test_build_route_requires_api_key_when_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # /build처럼 비용이 큰 엔드포인트도 예외 없이 보호돼야 한다.
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        resp = dispatch(
            _service(tmp_path), "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "auth1"}
        )
        assert resp.status_code == 401


class TestBuildFailureResponseCode:
    def test_failed_build_returns_502(self, tmp_path: Path) -> None:
        # 소스 fetch가 실패하면 status=failed + 502 — 매니페스트는 partial 정책으로 남는다.
        missing_source_yaml = VALID_SPEC_YAML.replace("air_quality", "missing")
        resp = _service(tmp_path).build(missing_source_yaml, run_id="run1")

        assert resp.status_code == 502
        assert resp.body["status"] == "failed"
        assert (tmp_path / "run1" / "manifest.json").exists()


@pytest.fixture(autouse=True)
def clear_cors_cache() -> None:
    """CORS 캐시를 각 테스트 전에 비운다 (#322)."""
    _clear_cors_cache()
    yield


@pytest.fixture()
def http_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterable[tuple[str, HTTPServer, threading.Thread]]:
    """실제 HTTPServer를 임의 포트에 띄워서 어댑터 레벨 동작을 검증한다."""
    # 테스트에서는 dev-mode를 설정하여 인증을 생략한다 (#321, ADR 0006).
    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
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


@pytest.fixture()
def http_server_with_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterable[tuple[str, HTTPServer, threading.Thread]]:
    """인증이 활성화된 HTTPServer (dev-mode 미설정, API 키 설정)."""
    monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
    monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
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

    def test_options_preflight_returns_204_with_cors(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # CORS preflight(OPTIONS)가 204와 허용 헤더를 반환해야 한다 (#254).
        base_url, _, _ = http_server
        req = urllib.request.Request(f"{base_url}/build", method="OPTIONS")
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 204
            assert response.headers["Access-Control-Allow-Origin"] == "*"
            assert response.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
            assert (
                response.headers["Access-Control-Allow-Headers"]
                == "Content-Type, X-API-Key, Authorization"
            )
            assert response.headers["Access-Control-Max-Age"] == "86400"

    def test_response_includes_cors_header(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # Same-origin 요청(Oriign 헤더 없음)은 CORS 헤더가 포함되어야 한다 (#322).
        base_url, _, _ = http_server
        with urllib.request.urlopen(f"{base_url}/version", timeout=2.0) as response:
            # Same-origin이면 `*`를 반환한다.
            assert response.headers["Access-Control-Allow-Origin"] == "*"

    def test_cors_default_denied_when_no_env(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 환경변수 미설정 시 크로스-오리진 요청은 CORS 헤더가 없어야 한다 (#322 default-deny).
        # env를 명확하게 지우고 테스트
        monkeypatch.delenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", raising=False)
        base_url, _, _ = http_server
        # Origin 헤더를 포함한 요청 (크로스-오리진으로 간주)
        req = urllib.request.Request(
            f"{base_url}/version", headers={"Origin": "http://localhost:5173"}
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            # default-deny이므로 CORS 헤더가 없어야 함
            assert "Access-Control-Allow-Origin" not in response.headers

    def test_cors_file_download_respects_allowlist(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 파일 응답(_write_file)도 CORS 허용 목록을 따라야 한다 (#382).
        # 이전에는 _write_file이 Origin을 전달하지 않아 same-origin으로 취급되어
        # 허용되지 않은 오리진에도 Access-Control-Allow-Origin: * 를 보냈다.
        monkeypatch.delenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", raising=False)
        base_url, _, _ = http_server
        build_req = urllib.request.Request(
            f"{base_url}/build",
            data=json.dumps({"spec": VALID_SPEC_YAML, "run_id": "run1"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(build_req, timeout=5.0) as response:
            assert response.status == 200
        # 크로스오리진 파일 다운로드 요청
        req = urllib.request.Request(
            f"{base_url}/artifacts/run1/manifest.json",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200
            # default-deny: 허용 목록에 없는 오리진은 CORS 헤더를 받지 않는다
            assert "Access-Control-Allow-Origin" not in response.headers

    def test_cors_file_download_allowed_origin_echoed(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 허용된 오리진의 파일 다운로드는 해당 오리진을 echo 해야 한다 (#382).
        monkeypatch.setenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", "http://localhost:5173")
        base_url, _, _ = http_server
        build_req = urllib.request.Request(
            f"{base_url}/build",
            data=json.dumps({"spec": VALID_SPEC_YAML, "run_id": "run1"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(build_req, timeout=5.0) as response:
            assert response.status == 200
        req = urllib.request.Request(
            f"{base_url}/artifacts/run1/manifest.json",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200
            assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"

    def test_cors_allowed_origins_configurable(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # KPUBDATA_BUILDER_ALLOWED_ORIGINS로 허용 Origin을 설정할 수 있어야 한다 (#322).
        monkeypatch.setenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", "http://localhost:5173")
        base_url, _, _ = http_server
        # Origin 헤더를 포함한 요청
        req = urllib.request.Request(
            f"{base_url}/version", headers={"Origin": "http://localhost:5173"}
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"

    def test_cors_multiple_origins_configurable(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 여러 Origin을 콤마로 구분하여 설정할 수 있어야 한다 (#322).
        monkeypatch.setenv(
            "KPUBDATA_BUILDER_ALLOWED_ORIGINS",
            "http://localhost:5173,https://studio.example.com",
        )
        base_url, _, _ = http_server
        # 첫 번째 오리진으로 요청
        req = urllib.request.Request(
            f"{base_url}/version", headers={"Origin": "http://localhost:5173"}
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
        # 두 번째 오리진으로 요청
        req = urllib.request.Request(
            f"{base_url}/version", headers={"Origin": "https://studio.example.com"}
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.headers["Access-Control-Allow-Origin"] == "https://studio.example.com"

    def test_cors_rejects_disallowed_origin(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 허용 목록에 없는 Origin은 CORS 헤더가 없어야 한다 (#322).
        monkeypatch.setenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", "http://localhost:5173")
        base_url, _, _ = http_server
        # 허용되지 않은 오리진으로 요청
        req = urllib.request.Request(f"{base_url}/version", headers={"Origin": "http://evil.com"})
        with urllib.request.urlopen(req, timeout=2.0) as response:
            # 허용되지 않은 오리진이므로 CORS 헤더가 없어야 함
            assert "Access-Control-Allow-Origin" not in response.headers

    def test_missing_api_key_returns_401_when_configured(
        self,
        http_server_with_auth: tuple[str, HTTPServer, threading.Thread],
    ) -> None:
        # 어댑터가 X-API-Key 헤더를 dispatch로 전달해야 한다 (#248).
        base_url, _, _ = http_server_with_auth
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/version", timeout=2.0)
        assert exc_info.value.code == 401

    def test_valid_api_key_header_is_accepted(
        self,
        http_server_with_auth: tuple[str, HTTPServer, threading.Thread],
    ) -> None:
        # http_server_with_auth fixture가 이미 API 키를 설정하므로 monkeypatch 불필요
        base_url, _, _ = http_server_with_auth
        req = urllib.request.Request(f"{base_url}/version", headers={"X-API-Key": "secret"})
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200

    def test_healthz_accessible_without_api_key(
        self,
        http_server_with_auth: tuple[str, HTTPServer, threading.Thread],
    ) -> None:
        # /healthz는 인증 게이트 밖에서 무인증 노출된다 (#372).
        # 프로브가 자격증명을 실을 수 없으므로 키 없이 200 + {"status":"ok"}만 반환.
        base_url, _, _ = http_server_with_auth
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=2.0) as response:
            assert response.status == 200
            body = cast(dict[str, object], json.loads(response.read()))
        assert body["status"] == "ok"
        assert "request_id" in body
        # 버전·서비스 메타 정보가 누출되지 않아야 한다.
        assert "api_version" not in body
        assert "service" not in body


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

    def test_serve_uses_bounded_threading_http_server(self, tmp_path: Path) -> None:
        # serve()가 BoundedThreadingHTTPServer를 사용해야 느린 클라이언트가 서버
        # 전체를 멈추지 않으면서도(#219) 동시 처리 스레드 수에 상한이 걸린다 (#253).
        import contextlib
        import unittest.mock
        from http.server import ThreadingHTTPServer

        from kpubdata_builder.service.http import BoundedThreadingHTTPServer, serve

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
        assert isinstance(created_servers[0], BoundedThreadingHTTPServer)

    def test_serve_passes_max_workers_to_executor(self, tmp_path: Path) -> None:
        from kpubdata_builder.service.http import BoundedThreadingHTTPServer, make_handler

        server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(_service(tmp_path)), max_workers=3
        )
        try:
            assert server._executor._max_workers == 3
        finally:
            server.server_close()

    def test_bounded_server_limits_concurrent_processing(self, tmp_path: Path) -> None:
        # 동시 처리 스레드 수가 max_workers로 상한이 걸려야 한다 (#253): 워커 수보다
        # 많은 클라이언트가 동시에 접속해도 실제 동시 처리량은 max_workers를 넘지 않는다.
        import time
        import unittest.mock

        from kpubdata_builder.service.app import ServiceResponse
        from kpubdata_builder.service.http import BoundedThreadingHTTPServer, make_handler

        max_workers = 2
        num_clients = 4
        server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(_service(tmp_path)), max_workers=max_workers
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"

        lock = threading.Lock()
        concurrent_count = 0
        max_seen = 0
        release = threading.Event()

        def _slow_dispatch(*args: object, **kwargs: object) -> ServiceResponse:
            nonlocal concurrent_count, max_seen
            with lock:
                concurrent_count += 1
                max_seen = max(max_seen, concurrent_count)
            release.wait(timeout=5.0)
            with lock:
                concurrent_count -= 1
            return ServiceResponse(200, {"service": "kpubdata-builder", "api_version": "1.0.0"})

        results: list[int] = []

        def _get() -> None:
            with urllib.request.urlopen(f"{base_url}/version", timeout=5.0) as resp:
                results.append(resp.status)

        try:
            with unittest.mock.patch(
                "kpubdata_builder.service.http.dispatch", side_effect=_slow_dispatch
            ):
                client_threads = [threading.Thread(target=_get) for _ in range(num_clients)]
                for t in client_threads:
                    t.start()

                # 워커 풀이 상한(max_workers)까지 채워질 때까지 능동적으로 대기한다.
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    with lock:
                        if concurrent_count >= max_workers:
                            break
                    time.sleep(0.02)

                with lock:
                    observed = max_seen

                release.set()
                for t in client_threads:
                    t.join(timeout=5.0)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1.0)

        assert observed == max_workers
        assert len(results) == num_clients

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


class TestArtifactFileServing:
    """아티팩트 파일 서빙 기능 테스트 (#323)."""

    def test_serves_existing_file(self, tmp_path: Path) -> None:
        from kpubdata_builder.service import FileResponse

        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        resp = service.serve_artifact_file("run1", "manifest.json")
        assert isinstance(resp, FileResponse)
        assert resp.status_code == 200
        assert resp.filename == "manifest.json"
        assert resp.file_path.exists()

    def test_returns_404_for_nonexistent_file(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        resp = service.serve_artifact_file("run1", "nonexistent.csv")
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 404
        assert "not found" in str(resp.body.get("error", ""))

    def test_returns_404_for_nonexistent_run(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        resp = service.serve_artifact_file("nope", "manifest.json")
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 404
        assert "run not found" in str(resp.body.get("error", ""))

    def test_blocks_path_traversal_with_dot_dot(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        resp = service.serve_artifact_file("run1", "../run2/manifest.json")
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 400
        assert "safe" in str(resp.body.get("error", "")).lower()

    def test_blocks_path_traversal_with_absolute_path(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        resp = service.serve_artifact_file("run1", "/etc/passwd")
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 400
        assert "safe" in str(resp.body.get("error", "")).lower()

    def test_returns_400_for_directory_not_file(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        # out 디렉터리는 빌드로 생성되므로 존재함
        (tmp_path / "run1" / "subdir").mkdir()

        resp = service.serve_artifact_file("run1", "subdir")
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 400
        assert "not a file" in str(resp.body.get("error", ""))

    def test_serve_artifact_file_route(self, tmp_path: Path) -> None:
        from kpubdata_builder.service import FileResponse

        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        resp = dispatch(service, "GET", "/artifacts/run1/manifest.json", None)
        assert isinstance(resp, FileResponse)
        assert resp.status_code == 200
        assert resp.filename == "manifest.json"

    def test_mime_type_detection(self, tmp_path: Path) -> None:
        from kpubdata_builder.service.http import _get_mime_type

        # 명시적 매핑
        assert _get_mime_type(tmp_path / "data.parquet") == "application/vnd.apache.parquet"
        assert _get_mime_type(tmp_path / "data.csv") == "text/csv"
        assert _get_mime_type(tmp_path / "data.json") == "application/json"
        assert _get_mime_type(tmp_path / "data.txt") == "text/plain"

        # mimetypes 라이브러리 (fallback)
        assert _get_mime_type(tmp_path / "data.html") == "text/html"
        assert _get_mime_type(tmp_path / "data.xml") == "application/xml"

        # 알 수 없는 확장자 → 기본값
        assert _get_mime_type(tmp_path / "data.unknown") == "application/octet-stream"
        assert _get_mime_type(tmp_path / "data") == "application/octet-stream"


class TestOwnershipEnforcement:
    """ENFORCE_OWNERSHIP 회귀: 인덱스 폴백 시에도 소유권이 강제되어야 한다 (#433).

    list_builds의 SQLite 인덱스 분기에만 소유권 필터가 있고, 파일시스템 폴백에는
    없어 ENFORCE_OWNERSHIP=true 여도 타인의 run_id가 노출되는 버그 회귀 테스트.
    ADR 0003이 폴백을 정상 동작 모드로 설계하므로 예외 상황이 아님.
    """

    def _build_as(self, service: BuilderService, run_id: str, created_by: str) -> None:
        """created_by를 명시적으로 기록하며 빌드 (테스트 단순화용 주입)."""
        dispatch(
            service,
            "POST",
            "/build",
            {"spec": VALID_SPEC_YAML, "run_id": run_id},
        )
        mpath = service._output_root / run_id / "manifest.json"
        data = json.loads(mpath.read_text(encoding="utf-8"))
        data["created_by"] = created_by
        mpath.write_text(json.dumps(data), encoding="utf-8")

    def test_fallback_filters_other_owners_when_index_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """인덱스가 비었을 때 폴백이 다른 사용자의 run을 노출하면 안 된다 (#433)."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "runA", "oidc:userA")
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        user_b = Principal(kind="oidc", identifier="userB")
        resp = service.list_builds(principal=user_b)

        assert resp.status_code == 200
        builds = cast(list[dict[str, object]], resp.body["builds"])
        run_ids = [cast(str, b["run_id"]) for b in builds]
        assert "runA" not in run_ids, (
            "폴백 경로가 다른 사용자의 run_id를 노출함 — 소유권 필터 누락 (#433)"
        )

    def test_fallback_filters_other_owners_when_index_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """인덱스 조회가 예외로 실패해도 폴백은 소유권을 강제해야 한다 (#433).

        SQLite 잠금 경합 등으로 list_builds가 예외를 던질 때, ENFORCE_OWNERSHIP+
        oidc 조합이면 타인 run이 폴백으로 새어나가면 안 됨 (fail-closed).
        """
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "runA", "oidc:userA")

        def _raise(_limit: int) -> list:
            raise RuntimeError("simulated sqlite lock contention")

        monkeypatch.setattr(service._build_index, "list_builds", _raise)

        user_b = Principal(kind="oidc", identifier="userB")
        resp = service.list_builds(principal=user_b)

        assert resp.status_code == 200
        builds = cast(list[dict[str, object]], resp.body["builds"])
        run_ids = [cast(str, b["run_id"]) for b in builds]
        assert "runA" not in run_ids

    def test_owner_sees_own_run_in_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """소유자 본인은 폴백에서도 자신의 run을 볼 수 있어야 한다 (양성 회귀)."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "runA", "oidc:userA")
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        user_a = Principal(kind="oidc", identifier="userA")
        resp = service.list_builds(principal=user_a)

        builds = cast(list[dict[str, object]], resp.body["builds"])
        run_ids = [cast(str, b["run_id"]) for b in builds]
        assert "runA" in run_ids


class _FakeCatalogRef:
    """DatasetRef 흉내 — catalog() 가 접근하는 속성만 노출."""

    def __init__(
        self, provider: str, dataset_key: str, name: str, *, service_key: bool = False
    ) -> None:
        self.provider = provider
        self.dataset_key = dataset_key
        self.name = name
        self.raw_metadata: dict[str, object] = (
            {"service_key_param": "serviceKey"} if service_key else {}
        )


class TestCatalog:
    """catalog 동적 provider 조회 (#436). ADR 0011 — 하드코딩 금지."""

    def _service_with_catalog(
        self,
        tmp_path: Path,
        refs: list[object],
        *,
        auth_provider_names: tuple[str, ...] = (),
    ) -> BuilderService:
        client = _FakeClient({}, catalog_items=refs, auth_provider_names=auth_provider_names)
        return BuilderService(output_root=tmp_path, client_factory=lambda: client)

    def test_catalog_groups_datasets_by_provider(self, tmp_path: Path) -> None:
        refs = [
            _FakeCatalogRef("datago", "air_quality", "대기오염", service_key=True),
            _FakeCatalogRef("datago", "village_fcst", "단기예보"),
            _FakeCatalogRef("bok", "base_rate", "기준금리"),
            _FakeCatalogRef("krx", "stock", "주식"),
        ]
        resp = self._service_with_catalog(tmp_path, refs, auth_provider_names=("bok",)).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        names = [cast(str, p["name"]) for p in providers]
        assert "datago" in names
        assert "bok" in names

        datago = next(p for p in providers if p["name"] == "datago")
        datago_datasets = cast(list[dict[str, object]], datago["datasets"])
        assert len(datago_datasets) == 2
        aq = next(d for d in datago_datasets if d["name"] == "air_quality")
        assert aq["requires_service_key"] is True
        vf = next(d for d in datago_datasets if d["name"] == "village_fcst")
        assert vf["requires_service_key"] is False

        bok = next(p for p in providers if p["name"] == "bok")
        bok_datasets = cast(list[dict[str, object]], bok["datasets"])
        assert bok_datasets[0]["requires_service_key"] is True
        krx = next(p for p in providers if p["name"] == "krx")
        krx_datasets = cast(list[dict[str, object]], krx["datasets"])
        assert krx_datasets[0]["requires_service_key"] is False

    def test_catalog_includes_unlisted_providers(self, tmp_path: Path) -> None:
        """하드코딩 8개에 없는 provider도 동적 조회로 떠야 한다 (#436)."""
        refs = [_FakeCatalogRef("newprovider", "new_ds", "새 데이터셋")]
        resp = self._service_with_catalog(tmp_path, refs).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        names = [cast(str, p["name"]) for p in providers]
        assert "newprovider" in names

    def test_catalog_empty_when_no_datasets(self, tmp_path: Path) -> None:
        resp = self._service_with_catalog(tmp_path, []).catalog()
        assert resp.status_code == 200
        assert resp.body["providers"] == []

    def test_catalog_closes_request_client(self, tmp_path: Path) -> None:
        client = _CloseTrackingClient(
            {}, catalog_items=[_FakeCatalogRef("datago", "air_quality", "대기오염")]
        )
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = service.catalog()

        assert resp.status_code == 200
        assert client.close_calls == 1

    def test_catalog_closes_request_client_when_catalog_fails(self, tmp_path: Path) -> None:
        class _BrokenCatalogClient:
            close_calls = 0

            @property
            def datasets(self) -> object:
                raise RuntimeError("catalog failed")

            def dataset(self, source_key: str) -> _FakeDataset:
                raise KeyError(source_key)

            def close(self) -> None:
                self.close_calls += 1

        client = _BrokenCatalogClient()
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = service.catalog()

        assert resp.status_code == 502
        assert client.close_calls == 1

    def test_catalog_returns_502_when_client_raises(self, tmp_path: Path) -> None:
        class _BrokenClient:
            @property
            def datasets(self) -> object:
                raise RuntimeError("client init failed")

        service = BuilderService(output_root=tmp_path, client_factory=lambda: _BrokenClient())
        resp = service.catalog()
        assert resp.status_code == 502


class TestRunIdRouteValidation:
    """/artifacts/{run_id} 라우트가 run_id를 소유권 검사보다 먼저 검증 (#439).

    _read_manifest_created_by 가 URL에서 온 run_id로 검증 없이 경로를 조립하므로,
    "../" 등 unsafe 세그먼트가 _check_ownership 보다 먼저 validate_path_segment
    에 도달해야 한다.
    """

    def test_unsafe_run_id_returns_400_before_ownership(self, tmp_path: Path) -> None:
        """unsafe run_id(``..``)는 _check_ownership 전에 400 (#439)."""
        resp = dispatch(_service(tmp_path), "GET", "/artifacts/../bad", None)
        assert resp.status_code == 400
        err = str(resp.body.get("error", "")).lower()
        assert "run_id" in err or "safe" in err

    def test_blank_run_id_returns_400(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/artifacts/", None)
        assert resp.status_code == 400
        assert "run_id" in str(resp.body.get("error", "")).lower()

    def test_safe_run_id_still_reaches_ownership_check(self, tmp_path: Path) -> None:
        """safe run_id는 validate 통과 후 artifacts(또는 소유권 검사)로 (#439 양성)."""
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1")
        # ENFORCE_OWNERSHIP off(기본) → 200
        resp = dispatch(service, "GET", "/artifacts/run1", None)
        assert resp.status_code == 200


class TestBuildSpecSnapshot:
    """GET /builds/{run_id}/spec의 조회·보안·legacy 정책 (#487)."""

    def test_owner_reads_snapshot_and_index_digest_matches(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        build = dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "spec-run"})
        assert build.status_code == 200

        response = dispatch(service, "GET", "/builds/spec-run/spec", None)

        assert response.status_code == 200
        snapshot = (tmp_path / "spec-run" / "buildspec.yaml").read_bytes()
        expected = f"sha256:{hashlib.sha256(snapshot).hexdigest()}"
        assert response.body == {
            "run_id": "spec-run",
            "spec": snapshot.decode("utf-8"),
            "spec_digest": expected,
        }
        entry = service._build_index.get("spec-run")
        assert entry is not None
        assert entry.spec_digest == expected

    def test_unknown_and_legacy_run_return_404(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert dispatch(service, "GET", "/builds/unknown/spec", None).status_code == 404

        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "manifest.json").write_text("{}", encoding="utf-8")
        response = dispatch(service, "GET", "/builds/legacy/spec", None)
        assert response.status_code == 404
        assert "unavailable" in str(response.body["error"])

    @pytest.mark.parametrize("run_id", ["..", "../escape", "bad%2Fsegment"])
    def test_invalid_run_id_returns_400(self, tmp_path: Path, run_id: str) -> None:
        response = dispatch(_service(tmp_path), "GET", f"/builds/{run_id}/spec", None)
        assert response.status_code == 400

    def test_another_owner_receives_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        build = dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "owned"})
        assert build.status_code == 200

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="b")
        )
        response = dispatch(service, "GET", "/builds/owned/spec", None)
        assert response.status_code == 403

        # ownership 거부 시 snapshot reader까지 도달하지 않는다.
        monkeypatch.setattr(Path, "read_bytes", lambda _path: pytest.fail("snapshot read leaked"))
        response = dispatch(service, "GET", "/builds/owned/spec", None)
        assert response.status_code == 403

    def test_unknown_run_returns_404_before_ownership(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )

        response = dispatch(_service(tmp_path), "GET", "/builds/unknown/spec", None)

        assert response.status_code == 404
