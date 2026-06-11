"""디렉터리 단위 원자적 교체 유틸 (#180).

기존 디렉터리를 ``shutil.rmtree`` 후 ``rename``하면, 삭제와 rename 사이에서
실패할 경우 기존 데이터도 신규 데이터도 남지 않아 데이터가 유실된다.

이를 막기 위해 기존 디렉터리를 ``.old`` 백업으로 먼저 rename한 뒤 신규 디렉터리를
제자리로 rename하고, 성공했을 때만 백업을 삭제한다. 두 rename은 동일 파일시스템
에서 원자적이며, 신규 배치 단계에서 실패하면 백업을 원위치로 복구한다.

주요 함수:
    - atomic_replace_dir: tmp 디렉터리를 최종 경로로 원자적으로 교체
"""

from __future__ import annotations

import shutil
from pathlib import Path


def atomic_replace_dir(tmp_dir: Path, final_dir: Path) -> None:
    """``tmp_dir``를 ``final_dir`` 위치로 원자적으로 교체한다.

    ``final_dir``가 없으면 단순 rename으로 끝낸다. 존재하면 기존 디렉터리를
    고유한 ``.old`` 백업으로 rename → 신규 디렉터리 rename → 백업 삭제 순서로
    교체하며, 신규 디렉터리 rename이 실패하면 백업을 복구한 뒤 예외를 전파한다.

    매개변수:
        tmp_dir: 최종 위치로 옮길 임시 디렉터리.
        final_dir: 산출물이 놓일 최종 경로.
    """
    if not final_dir.exists():
        tmp_dir.rename(final_dir)
        return

    # tmp_dir 이름(mkdtemp 기반)으로 백업 경로를 고유하게 만들어 이전 크래시가
    # 남긴 stale 백업과 충돌하지 않게 한다.
    backup = final_dir.with_name(f"{final_dir.name}.{tmp_dir.name}.old")
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)

    final_dir.rename(backup)  # 원자적: 기존 데이터를 백업으로 이동
    try:
        tmp_dir.rename(final_dir)  # 원자적: 신규 데이터를 제자리로
    except BaseException:
        # 신규 배치 실패 → 기존 데이터를 원위치로 복구한다.
        if not final_dir.exists():
            backup.rename(final_dir)
        raise
    shutil.rmtree(backup, ignore_errors=True)


__all__ = ["atomic_replace_dir"]
