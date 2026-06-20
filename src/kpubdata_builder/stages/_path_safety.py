"""stage 산출물 영속화를 위한 공용 경로 안전성 유틸 (#46/#47 review).

bronze/silver/gold persist가 공유하던 경로 세그먼트 검증과 워크스페이스 포함
검사를 한곳에 모은다. 세그먼트 규칙이 바뀌어도 한 곳만 수정하면 된다.

주요 함수:
    - validate_path_segment: 워크스페이스를 벗어날 수 있는 세그먼트 거부
    - ensure_within: 해석된 경로가 루트 아래에 있는지 검증
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..errors import PathTraversalError

_SAFE_PATH_SEGMENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def validate_path_segment(value: str, *, field_name: str) -> None:
    """워크스페이스를 벗어날 수 있는 경로 세그먼트를 거부한다.

    매개변수:
        value: 검증할 경로 세그먼트.
        field_name: 오류 메시지에 사용할 필드명.

    예외:
        ValueError: 비어 있거나 허용되지 않은 문자가 포함된 경우.
    """
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{field_name} must not have leading/trailing whitespace")
    if not _SAFE_PATH_SEGMENT.match(value):
        raise ValueError(
            f"{field_name} contains unsafe characters: {value!r}. "
            "Only alphanumeric, dot, hyphen, and underscore are allowed."
        )


def ensure_within(root: Path, target: Path, *, label: str) -> None:
    """target(해석 후)이 root(해석 후) 아래에 포함되는지 검증한다.

    문자열 prefix 비교(`startswith`)는 `/tmp/root2`가 `/tmp/root`를 통과시키는
    오탐이 가능하므로, 해석된 경로에 ``Path.is_relative_to``를 사용한다.

    매개변수:
        root: 허용된 루트 디렉터리.
        target: 검증할 대상 경로.
        label: 오류 메시지에 사용할 대상 설명.

    예외:
        ValueError: target이 root 밖으로 벗어나는 경우.
    """
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if not resolved_target.is_relative_to(resolved_root):
        raise ValueError(f"Resolved {label} {resolved_target} escapes output_root {resolved_root}")


def safe_output_path(base_dir: Path, relative_path: str | os.PathLike[str]) -> Path:
    """base_dir 아래로 한정된 출력 경로를 만들어 반환한다 (#210).

    exporter는 spec에서 온 사용자 제어 output_path로 파일을 기록한다. 절대 경로
    (``/etc/passwd``)나 상위 이동(``../../etc``)이 섞이면 build 워크스페이스 밖
    임의 위치에 파일이 생성/덮어쓰기될 수 있으므로, 결합·해석된 경로가 base_dir
    내부인지 확인한 뒤에만 경로를 돌려준다.

    매개변수:
        base_dir: 출력이 반드시 머물러야 하는 기준 디렉터리.
        relative_path: base_dir 기준의 (사용자 제어) 출력 경로.

    반환값:
        Path: base_dir 아래에 있음이 검증된 결합 경로(원본 형태 그대로).

    예외:
        PathTraversalError: 결합·해석된 경로가 base_dir를 벗어나는 경우.
    """
    candidate = base_dir / relative_path
    resolved_base = base_dir.resolve()
    resolved_target = candidate.resolve()
    if not resolved_target.is_relative_to(resolved_base):
        raise PathTraversalError(
            f"output path {os.fspath(relative_path)!r} escapes base directory "
            f"{resolved_base} (resolved to {resolved_target})"
        )
    return candidate
