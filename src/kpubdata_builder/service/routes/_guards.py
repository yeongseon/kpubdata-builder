"""Run route가 공유하는 존재·소유권 guard."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from ...stages._path_safety import ensure_within
from .. import ownership as ownership_module
from ..auth import Principal
from ..responses import ServiceResponse

if TYPE_CHECKING:
    from ..app import BuilderService


def _read_manifest_ownership(service: BuilderService, run_id: str) -> tuple[str | None, str | None]:
    manifest_path = service._output_root / run_id / "manifest.json"
    if not manifest_path.exists():
        return None, None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return cast(str | None, data.get("created_by")), cast(str | None, data.get("owner_id"))
    except Exception:
        return None, None


def check_ownership(
    service: BuilderService, run_id: str, principal: Principal
) -> ServiceResponse | None:
    created_by, owner_id = _read_manifest_ownership(service, run_id)
    if ownership_module.ownership_allows(
        created_by=created_by, owner_id=owner_id, principal=principal
    ):
        return None
    return ServiceResponse(403, {"error": "forbidden: not run owner"})


def check_run_exists(service: BuilderService, run_id: str) -> ServiceResponse | None:
    run_dir = service._output_root / run_id
    ensure_within(service._output_root, run_dir, label="run directory")
    if run_dir.is_dir():
        return None
    return ServiceResponse(404, {"error": f"run not found: {run_id}"})


def check_active_run_access(
    service: BuilderService, run_id: str, principal: Principal
) -> ServiceResponse | None:
    """``/builds/{run_id}/events`` 전용 존재·소유권 판정 (#496 follow-up: BLOCKER).

    ``check_run_exists``/``check_ownership``은 run directory와 manifest.json이
    이미 있다고 가정한다 - 하지만 async job은 ``run_submitted``가 worker
    enqueue *이전에* event store에 기록되고(``BuilderService.submit_build``의
    ``on_accept`` hook), run directory는 worker가 ``BuildContext.create()``에서,
    manifest는 run이 끝나야 비로소 만들어진다. 그 사이(queued/running) 구간에는
    ``check_run_exists``가 404를, ownership이 켜져 있으면 ``check_ownership``이
    (manifest 부재로 created_by/owner_id 둘 다 None) fail-closed 403을 반환해
    events polling이 아예 불가능해진다.

    판정 순서(run directory 존재 여부는 전환 기준으로 쓰지 않는다 - manifest만
    본다):
        1. manifest.json이 있으면 기존 ``check_ownership``(manifest 기반,
           stable ``owner_id`` 우선) 경로를 그대로 쓴다 - completed run은
           registry에 terminal entry가 남아있어도 이 경로로만 판정한다.
        2. manifest가 없고 async job registry(``AsyncBuildExecutor``, 프로세스
           메모리 상주)에 snapshot이 있으면(active든, enqueue 실패 등으로
           manifest 없이 종결된 terminal이든) 그 snapshot의 stable
           ``owner_id``로 ownership을 판정한다(#505 canonical identity -
           ``created_by``/``Principal.label``은 legacy fallback일 뿐이고,
           ``ownership_allows``에 그대로 넘겨 우선순위 판단을 위임한다).
        3. 둘 다 없으면 404.

    snapshot의 ``owner_id``는 ``BuilderService.submit_build``가 registry
    저장 목적으로만 전달한다 - wire 응답이나 run_build/source resolver에는
    전달되지 않는다(#498 async owner propagation 한계 유지, snapshot 자체에는
    이 필드가 있지만 ``BuildJobSnapshot.to_body()``가 절대 노출하지 않는다).

    ``/manifest``, ``/stages`` 등 다른 route는 여전히 persisted run만 다루므로
    이 함수를 쓰지 않는다 - 영향 범위를 events endpoint로 좁게 유지한다.
    """
    run_dir = service._output_root / run_id
    ensure_within(service._output_root, run_dir, label="run directory")
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        return check_ownership(service, run_id, principal)
    snapshot = service._async_builds.get(run_id)
    if snapshot is not None:
        if ownership_module.ownership_allows(
            created_by=snapshot.created_by, owner_id=snapshot.owner_id, principal=principal
        ):
            return None
        return ServiceResponse(403, {"error": "forbidden: not run owner"})
    return ServiceResponse(404, {"error": f"run not found: {run_id}"})
