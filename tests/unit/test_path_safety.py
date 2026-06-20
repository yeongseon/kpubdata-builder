"""공용 경로 안전성 유틸(stages/_path_safety) 검증 (#46/#47 review)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.errors import PathTraversalError
from kpubdata_builder.stages._path_safety import (
    ensure_within,
    safe_output_path,
    validate_path_segment,
)


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


class TestSafeOutputPath:
    def test_allows_simple_relative_path(self, tmp_path: Path) -> None:
        result = safe_output_path(tmp_path, "train.parquet")
        assert result == tmp_path / "train.parquet"

    def test_allows_nested_relative_path(self, tmp_path: Path) -> None:
        result = safe_output_path(tmp_path, "data/train.parquet")
        assert result == tmp_path / "data" / "train.parquet"

    @pytest.mark.parametrize(
        "evil",
        ["../escape.parquet", "../../etc/passwd", "data/../../etc/passwd", "a/b/../../../x"],
    )
    def test_rejects_parent_traversal(self, tmp_path: Path, evil: str) -> None:
        with pytest.raises(PathTraversalError, match="escapes base directory"):
            _ = safe_output_path(tmp_path, evil)

    def test_rejects_absolute_path(self, tmp_path: Path) -> None:
        # 절대 경로는 base_dir / "/etc/passwd" 결합 시 base를 무시하고 그대로 빠져나간다.
        with pytest.raises(PathTraversalError, match="escapes base directory"):
            _ = safe_output_path(tmp_path, "/etc/passwd")

    def test_is_export_error_subclass(self, tmp_path: Path) -> None:
        # 기존 except ExportError 경로에서도 잡히도록 ExportError를 상속한다.
        from kpubdata_builder.errors import ExportError

        with pytest.raises(ExportError):
            _ = safe_output_path(tmp_path, "../oops")
