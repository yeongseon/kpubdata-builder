"""Studio↔Builder 계약 통합 테스트 (#226).

Studio 같은 외부 UI가 직렬화하는 spec(snake_case 매핑)을 실제 BuilderService에
dispatch 레이어를 통해 흘려보내고, Studio가 의존하는 실제 wire 응답 형태를 고정한다.

검증 대상 엔드포인트:
    - POST /validate (200)
    - POST /build SUCCESS (200)
    - POST /build FAILURE (502)
    - GET  /artifacts/{run_id} (200)

데이터는 in-test fake source client(dataset(key).list(**params).items)로 공급한다.
실제 네트워크 호출은 하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml

from kpubdata_builder.service import API_CONTRACT_VERSION, BuilderService, dispatch
from kpubdata_builder.spec import JsonValue


class _FakeResult:
    """SourceClient Protocol의 DatasetResult 부분(items)을 만족하는 fake."""

    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    """dataset(key).list(**params) 부분을 만족하는 fake."""

    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    """builder가 의존하는 SourceClient Protocol을 구조적으로 만족하는 fake."""

    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _studio_spec(*, dataset_value: str = "air_quality") -> dict[str, JsonValue]:
    """Studio가 직렬화하는 형태의 spec 매핑을 만든다(snake_case).

    sources/exports/metadata와 선택 필드(params, alias, options)를 포함해
    Studio가 보내는 wire 형태를 의도적으로 그대로 재현한다.
    """
    return {
        "dataset_id": "dataset.studio_sample",
        "title": "Studio Sample Dataset",
        "description": "A dataset assembled via the Studio UI.",
        "sources": [
            {
                "provider": "datago",
                "dataset": dataset_value,
                "params": {"region": "seoul", "year": 2024},
                "alias": "aq",
            }
        ],
        "exports": [
            {
                "kind": "jsonl",
                "output_path": "out/data.jsonl",
                "options": {"ensure_ascii": False},
            }
        ],
        "metadata": {"owner": "studio", "license": "CC-BY-4.0"},
    }


def _serialize(spec: dict[str, JsonValue]) -> str:
    """Studio 직렬화 spec 매핑을 builder가 받는 wire 형태(YAML 문자열)로 변환한다."""
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda: client)


class TestValidateContract:
    def test_validate_returns_valid_envelope(self, tmp_path: Path) -> None:
        spec_yaml = _serialize(_studio_spec())
        resp = dispatch(_service(tmp_path), "POST", "/validate", {"spec": spec_yaml})

        assert resp.status_code == 200
        assert resp.body == {
            "status": "valid",
            "dataset_id": "dataset.studio_sample",
            "api_version": API_CONTRACT_VERSION,
        }


class TestBuildSuccessContract:
    def test_build_success_envelope(self, tmp_path: Path) -> None:
        spec_yaml = _serialize(_studio_spec())
        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/build",
            {"spec": spec_yaml, "run_id": "studio-run"},
        )

        assert resp.status_code == 200
        body = resp.body
        # 최상위 wire 형태 고정: status/manifest/outcomes/run_id/api_version.
        assert body["status"] == "ok"
        assert body["run_id"] == "studio-run"
        assert body["api_version"] == API_CONTRACT_VERSION
        assert isinstance(body["manifest"], str)
        assert body["manifest"].endswith("manifest.json")

        outcomes = body["outcomes"]
        assert isinstance(outcomes, list)
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert isinstance(outcome, dict)
        # alias가 주어지면 outcome의 source_key는 alias가 된다(provider.dataset 대신).
        assert outcome["source_key"] == "aq"
        assert outcome["status"] == "ok"
        assert outcome["error"] is None
        assert isinstance(outcome["stages_completed"], list)

        # manifest 파일이 실제로 기록되었는지 확인.
        assert (tmp_path / "studio-run" / "manifest.json").exists()


class TestBuildFailureContract:
    def test_build_failure_envelope(self, tmp_path: Path) -> None:
        # 존재하지 않는 소스를 가리키게 해서 fetch 실패를 유도한다.
        failing_spec = _studio_spec(dataset_value="missing")
        spec_yaml = _serialize(failing_spec)
        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/build",
            {"spec": spec_yaml, "run_id": "studio-fail"},
        )

        assert resp.status_code == 502
        body = resp.body
        assert body["status"] == "failed"
        assert body["run_id"] == "studio-fail"
        assert body["api_version"] == API_CONTRACT_VERSION

        outcomes = body["outcomes"]
        assert isinstance(outcomes, list)
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert isinstance(outcome, dict)
        # fetch는 provider.dataset(datago.missing)로 시도하지만, outcome의 source_key는
        # alias("aq")로 태깅된다(_output_source_key 정책).
        assert outcome["source_key"] == "aq"
        assert outcome["status"] == "failed"
        assert isinstance(outcome["error"], str)
        assert outcome["error"]

        # #226: 최상위 human-readable error 요약은 첫 실패 outcome의 error에서 파생된다.
        assert isinstance(body["error"], str)
        assert body["error"] == outcome["error"]


class TestArtifactsContract:
    def test_artifacts_lists_files_after_build(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        spec_yaml = _serialize(_studio_spec())
        dispatch(service, "POST", "/build", {"spec": spec_yaml, "run_id": "studio-art"})

        resp = dispatch(service, "GET", "/artifacts/studio-art", None)

        assert resp.status_code == 200
        body = resp.body
        assert body["run_id"] == "studio-art"
        files = body["files"]
        assert isinstance(files, list)
        assert all(isinstance(f, str) for f in files)
        assert any("manifest.json" in f for f in files)
