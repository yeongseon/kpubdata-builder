"""디렉터리 원자적 교체 유틸 atomic_replace_dir 동작 검증 (#180)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.stages._atomic import atomic_replace_dir


def _make_dir(parent: Path, name: str, content: str) -> Path:
    d = parent / name
    d.mkdir()
    _ = (d / "data.txt").write_text(content, encoding="utf-8")
    return d


def test_replaces_into_empty_destination(tmp_path: Path) -> None:
    tmp_dir = _make_dir(tmp_path, ".tmp_new", "new")
    final_dir = tmp_path / "final"

    atomic_replace_dir(tmp_dir, final_dir)

    assert (final_dir / "data.txt").read_text(encoding="utf-8") == "new"
    assert not tmp_dir.exists()


def test_swaps_over_existing_destination(tmp_path: Path) -> None:
    final_dir = _make_dir(tmp_path, "final", "old")
    tmp_dir = _make_dir(tmp_path, ".tmp_new", "new")

    atomic_replace_dir(tmp_dir, final_dir)

    assert (final_dir / "data.txt").read_text(encoding="utf-8") == "new"
    assert not tmp_dir.exists()
    # 백업(.old)이 남지 않아야 한다.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".old")]
    assert leftovers == []


def test_restores_existing_data_when_swap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 신규 배치(rename) 실패 시 기존 데이터가 손실되지 않고 복구되어야 한다.
    final_dir = _make_dir(tmp_path, "final", "old")
    tmp_dir = _make_dir(tmp_path, ".tmp_new", "new")

    original_rename = Path.rename
    state = {"calls": 0}

    def flaky_rename(self: Path, target: Path) -> Path:
        # 첫 rename(기존→백업)은 통과, 두 번째 rename(tmp→final)에서 실패시킨다.
        state["calls"] += 1
        if state["calls"] == 2:
            raise OSError("disk full")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    with pytest.raises(OSError, match="disk full"):
        atomic_replace_dir(tmp_dir, final_dir)

    monkeypatch.undo()
    # 기존 데이터가 제자리로 복구되어 있어야 한다.
    assert final_dir.exists()
    assert (final_dir / "data.txt").read_text(encoding="utf-8") == "old"


def test_restores_backup_even_when_final_dir_partially_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tmp→final rename 실패 후 final_dir가 이미 존재하더라도 backup을 복구해야 한다 (#224).
    # 실제 OS에서는 rename 실패 후 final_dir가 생기진 않지만, 방어 코드가 올바른지 검증한다.
    final_dir = _make_dir(tmp_path, "final", "old")
    tmp_dir = _make_dir(tmp_path, ".tmp_new", "new")

    original_rename = Path.rename
    state = {"calls": 0}

    def flaky_rename(self: Path, target: Path) -> Path:
        state["calls"] += 1
        if state["calls"] == 2:
            # rename 실패를 시뮬레이션하기 전에 final_dir를 빈 디렉터리로 만들어
            # "부분 배치" 상황을 재현한다.
            target.mkdir(exist_ok=True)
            raise OSError("partial rename failure")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    with pytest.raises(OSError, match="partial rename failure"):
        atomic_replace_dir(tmp_dir, final_dir)

    monkeypatch.undo()
    # final_dir는 백업에서 복구된 원본 데이터를 담고 있어야 한다.
    assert final_dir.exists()
    assert (final_dir / "data.txt").read_text(encoding="utf-8") == "old"
