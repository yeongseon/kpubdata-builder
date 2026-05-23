"""공용 경로 안전성 유틸(stages/_path_safety) 검증 (#46/#47 review)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.stages._path_safety import ensure_within, validate_path_segment


class TestValidatePathSegment:
    @pytest.mark.parametrize("value", ["run1", "datago.apt_trade", "a_b-c.1"])
    def test_accepts_safe_segments(self, value: str) -> None:
        validate_path_segment(value, field_name="seg")  # 예외 없음

    @pytest.mark.parametrize("value", ["", "../escape", " leading", "trailing ", "a/b"])
    def test_rejects_unsafe_segments(self, value: str) -> None:
        with pytest.raises(ValueError, match="seg"):
            validate_path_segment(value, field_name="seg")


class TestEnsureWithin:
    def test_allows_target_inside_root(self, tmp_path: Path) -> None:
        target = tmp_path / "run1" / "bronze"
        ensure_within(tmp_path, target, label="bronze directory")  # 예외 없음

    def test_rejects_sibling_with_shared_prefix(self, tmp_path: Path) -> None:
        # /tmp/root2 가 /tmp/root prefix를 공유하지만 포함되지는 않는 오탐 케이스.
        root = tmp_path / "root"
        root.mkdir()
        sibling = tmp_path / "root2"
        sibling.mkdir()

        with pytest.raises(ValueError, match="escapes output_root"):
            ensure_within(root, sibling, label="dir")

    def test_rejects_parent_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()

        with pytest.raises(ValueError, match="escapes output_root"):
            ensure_within(root, root / ".." / "outside", label="dir")

    def test_rejects_escape_via_existing_symlink(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "linked"
        link.symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="escapes output_root"):
            ensure_within(root, link / "child", label="dir")
