"""Builder 서비스 로직 (#36).

Studio 같은 외부 UI가 Builder를 호출할 수 있도록 validate/preview/build/artifacts
연산을 HTTP 전송과 분리된 순수 로직으로 제공한다. 각 메서드는 ServiceResponse
(상태 코드 + JSON 직렬화 가능한 body)를 반환하며, dispatch가 (method, path)를
해당 연산으로 라우팅한다.

주요 구성:
    - ServiceResponse: 상태 코드 + body
    - BuilderService: validate/preview/build/artifacts 연산
    - dispatch: 경로 라우팅
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from ..errors import SpecLoadError, ValidationError
from ..pipeline import preview_build, run_build
from ..spec import BuildSpec, JsonValue, parse_spec
from ..spec.validator import validate_spec
from ..stages._path_safety import ensure_within, validate_path_segment
from ..stages.bronze.build import SourceClient
from ..tabular import DEFAULT_PREVIEW_LIMIT

# Builder API 계약 버전. contract/builder-api.yaml의 info.version과 일치해야 하며
# (test_service_contract가 강제), 응답에 실어 Studio 같은 소비자가 하위 호환을
# 협상할 수 있게 한다 (#209).
API_CONTRACT_VERSION = "1.0.0"


@dataclass(frozen=True)
class ServiceResponse:
    """서비스 연산 결과.

    속성:
        status_code: HTTP 상태 코드.
        body: JSON 직렬화 가능한 응답 본문.
    """

    status_code: int
    body: dict[str, JsonValue]


def _parse_spec_text(spec_yaml: str) -> BuildSpec:
    """YAML 텍스트를 BuildSpec으로 파싱한다."""
    raw = cast(object, yaml.safe_load(spec_yaml))
    if not isinstance(raw, dict):
        raise SpecLoadError("top-level YAML must be a mapping")
    return parse_spec(cast(dict[str, object], raw))


class BuilderService:
    """Builder 연산을 HTTP 전송과 무관하게 제공하는 서비스."""

    def __init__(
        self,
        *,
        output_root: Path,
        client_factory: Callable[[], SourceClient],
    ) -> None:
        self._output_root = output_root
        self._client_factory = client_factory

    def version(self) -> ServiceResponse:
        """Builder API 계약 버전을 반환한다 (#209).

        소비자(Studio 등)가 호출 전에 계약 호환성을 확인할 수 있는 메타 엔드포인트다.
        """
        return ServiceResponse(
            200, {"service": "kpubdata-builder", "api_version": API_CONTRACT_VERSION}
        )

    def validate(self, spec_yaml: str) -> ServiceResponse:
        """BuildSpec을 파싱·검증한다."""
        try:
            spec = _parse_spec_text(spec_yaml)
            validate_spec(spec)
        except SpecLoadError as exc:
            return ServiceResponse(400, {"status": "error", "error": str(exc)})
        except ValidationError as exc:
            return ServiceResponse(400, {"status": "invalid", "problems": list(exc.problems)})
        return ServiceResponse(
            200,
            {
                "status": "valid",
                "dataset_id": spec.dataset_id,
                "api_version": API_CONTRACT_VERSION,
            },
        )

    def preview(self, spec_yaml: str, *, limit: int = DEFAULT_PREVIEW_LIMIT) -> ServiceResponse:
        """각 소스의 스키마와 샘플 행을 산출한다 (파일 미기록)."""
        if limit < 1:
            return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
        spec_or_error = self._load_validated(spec_yaml)
        if isinstance(spec_or_error, ServiceResponse):
            return spec_or_error

        result = preview_build(spec_or_error, client=self._client_factory(), limit=limit)
        previews: list[JsonValue] = [
            {
                "source_key": p.source_key,
                "status": p.status,
                "error": p.error,
                "schema": [
                    {
                        "name": column.name,
                        "dtype": column.dtype,
                        "nullable": column.nullable,
                        "unique_count": column.unique_count,
                    }
                    for column in p.schema.columns
                ],
                "sample": list(p.preview.rows),
                "total_rows": p.preview.total_rows,
            }
            for p in result.previews
        ]
        return ServiceResponse(200, {"dataset_id": spec_or_error.dataset_id, "previews": previews})

    def build(self, spec_yaml: str, *, run_id: str | None = None) -> ServiceResponse:
        """파이프라인을 실행하고 결과를 반환한다.

        응답 코드 정책:
            - 모든 소스 성공: 200
            - 하나라도 소스 fetch/stage가 실패: 502 (upstream 소스 의존 실패).
              매니페스트는 partial 정책으로 남기 때문에 body에 outcomes와 manifest가
              실린다.
        """
        spec_or_error = self._load_validated(spec_yaml)
        if isinstance(spec_or_error, ServiceResponse):
            return spec_or_error

        result = run_build(
            spec_or_error,
            client=self._client_factory(),
            output_root=self._output_root,
            run_id=run_id,
        )
        outcomes: list[JsonValue] = [
            {
                "source_key": outcome.source_key,
                "status": outcome.status,
                "stages_completed": list(outcome.stages_completed),
                "error": outcome.error,
            }
            for outcome in result.outcomes
        ]
        status_code = 200 if result.status == "ok" else 502
        body: dict[str, JsonValue] = {
            "status": result.status,
            "run_id": result.context.run_id,
            "outcomes": outcomes,
            "manifest": str(result.manifest_path),
            "api_version": API_CONTRACT_VERSION,
        }
        # 실패한 빌드는 첫 번째 실패 outcome의 error를 최상위 `error` 요약으로 노출해,
        # Studio 같은 소비자가 outcomes 배열을 파싱하지 않고도 사람이 읽을 수 있는
        # 사유를 즉시 표면화할 수 있게 한다 (#226).
        if result.status != "ok":
            first_error = next(
                (o.error for o in result.outcomes if o.status != "ok" and o.error), None
            )
            body["error"] = first_error or "build failed"
        return ServiceResponse(status_code, body)

    def artifacts(self, run_id: str) -> ServiceResponse:
        """실행 워크스페이스의 산출물 파일 목록을 반환한다."""
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        run_dir = self._output_root / run_id
        ensure_within(self._output_root, run_dir, label="run directory")
        if not run_dir.exists():
            return ServiceResponse(404, {"error": f"run not found: {run_id}"})

        files = sorted(
            str(path.relative_to(run_dir)) for path in run_dir.rglob("*") if path.is_file()
        )
        return ServiceResponse(200, {"run_id": run_id, "files": list(files)})

    def list_builds(self, *, limit: int = 50) -> ServiceResponse:
        """실행 이력 목록을 최신 수정 시각 기준으로 내림차순 반환한다.

        output_root 아래의 디렉터리를 스캔해 manifest.json이 있는 실행만
        포함한다. Studio 같은 소비자가 빌드 이력 목록을 표시하는 데 쓰인다 (#250).
        """
        if not self._output_root.exists():
            return ServiceResponse(200, {"builds": []})

        candidates = sorted(
            (d for d in self._output_root.iterdir() if d.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        builds: list[JsonValue] = []
        for run_dir in candidates[:limit]:
            manifest_path = run_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            builds.append(
                {
                    "run_id": run_dir.name,
                    "status": "failed" if manifest.get("errors") else "ok",
                    "started_at": manifest.get("started_at"),
                    "finished_at": manifest.get("finished_at"),
                }
            )
        return ServiceResponse(200, {"builds": builds})

    def _load_validated(self, spec_yaml: str) -> BuildSpec | ServiceResponse:
        """spec_yaml을 파싱·검증하고, 실패 시 오류 ServiceResponse를 반환한다."""
        try:
            spec = _parse_spec_text(spec_yaml)
            validate_spec(spec)
        except SpecLoadError as exc:
            return ServiceResponse(400, {"status": "error", "error": str(exc)})
        except ValidationError as exc:
            return ServiceResponse(400, {"status": "invalid", "problems": list(exc.problems)})
        return spec


def _spec_from_body(body: Mapping[str, JsonValue] | None) -> str | ServiceResponse:
    """요청 body에서 spec YAML 문자열을 추출한다."""
    if not body or "spec" not in body:
        return ServiceResponse(400, {"error": "missing 'spec' in request body"})
    spec_value = body["spec"]
    if not isinstance(spec_value, str):
        return ServiceResponse(400, {"error": "'spec' must be a YAML string"})
    return spec_value


def dispatch(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
) -> ServiceResponse:
    """(method, path)를 BuilderService 연산으로 라우팅한다."""
    if method == "GET" and path == "/version":
        return service.version()

    if method == "POST" and path == "/validate":
        spec = _spec_from_body(body)
        return spec if isinstance(spec, ServiceResponse) else service.validate(spec)

    if method == "POST" and path == "/preview":
        spec = _spec_from_body(body)
        if isinstance(spec, ServiceResponse):
            return spec
        # limit이 명시되면 양의 정수여야 한다 — 잘못된 값을 조용히 기본값으로 떨어뜨리지 않는다.
        if body is not None and "limit" in body:
            limit_value = body["limit"]
            # bool은 int의 하위 타입이지만 limit 의미가 없으므로 거부.
            if not isinstance(limit_value, int) or isinstance(limit_value, bool) or limit_value < 1:
                return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
            limit = limit_value
        else:
            limit = DEFAULT_PREVIEW_LIMIT
        return service.preview(spec, limit=limit)

    if method == "POST" and path == "/build":
        spec = _spec_from_body(body)
        if isinstance(spec, ServiceResponse):
            return spec
        run_id: str | None = None
        if body is not None and "run_id" in body:
            run_id_value = body["run_id"]
            # run_id가 명시되면 비어있지 않은 문자열이어야 한다. 잘못된 타입을 조용히
            # 자동 생성 run id로 떨어뜨리면 클라이언트가 의도한 run id와 실제 기록 위치가
            # 달라지므로 400으로 거부한다 (#185).
            if not isinstance(run_id_value, str) or not run_id_value.strip():
                return ServiceResponse(400, {"error": "'run_id' must be a non-empty string"})
            # 경로 안전하지 않은 run_id("../bad" 등)는 이후 BuildContext.create에서
            # ValueError를 일으켜 HTTP 어댑터에서 500/연결 끊김이 되므로, 진입점에서
            # 동일한 safe-segment 규칙으로 검증해 구조화된 400을 반환한다 (#200).
            try:
                validate_path_segment(run_id_value, field_name="run_id")
            except ValueError as exc:
                return ServiceResponse(400, {"error": str(exc)})
            run_id = run_id_value
        return service.build(spec, run_id=run_id)

    if method == "GET" and path.startswith("/artifacts/"):
        run_id = path[len("/artifacts/") :]
        return service.artifacts(run_id)

    if method == "GET" and path == "/builds":
        limit = 50
        if body is not None and "limit" in body:
            limit_value = body["limit"]
            if not isinstance(limit_value, int) or isinstance(limit_value, bool) or limit_value < 1:
                return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
            limit = limit_value
        return service.list_builds(limit=limit)

    return ServiceResponse(404, {"error": f"not found: {method} {path}"})


__all__ = ["API_CONTRACT_VERSION", "BuilderService", "ServiceResponse", "dispatch"]
