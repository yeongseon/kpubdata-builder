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
