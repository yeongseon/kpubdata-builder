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

import heapq
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast, runtime_checkable
from urllib.parse import parse_qs

import yaml
from kpubdata import Client
from kpubdata.core.models import DatasetRef

from ..errors import SpecLoadError, ValidationError
from ..pipeline import preview_build, run_build
from ..spec import BuildSpec, JsonValue, parse_spec
from ..spec.validator import validate_spec
from ..stages._path_safety import ensure_within, validate_path_segment
from ..stages.bronze.build import SourceClient
from ..store import BuildIndex
from ..tabular import DEFAULT_PREVIEW_LIMIT
from .auth import AuthError, Principal, authenticate

logger = logging.getLogger(__name__)

_OWNERSHIP_ENV = "ENFORCE_OWNERSHIP"


@runtime_checkable
class _CloseableClient(Protocol):
    def close(self) -> None: ...


def _close_request_client(client: SourceClient) -> None:
    if isinstance(client, _CloseableClient):
        client.close()


def _authenticated_provider_names(client: SourceClient) -> frozenset[str]:
    authenticated_providers = cast(Client, client).iter_authenticated_providers()
    return frozenset(provider.name for provider in authenticated_providers)


def _requires_service_key(dataset: DatasetRef, auth_provider_names: frozenset[str]) -> bool:
    return dataset.provider in auth_provider_names or bool(
        dataset.raw_metadata.get("service_key_param")
    )


def _enforce_ownership() -> bool:
    """run 소유권 강제가 활성화되어 있는지 (#389). 기본 off — 하위 호환."""
    return os.environ.get(_OWNERSHIP_ENV, "").lower() in ("true", "1")


def _read_manifest_created_by(service: BuilderService, run_id: str) -> str | None:
    """manifest.json에서 created_by를 읽는다 (#389). 없거나 읽을 수 없으면 None."""
    manifest_path = service._output_root / run_id / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return cast(str | None, data.get("created_by"))
    except Exception:
        return None


def _check_ownership(
    service: BuilderService, run_id: str, principal: Principal
) -> ServiceResponse | None:
    """소유권을 검사한다 (#389). 통과 시 None, 거부 시 403 ServiceResponse.

    - ENFORCE_OWNERSHIP off → 항상 None (통과).
    - dev/service principal → 모든 run 접근 가능 (관리자 권한).
    - oidc principal → created_by가 본인과 일치해야 함. NULL이면 거부.
    """
    if not _enforce_ownership():
        return None
    if principal.kind in ("dev", "service"):
        return None
    created_by = _read_manifest_created_by(service, run_id)
    if created_by is not None and created_by == principal.label:
        return None
    return ServiceResponse(403, {"error": "forbidden: not run owner"})


# Build list entry type for API responses
_BuildListEntry = dict[str, str | None]


def _apply_ownership(
    entries: list[_BuildListEntry], principal: Principal | None
) -> list[_BuildListEntry]:
    """list_builds 응답에서 본인 소유 run만 남긴다 (#433).

    ENFORCE_OWNERSHIP+oidc principal일 때만 필터링. dev/service principal과
    principal=None은 통과 (관리자 권한 + 하위 호환). 인덱스 분기와 파일시스템
    폴백 양쪽에서 공통으로 적용해 폴백 경로가 필터를 우회하지 않게 한다.
    """
    if not (_enforce_ownership() and principal and principal.kind == "oidc"):
        return entries
    return [e for e in entries if e.get("created_by") == principal.label]


# Builder API 계약 버전. contract/builder-api.yaml의 info.version과 일치해야 하며
# (test_service_contract가 강제), 응답에 실어 Studio 같은 소비자가 하위 호환을
# 협상할 수 있게 한다 (#209).
API_CONTRACT_VERSION = "1.3.0"


@dataclass(frozen=True)
class ServiceResponse:
    """서비스 연산 결과.

    속성:
        status_code: HTTP 상태 코드.
        body: JSON 직렬화 가능한 응답 본문.
    """

    status_code: int
    body: dict[str, JsonValue]


@dataclass(frozen=True)
class FileResponse:
    """파일 서빙 응답 (#323).

    속성:
        status_code: HTTP 상태 코드.
        file_path: 제공할 파일 경로.
        filename: 다운로드 시 사용할 파일 이름 (Content-Disposition 헤더용).
    """

    status_code: int
    file_path: Path
    filename: str


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
        self._build_index = BuildIndex(output_root)  # #309, ADR 0003

    def version(self) -> ServiceResponse:
        """Builder API 계약 버전을 반환한다 (#209).

        소비자(Studio 등)가 호출 전에 계약 호환성을 확인할 수 있는 메타 엔드포인트다.
        """
        return ServiceResponse(
            200, {"service": "kpubdata-builder", "api_version": API_CONTRACT_VERSION}
        )

    def catalog(self) -> ServiceResponse:
        """사용 가능한 provider/dataset 카탈로그를 반환한다 (#416, BL2, #436).

        kpubdata Client의 공개 ``datasets.list()`` 로 모든 데이터셋을 조회한 뒤
        provider별로 그룹화한다 (ADR 0011 — Builder가 provider 목록을
        하드코딩하지 않는다). 이전에는 ``getattr(client, "_catalog")`` private
        접근 + 8개 provider 하드코딩 튜플을 써서 kpubdata에 provider가 추가돼도
        카탈로그에 안 떴다 (#436). 시크릿 값은 노출하지 않고 필요 여부만 표시한다.
        """
        client = self._client_factory()
        try:
            all_datasets = cast(Client, client).datasets.list()
            auth_provider_names = _authenticated_provider_names(client)
        except Exception as exc:
            return ServiceResponse(502, {"error": f"catalog unavailable: {exc}"})
        finally:
            _close_request_client(client)

        # provider별 그룹화 (등록 순서 보존 위해 dict 사용).
        grouped: dict[str, list[DatasetRef]] = {}
        for ds in all_datasets:
            grouped.setdefault(ds.provider, []).append(ds)

        providers_data: list[JsonValue] = [
            {
                "name": provider_name,
                "datasets": [
                    {
                        "name": item.dataset_key,
                        "title": item.name,
                        "requires_service_key": _requires_service_key(item, auth_provider_names),
                    }
                    for item in items
                ],
            }
            for provider_name, items in grouped.items()
        ]
        return ServiceResponse(200, {"providers": providers_data})

    def validate(self, spec_yaml: str) -> ServiceResponse:
        """BuildSpec을 파싱·검증한다."""
        try:
            spec = _parse_spec_text(spec_yaml)
            validate_spec(spec)
        except SpecLoadError as exc:
            return ServiceResponse(400, {"status": "error", "error": str(exc)})
        except ValidationError as exc:
            body: dict[str, JsonValue] = {"status": "invalid", "problems": list(exc.problems)}
            if exc.structured_problems:
                body["structured_problems"] = [
                    {"code": p.code, "path": p.path, "message": p.message, "hint": p.hint}
                    for p in exc.structured_problems
                ]
            return ServiceResponse(400, body)
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

        client = self._client_factory()
        try:
            result = preview_build(spec_or_error, client=client, limit=limit)
        finally:
            _close_request_client(client)
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
                "statistics": {
                    "row_count": p.statistics.row_count,
                    "null_counts": dict(p.statistics.null_counts),
                    "duplicate_rate": p.statistics.duplicate_rate,
                },
            }
            for p in result.previews
        ]
        return ServiceResponse(200, {"dataset_id": spec_or_error.dataset_id, "previews": previews})

    def build(
        self, spec_yaml: str, *, run_id: str | None = None, created_by: str | None = None
    ) -> ServiceResponse:
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

        client = self._client_factory()
        try:
            result = run_build(
                spec_or_error,
                client=client,
                output_root=self._output_root,
                run_id=run_id,
                created_by=created_by,
            )
        finally:
            _close_request_client(client)
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

        # ADR 0003: 빌드 완료 후 인덱스 갱신 (best-effort, 실패해도 빌드 성공 유지)
        try:
            manifest_data = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            started_at = manifest_data.get("started_at")
            finished_at = manifest_data.get("finished_at")
            self._build_index.insert_or_replace(
                run_id=result.context.run_id,
                status=result.status,  # type: ignore[arg-type]
                started_at=started_at,
                finished_at=finished_at,
                created_by=manifest_data.get("created_by"),
            )
        except Exception:
            # 인덱스 갱신 실패는 무시 (ADR 0003)
            pass

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

    def manifest(self, run_id: str) -> ServiceResponse:
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        run_dir = self._output_root / run_id
        ensure_within(self._output_root, run_dir, label="run directory")
        manifest_path = run_dir / "manifest.json"
        ensure_within(run_dir, manifest_path, label="manifest file")
        if not manifest_path.exists():
            return ServiceResponse(404, {"error": f"manifest not found: {run_id}"})

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return ServiceResponse(500, {"error": f"invalid manifest JSON: {exc.msg}"})
        except OSError as exc:
            return ServiceResponse(500, {"error": f"failed to read manifest: {exc}"})
        if not isinstance(manifest, dict):
            return ServiceResponse(500, {"error": "invalid manifest: expected object"})
        return ServiceResponse(200, cast(dict[str, JsonValue], manifest))

    def serve_artifact_file(self, run_id: str, file_path: str) -> ServiceResponse | FileResponse:
        """실행 워크스페이스의 특정 파일을 제공한다 (#323).

        경로 트래버설 공격을 방지하기 위해 run_id와 file_path 모두
        검증하며, 심볼릭 링크를 따르지 않는다.

        매개변수:
            run_id: 실행 식별자.
            file_path: 요청된 파일 경로 (run_id 하위의 상대 경로).

        반환값:
            FileResponse (파일 발견 시) 또는 ServiceResponse (오류 시).
        """
        # run_id 검증
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        # run_dir 확인
        run_dir = self._output_root / run_id
        ensure_within(self._output_root, run_dir, label="run directory")
        if not run_dir.exists():
            return ServiceResponse(404, {"error": f"run not found: {run_id}"})

        # file_path 검증 (경로 트래버설 방지)
        try:
            validate_path_segment(file_path, field_name="file_path")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        # 요청된 파일의 전체 경로 계산
        requested_file = run_dir / file_path
        # 경로가 run_dir 내에 있는지 확인 (심볼릭 링크도 해석하여 안전 검사)
        ensure_within(run_dir, requested_file, label="artifact file")

        if not requested_file.exists():
            return ServiceResponse(404, {"error": f"file not found: {file_path}"})
        if not requested_file.is_file():
            return ServiceResponse(400, {"error": f"not a file: {file_path}"})

        # 파일명 추출 (Content-Disposition용)
        filename = requested_file.name

        return FileResponse(status_code=200, file_path=requested_file, filename=filename)

    def list_builds(
        self, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """실행 이력 목록을 최신 완료 시각 기준 내림차순 반환한다.

        ADR 0003에 따라 SQLite 인덱스를 우선 조회하고, 인덱스가 없거나
        비어있으면 파일시스템 스캔으로 폴백한다. ENFORCE_OWNERSHIP+oidc일 때는
        두 경로 모두 _apply_ownership으로 본인 소유 run만 노출한다 (#433).
        """
        # 인덱스 우선 조회
        try:
            entries = self._build_index.list_builds(limit=limit)
            if entries:
                index_builds: list[_BuildListEntry] = [
                    {
                        "run_id": entry.run_id,
                        "status": entry.status,
                        "started_at": entry.started_at,
                        "finished_at": entry.finished_at,
                        "created_by": entry.created_by,
                    }
                    for entry in entries
                ]
                return ServiceResponse(
                    200,
                    {"builds": cast(list[JsonValue], _apply_ownership(index_builds, principal))},
                )
        except Exception:
            # 인덱스 조회 실패. ENFORCE_OWNERSHIP+oidc면 타인 run이 폴백으로
            # 새어나갈 수 있으므로 fail-closed로 빈 배열을 반환한다 (#433).
            # 일반 모드는 기존대로 파일시스템 폴백으로 진행한다 (ADR 0003).
            if _enforce_ownership() and principal and principal.kind == "oidc":
                logger.warning(
                    "build index query failed; returning empty list "
                    "(ownership enforced, fail-closed)",
                    exc_info=True,
                )
                return ServiceResponse(200, {"builds": []})

        # 폴백: 파일시스템 스캔
        if not self._output_root.exists():
            return ServiceResponse(200, {"builds": []})

        candidates = heapq.nlargest(
            limit,
            (d for d in self._output_root.iterdir() if d.is_dir()),
            key=lambda p: p.stat().st_mtime,
        )
        fs_builds: list[_BuildListEntry] = []
        for run_dir in candidates:
            manifest_path = run_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            fs_builds.append(
                {
                    "run_id": run_dir.name,
                    "status": "failed" if manifest.get("errors") else "ok",
                    "started_at": manifest.get("started_at"),
                    "finished_at": manifest.get("finished_at"),
                    "created_by": manifest.get("created_by"),
                }
            )
        return ServiceResponse(
            200,
            {"builds": cast(list[JsonValue], _apply_ownership(fs_builds, principal))},
        )

    def _load_validated(self, spec_yaml: str) -> BuildSpec | ServiceResponse:
        """spec_yaml을 파싱·검증하고, 실패 시 오류 ServiceResponse를 반환한다."""
        try:
            spec = _parse_spec_text(spec_yaml)
            validate_spec(spec)
        except SpecLoadError as exc:
            return ServiceResponse(400, {"status": "error", "error": str(exc)})
        except ValidationError as exc:
            body: dict[str, JsonValue] = {"status": "invalid", "problems": list(exc.problems)}
            if exc.structured_problems:
                body["structured_problems"] = [
                    {"code": p.code, "path": p.path, "message": p.message, "hint": p.hint}
                    for p in exc.structured_problems
                ]
            return ServiceResponse(400, body)
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
    query: str = "",
    *,
    api_key: str | None = None,
    bearer_token: str | None = None,
) -> ServiceResponse | FileResponse:
    """(method, path)를 BuilderService 연산으로 라우팅한다.

    GET /healthz는 인증 없이 반환하고 (#372), 그 외 엔드포인트는
    authenticate()로 Principal을 얻어 인증 게이트를 통과한 후 라우팅한다.
    dev-mode이면 인증 생략, 그 외는 fail-closed(401)로 동작한다 (#248, #384).

    반환값:
        ServiceResponse 또는 FileResponse (#323).
    """
    # /healthz는 인증 게이트 밖에서 무인증 노출 (#372).
    if method == "GET" and path == "/healthz":
        return ServiceResponse(200, {"status": "ok"})

    # 인증 게이트 (#384): Principal을 얻지 못하면 401.
    principal = authenticate(api_key=api_key, bearer_token=bearer_token)
    if isinstance(principal, AuthError):
        return ServiceResponse(principal.status_code, {"error": principal.reason})

    if method == "GET" and path == "/version":
        return service.version()

    if method == "GET" and path == "/catalog":
        return service.catalog()

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
        return service.build(spec, run_id=run_id, created_by=principal.label)

    if method == "GET" and path.startswith("/builds/") and path.endswith("/manifest"):
        rest = path[len("/builds/") :]
        parts = rest.split("/", 1)
        run_id = parts[0]
        if len(parts) == 2 and parts[1] == "manifest":
            try:
                validate_path_segment(run_id, field_name="run_id")
            except ValueError as exc:
                return ServiceResponse(400, {"error": str(exc)})
            ownership_error = _check_ownership(service, run_id, principal)
            if ownership_error is not None:
                return ownership_error
            return service.manifest(run_id)

    if method == "GET" and path.startswith("/artifacts/"):
        rest = path[len("/artifacts/") :]
        parts = rest.split("/", 1)
        run_id = parts[0]
        # run_id를 소유권 검사보다 먼저 검증 (#439). _read_manifest_created_by 가
        # 검증 없이 경로를 조립하므로 "../" 등 unsafe 세그먼트가 _check_ownership
        # 보다 먼저 도달해야 한다.
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})
        ownership_error = _check_ownership(service, run_id, principal)
        if ownership_error is not None:
            return ownership_error
        if len(parts) == 2 and parts[1]:
            return service.serve_artifact_file(run_id, parts[1])
        return service.artifacts(run_id)

    if method == "GET" and path == "/builds":
        limit = 50
        # 표준 REST 스타일에 맞게 쿼리 파라미터(?limit=N)를 우선 지원한다 (#252).
        # 기존 소비자 호환을 위해 body의 limit도 계속 받아들인다.
        query_params = parse_qs(query)
        if "limit" in query_params:
            raw_limit = query_params["limit"][-1]
            try:
                query_limit = int(raw_limit)
            except ValueError:
                return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
            if query_limit < 1:
                return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
            limit = query_limit
        elif body is not None and "limit" in body:
            limit_value = body["limit"]
            if not isinstance(limit_value, int) or isinstance(limit_value, bool) or limit_value < 1:
                return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
            limit = limit_value
        return service.list_builds(limit=limit, principal=principal)

    return ServiceResponse(404, {"error": f"not found: {method} {path}"})


__all__ = ["API_CONTRACT_VERSION", "BuilderService", "ServiceResponse", "FileResponse", "dispatch"]
