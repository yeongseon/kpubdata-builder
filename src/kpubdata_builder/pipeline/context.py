"""빌드 실행 컨텍스트 (#48).

하나의 빌드 실행을 식별하는 run_id, 출력 워크스페이스 루트, 대상 BuildSpec,
실행 시작 시각을 한데 묶는다.

주요 구성:
    - BuildContext: 단일 실행 컨텍스트
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..spec import BuildSpec

_SAFE_RUN_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _utc_now() -> datetime:
    """시간대 정보가 있는 UTC 현재 시각을 반환한다."""
    return datetime.now(tz=timezone.utc)


def _validate_run_id(run_id: str) -> None:
    """워크스페이스를 벗어날 수 있는 run_id를 거부한다.

    예외:
        ValueError: 비어 있거나 허용되지 않은 문자가 포함된 경우.
    """
    if not run_id or run_id != run_id.strip() or not _SAFE_RUN_ID.match(run_id):
        raise ValueError(
            f"run_id contains unsafe characters: {run_id!r}. "
            "Only alphanumeric, dot, hyphen, and underscore are allowed."
        )


@dataclass(frozen=True)
class BuildContext:
    """단일 빌드 실행에 대한 컨텍스트.

    속성:
        run_id: 실행 식별자 (출력 디렉터리 세그먼트로 사용).
        output_root: 실행 워크스페이스 루트.
        spec: 실행 대상 빌드 명세.
        started_at: 실행 시작 시각 (timezone-aware).
    """

    run_id: str
    output_root: Path
    spec: BuildSpec
    started_at: datetime

    @classmethod
    def create(
        cls,
        spec: BuildSpec,
        *,
        output_root: Path,
        run_id: str | None = None,
        started_at: datetime | None = None,
    ) -> BuildContext:
        """run_id를 검증/생성하여 BuildContext를 만든다.

        run_id를 생략하면 시작 시각 기반의 결정적 타임스탬프를 사용한다.

        예외:
            ValueError: run_id에 안전하지 않은 문자가 포함된 경우.
        """
        started = started_at or _utc_now()
        if started.tzinfo is None or started.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        resolved_run_id = run_id or started.strftime("%Y%m%dT%H%M%S%fZ")
        _validate_run_id(resolved_run_id)
        return cls(
            run_id=resolved_run_id,
            output_root=output_root,
            spec=spec,
            started_at=started,
        )
