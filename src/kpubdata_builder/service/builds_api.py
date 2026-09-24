"""Build 산출물 조회 서비스 (#596 후속, #637).

완료된 run 의 **읽기 표면** 이다 — artifacts 목록, manifest, spec 스냅샷, 파일
서빙, build 목록, event timeline.

빌드를 *실행하는* 쪽(``build``/``submit_build``/``cancel_build``)은 남겨 두었다.
그쪽은 client factory, credential resolver, upload repository, spec 검증까지 열한
가지를 쥐고 있어서, 지금 함께 떼면 새 서비스의 생성자가 사실상 ``BuilderService``
전체를 다시 받게 된다 — #596 이 없애려던 모양 그대로다. 읽기 경로는 네 가지만
쓰므로 경계가 실제로 좁아진다.

**wire 계약은 바뀌지 않는다.** ``BuilderService`` 가 같은 시그니처로 위임한다.
"""

from __future__ import annotations

import heapq
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast
from urllib.parse import unquote

from kpubdata_builder.manifest import status_from_manifest
from kpubdata_builder.service import events as events_service
from kpubdata_builder.service import ownership as ownership_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.events import BuildEventStore
from kpubdata_builder.service.responses import FileResponse, ServiceResponse
from kpubdata_builder.spec import JsonValue, compute_spec_digest
from kpubdata_builder.spec.serializer import BUILDSPEC_SNAPSHOT_FILENAME
from kpubdata_builder.stages._path_safety import ensure_within, validate_path_segment
from kpubdata_builder.store.artifacts import ArtifactStore
from kpubdata_builder.store.build_index import BuildIndex

logger = logging.getLogger(__name__)

# Build list entry type for API responses
_BuildListEntry = dict[str, str | None]


def _apply_ownership(
    entries: list[_BuildListEntry], principal: Principal | None
) -> list[_BuildListEntry]:
    """list_builds 응답에서 본인 소유 run만 남긴다 (#433, #505).

    ENFORCE_OWNERSHIP+oidc principal일 때만 필터링. dev/service principal과
    principal=None은 통과 (관리자 권한 + 하위 호환). 인덱스 분기와 파일시스템
    폴백 양쪽에서 공통으로 적용해 폴백 경로가 필터를 우회하지 않게 한다.

    각 entry는 판정용으로 내부 전용 "owner_id" 키를 담고 있어야 한다 — 응답
    직전에 ``_strip_internal_fields``로 제거되므로 wire 응답 shape는 바뀌지
    않는다.
    """
    if not (ownership_module.enforce_ownership() and principal and principal.kind == "oidc"):
        return entries
    return [
        e
        for e in entries
        if ownership_module.ownership_allows(
            created_by=e.get("created_by"),
            owner_id=e.get("owner_id"),
            principal=principal,
            enforce=True,
        )
    ]


def _strip_internal_fields(entries: list[_BuildListEntry]) -> list[_BuildListEntry]:
    """ownership 판정에만 쓰인 내부 전용 필드를 응답 직전에 제거한다 (#505).

    ``owner_id``는 canonical hash일 뿐 클라이언트에 의미가 없고, 이를 노출하면
    ``/builds`` 응답 wire shape가 바뀐다 — 계약 변경 없이 내부적으로만 쓴다.
    """
    return [{k: v for k, v in e.items() if k != "owner_id"} for e in entries]


class BuildArtifactsApiService:
    """완료된 run 의 산출물·manifest·spec·event 조회 (#433, #487, #496)."""

    def __init__(
        self,
        *,
        output_root: Path,
        store: ArtifactStore,
        build_index: BuildIndex,
        event_store: Callable[[], BuildEventStore],
    ) -> None:
        self._output_root = output_root
        self._store = store
        self._build_index = build_index
        # event store 는 지연 생성된다(#496) — preview 만 하는 워크스페이스에
        # sqlite 파일을 남기지 않기 위해서다. 그 성질을 유지하려고 값이 아니라
        # 접근자를 받는다.
        self._event_store_factory = event_store

    @property
    def _event_store(self) -> BuildEventStore:
        return self._event_store_factory()

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

        # wire에는 항상 run 디렉터리 기준 POSIX 상대 경로만 노출한다 — output_root 절대
        # 경로나 OS별 구분자를 드러내지 않는다. `serve_artifact_file`이 받는 canonical
        # artifact identifier가 바로 이 값이다(클라이언트는 storage layout을 알 필요 없다).
        files = sorted(
            path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file()
        )
        return ServiceResponse(200, {"run_id": run_id, "files": list(files)})

    def manifest(self, run_id: str) -> ServiceResponse:
        """persisted manifest를 읽되 내부 ownership 필드는 wire에서 제거한다.

        ``owner_id``는 디스크의 ``manifest.json``과 BuildIndex에만 저장되는 내부
        식별자다(#505). OpenAPI ``BuildManifest``는 실제 HTTP 응답의 SSOT이므로
        이 메서드에서 명시적으로 제거해 공개 API 필드가 되지 않게 한다.
        """
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        # ADR 0016: manifest 정본은 store 를 통해 조회한다(cubrid=CUBRID 행 우선, FS 폴백;
        # local=FS). get_manifest 는 경로 안전·손상·미존재를 모두 None 으로 합친다.
        manifest = self._store.get_manifest(run_id)
        if manifest is None:
            return ServiceResponse(404, {"error": f"manifest not found: {run_id}"})
        manifest.pop("owner_id", None)
        return ServiceResponse(200, cast(dict[str, JsonValue], manifest))

    def spec(self, run_id: str) -> ServiceResponse:
        """run에서 실제 사용한 canonical BuildSpec snapshot과 digest를 반환한다."""
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        run_dir = self._output_root / run_id
        ensure_within(self._output_root, run_dir, label="run directory")
        if not run_dir.is_dir():
            return ServiceResponse(404, {"error": f"run not found: {run_id}"})

        snapshot_path = run_dir / BUILDSPEC_SNAPSHOT_FILENAME
        ensure_within(run_dir, snapshot_path, label="BuildSpec snapshot")
        if not snapshot_path.is_file():
            return ServiceResponse(
                404, {"error": f"BuildSpec snapshot unavailable for run: {run_id}"}
            )
        try:
            payload = snapshot_path.read_bytes()
            spec_text = payload.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ServiceResponse(500, {"error": f"failed to read BuildSpec snapshot: {exc}"})
        return ServiceResponse(
            200,
            {
                "run_id": run_id,
                "spec": spec_text,
                "spec_digest": compute_spec_digest(payload),
            },
        )

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

        # file_path 검증 (경로 트래버설 방지).
        #
        # canonical artifact identifier는 `GET /artifacts/{run_id}`가 돌려주는 run
        # 디렉터리 기준 POSIX 상대 경로(예: "silver/datago.air_quality/table.parquet")다.
        # HTTP 라우트는 이 경로를 URL segment 사이의 "/"로만 넘기고 각 segment는 percent
        # 인코딩될 수 있으므로(브라우저가 non-ASCII/특수문자를 %XX로 바꾼다), 먼저 한 번
        # percent-decode한다. decode 후에는 반드시 다시 검증한다 — "%2e%2e"/"%2f"/"%5c"
        # 같은 인코딩된 트래버설이 decode되어 성분 검사에 걸리고, 이중 인코딩("%252e")은
        # decode 후에도 "%"가 남아 성분 규칙에서 거부된다.
        decoded_path = unquote(file_path)
        segments = decoded_path.replace("\\", "/").split("/")
        if not decoded_path.strip() or any(seg in ("", ".", "..") for seg in segments):
            return ServiceResponse(
                400, {"error": f"file_path is not a safe relative path: {file_path!r}"}
            )
        try:
            for segment in segments:
                validate_path_segment(segment, field_name="file_path")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        # 요청된 파일의 전체 경로 계산 (성분 검증을 통과한 상대 경로만 결합)
        requested_file = run_dir.joinpath(*segments)
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
                        "owner_id": entry.owner_id,
                    }
                    for entry in entries
                ]
                filtered = _strip_internal_fields(_apply_ownership(index_builds, principal))
                return ServiceResponse(200, {"builds": cast(list[JsonValue], filtered)})
        except Exception:
            # 인덱스 조회 실패. ENFORCE_OWNERSHIP+oidc면 타인 run이 폴백으로
            # 새어나갈 수 있으므로 fail-closed로 빈 배열을 반환한다 (#433).
            # 일반 모드는 기존대로 파일시스템 폴백으로 진행한다 (ADR 0003).
            if ownership_module.enforce_ownership() and principal and principal.kind == "oidc":
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
                    # manifest.json이 정본이므로 파생 규칙은 한 곳에만 둔다
                    # (#481) — cancelled run은 errors가 비어 있을 수 있어
                    # 기존 "errors 유무" 파생만으로는 ok로 잘못 보고된다.
                    "status": status_from_manifest(manifest),
                    "started_at": manifest.get("started_at"),
                    "finished_at": manifest.get("finished_at"),
                    "created_by": manifest.get("created_by"),
                    "owner_id": manifest.get("owner_id"),
                }
            )
        filtered = _strip_internal_fields(_apply_ownership(fs_builds, principal))
        return ServiceResponse(200, {"builds": cast(list[JsonValue], filtered)})

    def get_build_events(self, run_id: str, *, limit: int, tail: bool) -> ServiceResponse:
        """run의 append-only structured event timeline을 조회한다 (#496).

        호출 전에 run_id 검증·존재 확인·ownership 게이팅이 끝나 있어야 한다
        (``/builds/{run_id}/stages``와 동일하게 dispatch route adapter가 먼저
        처리한다). 반환은 항상 chronological ascending이다 — ``tail=True``도
        최신 ``limit``개를 고르되 정렬 자체는 뒤집지 않는다(#496 ordering 정책).
        """
        events = self._event_store.list_for_run(run_id, limit=limit, tail=tail)
        body: dict[str, JsonValue] = {
            "run_id": run_id,
            "events": cast(JsonValue, [events_service.event_to_json(e) for e in events]),
        }
        return ServiceResponse(200, body)
