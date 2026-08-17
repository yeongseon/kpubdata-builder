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


def _strip_windows_extended_prefix(path: Path) -> Path:
    """Windows ``\\\\?\\`` 확장 경로 프리픽스를 제거해 일관되게 비교할 수 있게 한다.

    Windows에서 ``Path.resolve()``는 대상에 대한 파일 핸들을 열 수 있으면
    ``\\\\?\\`` 프리픽스가 붙은 확장 경로를 돌려주고, 동시 디스크 I/O로 핸들
    열기가 일시적으로 실패하면 프리픽스 없는 수동 정규화 경로로 폴백한다.
    같은 실제 경로인데도 어느 쪽이 폴백을 탔는지에 따라 문자열이 달라져,
    여러 source를 스레드 풀에서 동시에 쓰는 상황(#247)에서
    ``is_relative_to`` 비교가 간헐적으로 잘못된 traversal 오탐을 냈다(#506
    composition 작업 중 재현·확인). POSIX에서는 경로가 이 프리픽스로
    시작할 수 없으므로 no-op이다.
    """
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + text[len("\\\\?\\UNC\\") :])
    if text.startswith("\\\\?\\"):
        return Path(text[len("\\\\?\\") :])
    return path


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
    resolved_root = _strip_windows_extended_prefix(root.resolve())
    resolved_target = _strip_windows_extended_prefix(target.resolve())
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
    resolved_base = _strip_windows_extended_prefix(base_dir.resolve())
    resolved_target = _strip_windows_extended_prefix(candidate.resolve())
    if not resolved_target.is_relative_to(resolved_base):
        raise PathTraversalError(
            f"output path {os.fspath(relative_path)!r} escapes base directory "
            f"{resolved_base} (resolved to {resolved_target})"
        )
    return candidate
