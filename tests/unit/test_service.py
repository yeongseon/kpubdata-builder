"""HTTP 서비스 façade(#36): validate/preview/build/artifacts 로직과 라우팅 검증."""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from http.server import HTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import kpubdata_builder.service.app as app_module
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.http import _clear_cors_cache, make_handler
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.spec import JsonValue

from ._openapi import response_schema, validate

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
    return BuilderService(output_root=tmp_path, client_factory=lambda **_kwargs: client)


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

    def test_returns_source_sample_and_diff_fields(self, tmp_path: Path) -> None:
        # #497: 기존 필드(sample/total_rows/statistics)와 함께 신규 필드가 실린다.
        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=2)
        assert resp.status_code == 200
        preview = resp.body["previews"][0]
        assert preview["sample_mode"] == "first"
        assert preview["diff_available"] is True
        assert isinstance(preview["source_sample"], list)
        assert isinstance(preview["diffs"], list)
        assert preview["transform_summary"] == {"changed_cells": 0, "changed_rows": 0}
        assert preview["diff_truncated"] is False

    def test_random_sample_mode_is_reproducible_through_service(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air_quality": [{"id": str(i)} for i in range(50)]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        first = service.preview(VALID_SPEC_YAML, limit=5, sample_mode="random", seed=3)
        second = service.preview(VALID_SPEC_YAML, limit=5, sample_mode="random", seed=3)

        assert first.status_code == second.status_code == 200
        assert (
            first.body["previews"][0]["source_sample"]
            == second.body["previews"][0]["source_sample"]
        )
        assert first.body["previews"][0]["sample_mode"] == "random"

    def test_wide_dataset_diffs_are_truncated_over_the_wire(self, tmp_path: Path) -> None:
        # #497 sample/diff memory 상한: limit(행 수)만으로는 wide dataset의 diff
        # item 개수를 막지 못하므로, 실제 서비스 응답에서도 diffs가 상한을 지키고
        # diff_truncated=true를 실어 클라이언트가 전체 diff로 오인하지 않게 한다.
        from kpubdata_builder.pipeline import MAX_PREVIEW_DIFF_ITEMS

        column_count = MAX_PREVIEW_DIFF_ITEMS + 50
        columns = [f"c{i}" for i in range(column_count)]
        spec_yaml = (
            "dataset_id: dataset.wide\n"
            "title: Wide Dataset\n"
            "description: many columns\n"
            "sources:\n"
            "  - provider: datago\n"
            "    dataset: air_quality\n"
            "    schema:\n"
            "      casts:\n" + "".join(f"        {c}: int\n" for c in columns) + "exports:\n"
            "  - kind: jsonl\n"
            "    output_path: out/data.jsonl\n"
        )
        client = _FakeClient({"datago.air_quality": [dict.fromkeys(columns, "1")]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = service.preview(spec_yaml, limit=1)

        assert resp.status_code == 200
        preview = resp.body["previews"][0]
        assert preview["diff_available"] is True
        assert len(preview["diffs"]) == MAX_PREVIEW_DIFF_ITEMS
        assert preview["diff_truncated"] is True
        assert preview["transform_summary"]["changed_cells"] == column_count


class TestPreviewLimitGuard:
    def test_preview_direct_call_rejects_zero_limit(self, tmp_path: Path) -> None:
        # #225: BuilderService.preview()를 직접 호출할 때도 limit<1이면 400을 반환한다.
        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=0)
        assert resp.status_code == 400
        assert "limit" in str(resp.body.get("error", ""))

    def test_preview_direct_call_rejects_negative_limit(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=-5)
        assert resp.status_code == 400

    def test_preview_direct_call_rejects_limit_above_max(self, tmp_path: Path) -> None:
        # #497: limit 상한(1000) 신규 도입 — 이전엔 상한이 없었다(behavioral tightening).
        from kpubdata_builder.service.app import MAX_PREVIEW_LIMIT

        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=MAX_PREVIEW_LIMIT + 1)
        assert resp.status_code == 400
        assert "limit" in str(resp.body.get("error", ""))

    def test_preview_direct_call_accepts_limit_at_max(self, tmp_path: Path) -> None:
        from kpubdata_builder.service.app import MAX_PREVIEW_LIMIT

        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=MAX_PREVIEW_LIMIT)
        assert resp.status_code == 200

    def test_preview_direct_call_rejects_invalid_sample_mode(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).preview(VALID_SPEC_YAML, sample_mode="shuffle")
        assert resp.status_code == 400
        assert "sample_mode" in str(resp.body.get("error", ""))

    def test_preview_direct_call_rejects_non_int_seed(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).preview(
            VALID_SPEC_YAML, sample_mode="random", seed=cast(int, "7")
        )
        assert resp.status_code == 400
        assert "seed" in str(resp.body.get("error", ""))

    def test_preview_direct_call_rejects_bool_seed(self, tmp_path: Path) -> None:
        # bool은 int의 하위 타입이지만 seed 의미가 없으므로 거부한다.
        resp = _service(tmp_path).preview(
            VALID_SPEC_YAML, sample_mode="random", seed=cast(int, True)
        )
        assert resp.status_code == 400
        assert "seed" in str(resp.body.get("error", ""))


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

    def test_manifest_route_strips_persisted_internal_owner_id(self, tmp_path: Path) -> None:
        """owner_id는 persisted manifest에 남지만 HTTP wire에는 노출하지 않는다 (#505)."""
        service = _service(tmp_path)
        service.build(
            VALID_SPEC_YAML,
            run_id="run1",
            created_by="oidc:abcdef12",
            owner_id="oidc:canonical-owner",
        )

        persisted = json.loads((tmp_path / "run1" / "manifest.json").read_text(encoding="utf-8"))
        assert persisted["owner_id"] == "oidc:canonical-owner"

        resp = dispatch(service, "GET", "/builds/run1/manifest", None)

        assert resp.status_code == 200
        assert "owner_id" not in resp.body
        assert resp.body["created_by"] == "oidc:abcdef12"

    def test_manifest_route_returns_404_for_missing_run(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/builds/nope/manifest", None)

        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 404


def _file_source_spec_yaml(upload_id: str) -> str:
    return (
        f"""
dataset_id: dataset.uploaded
title: Uploaded Trades
description: file source build (#498)
sources:
  - kind: file
    upload_id: {upload_id}
    format: csv
    encoding: utf-8
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
        + "\n"
    )


class TestUploads:
    """kind="file" source(#498) — POST /uploads가 owner_id로 격리한 업로드를
    BuildSpec이 참조해 build/preview까지 이어지는 end-to-end 흐름을 검증한다."""

    def test_create_upload_then_build_end_to_end(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        principal = Principal(kind="oidc", identifier="u1", owner_id="oidc:owner-1")

        created = service.create_upload(
            b"id,amount\n1,1000\n2,2500\n",
            format="csv",
            encoding="utf-8",
            original_filename="trades.csv",
            principal=principal,
        )
        assert created.status_code == 200
        upload_id = created.body["upload_id"]
        assert isinstance(upload_id, str)

        result = service.build(
            _file_source_spec_yaml(upload_id),
            run_id="upload-run",
            owner_id=principal.owner_id,
            principal=principal,
        )

        assert result.status_code == 200
        assert result.body["status"] == "ok"

    def test_build_rejects_upload_owned_by_another_principal(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        owner = Principal(kind="oidc", identifier="u1", owner_id="oidc:owner-1")
        other = Principal(kind="oidc", identifier="u2", owner_id="oidc:owner-2")

        created = service.create_upload(
            b"id\n1\n", format="csv", encoding="utf-8", original_filename=None, principal=owner
        )
        upload_id = created.body["upload_id"]
        assert isinstance(upload_id, str)

        result = service.build(
            _file_source_spec_yaml(upload_id),
            run_id="run-other-owner",
            owner_id=other.owner_id,
            principal=other,
        )

        assert result.status_code == 502
        outcomes = cast(list[dict[str, JsonValue]], result.body["outcomes"])
        assert outcomes[0]["status"] == "failed"
        assert "not found" in cast(str, outcomes[0]["error"])

    def test_get_and_delete_upload_round_trip(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        principal = Principal(kind="oidc", identifier="u1", owner_id="oidc:owner-1")
        created = service.create_upload(
            b"id\n1\n", format="csv", encoding="utf-8", original_filename=None, principal=principal
        )
        upload_id = created.body["upload_id"]
        assert isinstance(upload_id, str)

        fetched = service.get_upload(upload_id, principal=principal)
        assert fetched.status_code == 200
        assert fetched.body["upload_id"] == upload_id
        assert "content" not in fetched.body

        deleted = service.delete_upload(upload_id, principal=principal)
        assert deleted.status_code == 200
        assert deleted.body == {"upload_id": upload_id, "deleted": True}

        missing = service.get_upload(upload_id, principal=principal)
        assert missing.status_code == 404

    def test_get_upload_hides_existence_from_other_principal(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        owner = Principal(kind="oidc", identifier="u1", owner_id="oidc:owner-1")
        other = Principal(kind="oidc", identifier="u2", owner_id="oidc:owner-2")
        created = service.create_upload(
            b"id\n1\n", format="csv", encoding="utf-8", original_filename=None, principal=owner
        )
        upload_id = created.body["upload_id"]
        assert isinstance(upload_id, str)

        resp = service.get_upload(upload_id, principal=other)

        assert resp.status_code == 404

    def test_create_upload_rejects_corrupt_content(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        principal = Principal(kind="oidc", identifier="u1", owner_id="oidc:owner-1")

        resp = service.create_upload(
            b"not json",
            format="json",
            encoding="utf-8",
            original_filename=None,
            principal=principal,
        )

        assert resp.status_code == 400

    def test_create_upload_rejects_unsupported_format(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        principal = Principal(kind="oidc", identifier="u1", owner_id="oidc:owner-1")

        resp = service.create_upload(
            b"data", format="xlsx", encoding="utf-8", original_filename=None, principal=principal
        )

        assert resp.status_code == 400

    def test_preview_without_file_source_never_touches_upload_store(self, tmp_path: Path) -> None:
        """file source가 없는 preview는 uploads.sqlite3를 만들지 않는다(지연 생성)."""
        _service(tmp_path).preview(VALID_SPEC_YAML)

        assert not (tmp_path / ".service" / "uploads.sqlite3").exists()


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

    def test_preview_rejects_limit_above_max(self, tmp_path: Path) -> None:
        # #497: 신규 상한(1000) 도입 — 이전 client가 그 이상을 보내던 관행은 깨진다
        # (behavioral tightening, 이전엔 상한이 없었다).
        from kpubdata_builder.service.app import MAX_PREVIEW_LIMIT

        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "limit": MAX_PREVIEW_LIMIT + 1},
        )
        assert resp.status_code == 400
        assert "limit" in str(resp.body.get("error", ""))

    def test_preview_accepts_limit_at_max(self, tmp_path: Path) -> None:
        from kpubdata_builder.service.app import MAX_PREVIEW_LIMIT

        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "limit": MAX_PREVIEW_LIMIT},
        )
        assert resp.status_code == 200

    def test_preview_rejects_invalid_sample_mode(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "sample_mode": "shuffle"},
        )
        assert resp.status_code == 400
        assert "sample_mode" in str(resp.body.get("error", ""))

    def test_preview_rejects_non_string_sample_mode(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "sample_mode": 1},
        )
        assert resp.status_code == 400
        assert "sample_mode" in str(resp.body.get("error", ""))

    def test_preview_rejects_non_int_seed(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "sample_mode": "random", "seed": "7"},
        )
        assert resp.status_code == 400
        assert "seed" in str(resp.body.get("error", ""))

    def test_preview_rejects_bool_seed(self, tmp_path: Path) -> None:
        # bool은 int의 하위 타입이지만 seed로는 거부한다.
        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "sample_mode": "random", "seed": True},
        )
        assert resp.status_code == 400
        assert "seed" in str(resp.body.get("error", ""))

    def test_preview_dispatch_passes_sample_mode_and_seed_through(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air_quality": [{"id": str(i)} for i in range(20)]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = dispatch(
            service,
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "limit": 3, "sample_mode": "random", "seed": 5},
        )

        assert resp.status_code == 200
        assert resp.body["previews"][0]["sample_mode"] == "random"
        assert len(resp.body["previews"][0]["source_sample"]) == 3

    def test_preview_dispatch_defaults_sample_mode_to_first_when_omitted(
        self, tmp_path: Path
    ) -> None:
        # 기존 client가 sample_mode/seed 없이 호출해도 기존과 동일하게 동작한다.
        resp = dispatch(
            _service(tmp_path), "POST", "/preview", {"spec": VALID_SPEC_YAML, "limit": 1}
        )
        assert resp.status_code == 200
        assert resp.body["previews"][0]["sample_mode"] == "first"

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


class TestPreviewWireSerialization:
    """POST /preview 실제 wire JSON 직렬화를 검증한다 (#497, 계약 섹션 6).

    ``BuilderService.preview()``가 반환하는 dict는 date/datetime 등 파이썬
    객체를 그대로 담고 있을 수 있어(#440부터의 기존 ``sample`` 필드와 동일한
    패턴), 실제 HTTP 응답 바이트까지 확인해야 ``service/http.py``의
    ``json.dumps(default=str)`` 경로가 ``source_sample``/``diffs``에도 올바르게
    적용되는지 알 수 있다. 별도 serializer를 새로 만들지 않고 기존 경로를
    그대로 재사용한다.
    """

    def _post_preview(
        self, service: BuilderService, spec_yaml: str, **body_extra: object
    ) -> dict[str, object]:
        server = HTTPServer(("127.0.0.1", 0), make_handler(service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
        try:
            req = urllib.request.Request(
                f"{base_url}/preview",
                data=json.dumps({"spec": spec_yaml, **body_extra}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2.0) as response:
                return cast(dict[str, object], json.loads(response.read()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_null_int_float_bool_string_date_and_datetime_survive_the_wire(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        # casts를 선언하지 않고 raw record에 이미 파이썬 타입 값을 실어, kpubdata
        # provider가 이미 typed 값을 돌려주는 흔한 경우를 흉내낸다 — records_to_dataframe가
        # 이 타입들을 그대로 추론해 실어 나른다.
        row: dict[str, JsonValue] = {
            "id": "1",
            "n": None,
            "count": 3,
            "ratio": 1.5,
            "active": True,
            "label": "seoul",
            "d": date(2025, 1, 1),  # type: ignore[dict-item]
            "ts": datetime(2025, 1, 1, 12, 30, 0),  # type: ignore[dict-item]
        }
        client = _FakeClient({"datago.air_quality": [row]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        body = self._post_preview(service, VALID_SPEC_YAML, limit=1)

        preview = cast(dict[str, object], cast(list[object], body["previews"])[0])
        source_row = cast(dict[str, object], cast(list[object], preview["source_sample"])[0])
        assert source_row["n"] is None
        assert source_row["count"] == 3
        assert source_row["ratio"] == 1.5
        assert source_row["active"] is True
        assert source_row["label"] == "seoul"
        # date/naive datetime은 http.py의 json.dumps(default=str)을 거쳐 str()
        # 형식(공백 구분)의 문자열이 된다 — 기존 sample 필드와 동일 규칙(#497은 이를
        # 재사용할 뿐 새 규칙을 만들지 않는다).
        assert source_row["d"] == "2025-01-01"
        assert source_row["ts"] == "2025-01-01 12:30:00"

        transformed_row = cast(dict[str, object], cast(list[object], preview["sample"])[0])
        assert transformed_row["d"] == "2025-01-01"
        assert transformed_row["ts"] == "2025-01-01 12:30:00"

    def test_timezone_aware_datetime_survives_the_wire(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # test_silver.py::test_serializes_timezone_aware_datetime_values_as_iso_strings와
        # 같은 패턴(직접 aware datetime 값을 실어 polars가 UTC로 정규화하게 한다).
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        kst = timezone(timedelta(hours=9))
        row: dict[str, JsonValue] = {
            "id": "1",
            "tz": datetime(2025, 1, 1, 21, 30, 0, tzinfo=kst),  # type: ignore[dict-item]
        }
        client = _FakeClient({"datago.air_quality": [row]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        body = self._post_preview(service, VALID_SPEC_YAML, limit=1)

        preview = cast(dict[str, object], cast(list[object], body["previews"])[0])
        source_row = cast(dict[str, object], cast(list[object], preview["source_sample"])[0])
        transformed_row = cast(dict[str, object], cast(list[object], preview["sample"])[0])
        # source_sample은 bronze raw record를 그대로 노출하므로 원래 KST offset을
        # 유지하고, Silver(transformed)는 polars가 UTC로 정규화한다 — KST 21:30과
        # UTC 12:30은 같은 instant이므로 diff는 없지만(값 자체는 동일), 두 표현이
        # 서로 다른 offset의 str() 문자열로 각자 정확히 직렬화되는지 확인한다.
        assert source_row["tz"] == "2025-01-01 21:30:00+09:00"
        assert transformed_row["tz"] == "2025-01-01 12:30:00+00:00"

    def test_diff_before_after_carry_wire_correct_types(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # declared cast로 실제 diff item이 만들어질 때도 before(str)/after(int)가
        # 각자의 실제 JSON 타입으로 wire에 실린다(#497 diff item 예시와 동일한 형태).
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        spec_yaml = (
            """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
    schema:
      casts:
        v: int
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
            + "\n"
        )
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": "128000"}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        body = self._post_preview(service, spec_yaml, limit=1)

        preview = cast(dict[str, object], cast(list[object], body["previews"])[0])
        diffs = cast(list[dict[str, object]], preview["diffs"])
        assert len(diffs) == 1
        assert diffs[0]["before"] == "128000"
        assert diffs[0]["after"] == 128000
        assert diffs[0]["transform"] == "cast:int"


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
            assert response.headers["Access-Control-Allow-Methods"] == (
                "GET, POST, PUT, DELETE, OPTIONS"
            )
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
            request_id = response.headers["X-Request-ID"]
            body = cast(dict[str, object], json.loads(response.read()))
        assert body["status"] == "ok"
        assert "request_id" not in body
        assert request_id
        # 버전·서비스 메타 정보가 누출되지 않아야 한다.
        assert "api_version" not in body
        assert "service" not in body

    @pytest.mark.parametrize("stage", ["bronze", "silver", "gold"])
    def test_stage_detail_wire_response_conforms_to_openapi(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        stage: str,
    ) -> None:
        base_url, _, _ = http_server
        build_req = urllib.request.Request(
            f"{base_url}/build",
            data=json.dumps({"spec": VALID_SPEC_YAML, "run_id": "wire-stage-detail"}).encode(
                "utf-8"
            ),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(build_req, timeout=5.0) as response:
            assert response.status == 200

        url = f"{base_url}/builds/wire-stage-detail/stages/{stage}?source=datago.air_quality"
        with urllib.request.urlopen(url, timeout=2.0) as response:
            assert response.status == 200
            status_code = response.status
            request_id = response.headers["X-Request-ID"]
            body = cast(dict[str, object], json.loads(response.read()))

        assert "request_id" not in body
        assert request_id
        contract_path = Path(__file__).parents[2] / "contract" / "builder-api.yaml"
        contract = cast(dict[str, Any], yaml.safe_load(contract_path.read_text(encoding="utf-8")))
        schema = response_schema(contract, "/builds/{run_id}/stages/{stage}", "GET", status_code)
        assert schema is not None
        assert validate(body, schema, contract) == []


class TestHttpUploads:
    """POST /uploads(#498)의 실제 소켓 왕복 — binary body 전송·query 파싱·상한."""

    def test_create_get_delete_upload_round_trip_over_http(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/uploads?format=csv&filename=trades.csv",
            data=b"id,amount\n1,1000\n",
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200
            created = cast(dict[str, object], json.loads(response.read()))
        upload_id = created["upload_id"]
        assert isinstance(upload_id, str) and upload_id.startswith("upl_")
        assert created["original_filename"] == "trades.csv"

        with urllib.request.urlopen(f"{base_url}/uploads/{upload_id}", timeout=2.0) as response:
            assert response.status == 200
            fetched = cast(dict[str, object], json.loads(response.read()))
        assert fetched["upload_id"] == upload_id
        assert "content" not in fetched

        delete_req = urllib.request.Request(f"{base_url}/uploads/{upload_id}", method="DELETE")
        with urllib.request.urlopen(delete_req, timeout=2.0) as response:
            assert response.status == 200
            deleted = cast(dict[str, object], json.loads(response.read()))
        assert deleted["upload_id"] == upload_id
        assert deleted["deleted"] is True

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/uploads/{upload_id}", timeout=2.0)
        assert exc_info.value.code == 404

    def test_create_upload_over_configured_limit_returns_413(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_MAX_UPLOAD_BYTES", "10")
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/uploads?format=csv",
            data=b"a,b\n" * 10,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2.0)
        assert exc_info.value.code == 413

    def test_create_upload_missing_format_returns_400(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/uploads",
            data=b"id,amount\n1,1000\n",
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2.0)
        assert exc_info.value.code == 400

    def test_create_upload_body_is_not_parsed_as_json(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # POST /uploads의 body는 CSV/바이너리도 그대로 허용된다 — 다른 endpoint와
        # 달리 JSON 파싱을 시도하지 않는다(#498). 이 body는 유효한 JSON이 아니지만
        # (raw text) 유효한 CSV이므로 format=csv로 성공해야 한다.
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/uploads?format=csv",
            data=b"a,b\n1,2\n",
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200


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

    def test_serves_nested_relative_path(self, tmp_path: Path) -> None:
        """GET /artifacts/{run_id}가 돌려주는 run 디렉터리 기준 상대 경로(슬래시 포함)를
        serve_artifact_file도 그대로 받아야 한다 (#323 후속). 이전에는 file_path 전체를
        한 세그먼트로 검증해 'silver/air/table.parquet' 같은 값이 전부 400이었다."""
        from kpubdata_builder.service import FileResponse

        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        nested = tmp_path / "run1" / "silver" / "air" / "table.parquet"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_bytes(b"PAR1nested")

        listed = service.artifacts("run1")
        assert isinstance(listed, ServiceResponse)
        # wire 목록은 항상 POSIX "/" 기반이어야 한다 (OS 구분자·output_root prefix 없음).
        for wire_path in listed.body["files"]:
            assert "\\" not in wire_path
            assert not wire_path.startswith("/")
            assert str(tmp_path) not in wire_path
        assert "silver/air/table.parquet" in set(listed.body["files"])

        resp = service.serve_artifact_file("run1", "silver/air/table.parquet")
        assert isinstance(resp, FileResponse)
        assert resp.status_code == 200
        assert resp.filename == "table.parquet"
        assert resp.file_path.read_bytes() == b"PAR1nested"

        route_resp = dispatch(service, "GET", "/artifacts/run1/silver/air/table.parquet", None)
        assert isinstance(route_resp, FileResponse)
        assert route_resp.status_code == 200

    def test_blocks_backslash_in_nested_path(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        resp = service.serve_artifact_file("run1", "silver\\..\\..\\secret.txt")
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 400

    def test_blocks_percent_encoded_traversal(self, tmp_path: Path) -> None:
        """percent-encode된 트래버설/구분자는 decode 후 다시 검증되어 차단된다."""
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run2"})

        for encoded in (
            "%2e%2e/run2/manifest.json",  # ../run2/manifest.json
            "silver%2f..%2f..%2fmanifest.json",  # silver/../../manifest.json
            "silver%5c..%5c..%5cmanifest.json",  # silver\..\..\manifest.json
            "%252e%252e/run2/manifest.json",  # double-encoded ..
            "..%2f..%2fetc%2fpasswd",
        ):
            resp = service.serve_artifact_file("run1", encoded)
            assert isinstance(resp, ServiceResponse), encoded
            assert resp.status_code == 400, encoded

    def test_blocks_cross_run_and_double_slash(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run2"})

        # 다른 run 파일: '..'로만 접근 가능하므로 차단된다.
        assert service.serve_artifact_file("run1", "../run2/manifest.json").status_code == 400
        # double slash -> 빈 성분
        assert service.serve_artifact_file("run1", "silver//table.parquet").status_code == 400

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
        self._build_with_manifest_fields(service, run_id, created_by=created_by)

    def _build_with_manifest_fields(
        self, service: BuilderService, run_id: str, **fields: object
    ) -> None:
        """빌드 후 manifest.json의 임의 필드(created_by/owner_id 등)를 덮어쓴다 (#505).

        owner_id를 None으로 명시하면 manifest에서 해당 키를 완전히 제거해
        legacy(#505 이전) manifest shape를 흉내낸다.
        """
        dispatch(
            service,
            "POST",
            "/build",
            {"spec": VALID_SPEC_YAML, "run_id": run_id},
        )
        mpath = service._output_root / run_id / "manifest.json"
        data = json.loads(mpath.read_text(encoding="utf-8"))
        for key, value in fields.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
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


def _build_run_ids(resp: ServiceResponse) -> list[str]:
    builds = cast(list[dict[str, object]], resp.body["builds"])
    return [cast(str, b["run_id"]) for b in builds]


class TestStableOwnerIdOwnership:
    """canonical owner_id 기반 ownership 판정 (#505) — /builds 목록·상세 경로."""

    def _build_with_manifest_fields(
        self, service: BuilderService, run_id: str, **fields: object
    ) -> None:
        dispatch(
            service,
            "POST",
            "/build",
            {"spec": VALID_SPEC_YAML, "run_id": run_id},
        )
        mpath = service._output_root / run_id / "manifest.json"
        data = json.loads(mpath.read_text(encoding="utf-8"))
        for key, value in fields.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        mpath.write_text(json.dumps(data), encoding="utf-8")

    def test_owner_id_match_wins_even_with_different_display_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """owner_id가 일치하면 표시용 label(created_by)이 달라도 소유자로 인정된다 (#505).

        display identity가 바뀌어도(향후 프로필 이름 갱신 등) persistent owner
        identity는 바뀌지 않아야 한다는 완료 조건을 응답 목록 수준에서 검증한다.
        """
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_with_manifest_fields(
            service, "runA", created_by="oidc:old-display-name", owner_id="oidc:canonical-abc"
        )
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        # label은 manifest의 created_by와 다르지만 owner_id는 동일 — 여전히 소유자.
        renamed_principal = Principal(
            kind="oidc", identifier="new-display-name", owner_id="oidc:canonical-abc"
        )
        resp = service.list_builds(principal=renamed_principal)
        run_ids = _build_run_ids(resp)
        assert "runA" in run_ids

    def test_owner_id_mismatch_denied_even_with_matching_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """owner_id가 있는 신규 레코드는 label이 같아도 owner_id 불일치면 거부한다 (#505).

        legacy 트렁케이션(sub 앞 8자) 충돌로 label만 우연히 같아지는 상황에서도
        canonical owner_id가 우선하여 ownership이 섞이지 않는다.
        """
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_with_manifest_fields(
            service, "runA", created_by="oidc:userA", owner_id="oidc:canonical-real-owner"
        )
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        # label(identifier)은 "userA"로 원래 소유자와 같지만 owner_id는 다르다.
        impostor = Principal(kind="oidc", identifier="userA", owner_id="oidc:different-owner")
        resp = service.list_builds(principal=impostor)
        run_ids = _build_run_ids(resp)
        assert "runA" not in run_ids

    def test_legacy_run_without_owner_id_falls_back_to_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """owner_id가 없는(#505 이전) run은 created_by/label 비교로 계속 접근 가능해야 한다."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_with_manifest_fields(service, "runA", created_by="oidc:userA", owner_id=None)
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        # owner_id 없이(예: 기존 구성) 인증된 principal도 label로 자신의 legacy run에 접근.
        user_a = Principal(kind="oidc", identifier="userA")
        resp = service.list_builds(principal=user_a)
        run_ids = _build_run_ids(resp)
        assert "runA" in run_ids

    def test_ambiguous_record_with_no_owner_info_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """owner_id도 created_by도 없는 레코드는 "누구나 접근 가능"으로 취급하지 않는다.

        "owner field가 없으니 누구나 접근 가능" 폴백은 금지한다는 요구사항의 회귀 테스트.
        """
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_with_manifest_fields(service, "runA", created_by=None, owner_id=None)
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        any_user = Principal(kind="oidc", identifier="userA", owner_id="oidc:canonical-abc")
        resp = service.list_builds(principal=any_user)
        run_ids = _build_run_ids(resp)
        assert "runA" not in run_ids

    def test_builds_response_does_not_leak_owner_id_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """owner_id는 ownership 판정 내부용일 뿐 /builds wire 응답에 노출되면 안 된다 (#505)."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_with_manifest_fields(
            service, "runA", created_by="oidc:userA", owner_id="oidc:canonical-abc"
        )
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        user_a = Principal(kind="oidc", identifier="userA", owner_id="oidc:canonical-abc")
        resp = service.list_builds(principal=user_a)
        builds = cast(list[dict[str, object]], resp.body["builds"])
        assert builds
        assert "owner_id" not in builds[0]


class _FakeCatalogRef:
    """DatasetRef 흉내 — catalog() 가 접근하는 속성만 노출.

    기본값은 실제 DatasetRef의 기본값과 같다(metadata 없는 dataset — #490
    null/empty 직렬화 규칙 검증에 그대로 쓴다).
    """

    def __init__(
        self,
        provider: str,
        dataset_key: str,
        name: str,
        *,
        service_key: bool = False,
        description: str | None = None,
        tags: tuple[str, ...] = (),
        source_url: str | None = None,
        representation: object = None,
        operations: frozenset[object] = frozenset(),
        query_support: object = None,
        raw_metadata_extra: dict[str, object] | None = None,
    ) -> None:
        from kpubdata.core.models import Representation

        self.provider = provider
        self.dataset_key = dataset_key
        self.name = name
        self.description = description
        self.tags = tags
        self.source_url = source_url
        self.representation = representation or Representation.API_JSON
        self.operations = operations
        self.query_support = query_support
        self.raw_metadata: dict[str, object] = (
            {"service_key_param": "serviceKey"} if service_key else {}
        )
        if raw_metadata_extra:
            self.raw_metadata.update(raw_metadata_extra)


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

    def _service_with_lazy_provider_catalog(
        self,
        tmp_path: Path,
        *,
        missing_provider: str | None = None,
        missing_module: str = "pandas",
    ) -> BuilderService:
        refs = {
            "datago": [_FakeCatalogRef("datago", "air_quality", "대기오염")],
            "krx": [_FakeCatalogRef("krx", "stock", "주식")],
        }

        class Registry:
            def __iter__(self) -> Iterable[str]:
                return iter(("datago", "krx"))

            def get(self, name: str) -> object:
                if name == missing_provider:
                    raise ModuleNotFoundError(
                        f"No module named '{missing_module}'", name=missing_module
                    )
                return type("Adapter", (), {"requires_api_key": name == "datago"})()

        class Catalog:
            @staticmethod
            def list(*, provider: str) -> list[object]:
                return refs[provider]

        class Client:
            _registry = Registry()
            datasets = Catalog()

            def close(self) -> None:
                return None

        return BuilderService(output_root=tmp_path, client_factory=Client)

    def test_catalog_skips_only_krx_when_optional_pandas_is_missing(self, tmp_path: Path) -> None:
        response = self._service_with_lazy_provider_catalog(
            tmp_path, missing_provider="krx"
        ).catalog()

        assert response.status_code == 200
        providers = cast(list[dict[str, object]], response.body["providers"])
        assert [provider["name"] for provider in providers] == ["datago"]

    def test_catalog_does_not_hide_other_provider_missing_module(self, tmp_path: Path) -> None:
        response = self._service_with_lazy_provider_catalog(
            tmp_path, missing_provider="datago", missing_module="internal_datago"
        ).catalog()

        assert response.status_code == 502
        assert "internal_datago" in cast(str, response.body["error"])

    def test_catalog_keeps_krx_when_optional_dependency_is_available(self, tmp_path: Path) -> None:
        response = self._service_with_lazy_provider_catalog(tmp_path).catalog()

        assert response.status_code == 200
        providers = cast(list[dict[str, object]], response.body["providers"])
        assert [provider["name"] for provider in providers] == ["datago", "krx"]

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

    def test_catalog_serializes_discovery_metadata(self, tmp_path: Path) -> None:
        """DatasetRef의 탐색용 metadata가 allowlist로 직렬화된다 (#490)."""
        from kpubdata.core.capability import PaginationMode, QuerySupport
        from kpubdata.core.models import Operation, Representation

        ref = _FakeCatalogRef(
            "datago",
            "air_quality",
            "대기오염",
            description="측정소별 대기오염 물질 농도",
            tags=("environment", "air"),
            source_url="https://www.data.go.kr/data/15073861/openapi",
            representation=Representation.API_JSON,
            operations=frozenset({Operation.GET, Operation.LIST}),
            query_support=QuerySupport(
                pagination=PaginationMode.OFFSET,
                filterable_fields=frozenset({"station_name"}),
                sortable_fields=frozenset(),
                time_range=True,
                max_page_size=1000,
            ),
        )
        resp = self._service_with_catalog(tmp_path, [ref]).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        dataset = cast(dict[str, object], providers[0]["datasets"][0])
        assert dataset["description"] == "측정소별 대기오염 물질 농도"
        assert dataset["tags"] == ["air", "environment"]
        assert dataset["source_url"] == "https://www.data.go.kr/data/15073861/openapi"
        assert dataset["representation"] == "api_json"
        assert dataset["operations"] == ["get", "list"]
        query_support = cast(dict[str, object], dataset["query_support"])
        assert query_support["pagination"] == "offset"
        assert query_support["filterable_fields"] == ["station_name"]
        assert query_support["sortable_fields"] == []
        assert query_support["time_range"] is True
        assert query_support["max_page_size"] == 1000

    def test_catalog_metadata_less_dataset_serializes_null_and_empty(self, tmp_path: Path) -> None:
        """metadata 없는 dataset은 null/empty로 직렬화되고 응답이 깨지지 않는다 (#490)."""
        resp = self._service_with_catalog(
            tmp_path, [_FakeCatalogRef("datago", "air_quality", "대기오염")]
        ).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        dataset = cast(dict[str, object], providers[0]["datasets"][0])
        assert dataset["description"] is None
        assert dataset["tags"] == []
        assert dataset["source_url"] is None
        assert dataset["representation"] == "api_json"
        assert dataset["operations"] == []
        assert dataset["query_support"] is None
        assert dataset["requires_service_key"] is False
        assert dataset["request_parameters"] == []
        assert dataset["application"] is None

    def test_catalog_serializes_application_when_declared(self, tmp_path: Path) -> None:
        """raw_metadata.application을 그대로 전달한다 (활용신청 안내, secret 없음)."""
        ref = _FakeCatalogRef(
            "datago",
            "air_quality",
            "대기오염",
            service_key=True,
            raw_metadata_extra={
                "application": {
                    "required": True,
                    "url": "https://www.data.go.kr/data/15073861/openapi.do",
                },
            },
        )
        resp = self._service_with_catalog(tmp_path, [ref]).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        dataset = cast(dict[str, object], providers[0]["datasets"][0])
        assert dataset["application"] == {
            "required": True,
            "url": "https://www.data.go.kr/data/15073861/openapi.do",
        }

    def test_catalog_rejects_non_http_application_url(self, tmp_path: Path) -> None:
        """application.url이 http(s)가 아니면 통째로 노출하지 않는다(임의 스킴 차단)."""
        ref = _FakeCatalogRef(
            "datago",
            "air_quality",
            "대기오염",
            raw_metadata_extra={
                "application": {"required": True, "url": "javascript:alert(1)"},
            },
        )
        resp = self._service_with_catalog(tmp_path, [ref]).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        dataset = cast(dict[str, object], providers[0]["datasets"][0])
        assert dataset["application"] is None

    def test_catalog_serializes_request_parameters_without_secrets(self, tmp_path: Path) -> None:
        """raw_metadata.request_parameters를 secret-free allowlist로 직렬화한다."""
        ref = _FakeCatalogRef(
            "datago",
            "air_quality",
            "대기오염",
            service_key=True,
            raw_metadata_extra={
                "request_parameters": [
                    {
                        "name": "sidoName",
                        "required": True,
                        "description": "조회할 시·도",
                        "example": "서울",
                        "internal_hint": "leak me",
                    },
                    # service_key_param / secret-like 이름은 제외된다.
                    {"name": "serviceKey", "required": True},
                    {"name": "apiKey", "required": True},
                    # name 없는 항목은 버린다.
                    {"required": True},
                    "not-a-dict",
                ],
            },
        )
        resp = self._service_with_catalog(tmp_path, [ref]).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        dataset = cast(dict[str, object], providers[0]["datasets"][0])
        assert dataset["request_parameters"] == [
            {
                "name": "sidoName",
                "required": True,
                "description": "조회할 시·도",
                "example": "서울",
            }
        ]
        assert "internal_hint" not in json.dumps(resp.body, ensure_ascii=False)
        assert "leak me" not in json.dumps(resp.body, ensure_ascii=False)

    def test_catalog_never_exposes_raw_metadata_or_secrets(self, tmp_path: Path) -> None:
        """raw_metadata와 secret-like 값은 응답에 절대 노출되지 않는다 (#490)."""
        ref = _FakeCatalogRef(
            "datago",
            "air_quality",
            "대기오염",
            service_key=True,
            raw_metadata_extra={
                "internal_note": "provider private",
                "api_key": "sk-secret-value",
                "service_key": "raw-secret",
                "endpoint_template": "/openapi/{serviceKey}",
            },
        )
        resp = self._service_with_catalog(tmp_path, [ref]).catalog()

        assert resp.status_code == 200
        serialized = json.dumps(resp.body, ensure_ascii=False)
        assert "internal_note" not in serialized
        assert "provider private" not in serialized
        assert "sk-secret-value" not in serialized
        assert "raw-secret" not in serialized
        assert "endpoint_template" not in serialized
        # allowlist 필드만 존재한다.
        providers = cast(list[dict[str, object]], resp.body["providers"])
        dataset = cast(dict[str, object], providers[0]["datasets"][0])
        assert set(dataset) == {
            "name",
            "title",
            "description",
            "tags",
            "source_url",
            "representation",
            "operations",
            "query_support",
            "requires_service_key",
            "request_parameters",
            "application",
        }
        # service_key_param 존재 여부는 requires_service_key 불리언으로만 전달된다.
        assert dataset["requires_service_key"] is True
        assert "service_key_param" not in serialized

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
