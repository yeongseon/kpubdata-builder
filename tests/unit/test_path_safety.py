"""공용 경로 안전성 유틸(stages/_path_safety) 검증 (#46/#47 review)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.errors import PathTraversalError
from kpubdata_builder.stages._path_safety import (
    _strip_windows_extended_prefix,
    ensure_within,
    safe_output_path,
    validate_path_segment,
)


class TestStripWindowsExtendedPrefix:
    """Windows ``\\\\?\\`` 확장 경로 프리픽스 제거 (#506).

    멀티소스 병렬 빌드(#247)에서 ``Path.resolve()``가 root/target 중 한쪽에만
    이 프리픽스를 붙이는 경우가 있어(핸들 열기 성공 여부에 따라 갈림)
    ``ensure_within``이 간헐적으로 잘못된 traversal 오탐을 냈다. 순수 문자열
    함수라 플랫폼과 무관하게 테스트한다.
    """

    def test_strips_extended_prefix(self) -> None:
        result = _strip_windows_extended_prefix(Path("\\\\?\\C:\\a\\b"))
        assert str(result) == "C:\\a\\b"

    def test_strips_unc_extended_prefix(self) -> None:
        result = _strip_windows_extended_prefix(Path("\\\\?\\UNC\\server\\share"))
        # bare UNC share root은 pathlib이 anchor로 취급해 trailing backslash를
        # 붙여 문자열화한다(drive root "C:\\"와 동일한 관례) — 버그가 아니다.
        assert str(result) == "\\\\server\\share\\"

    def test_noop_without_prefix(self) -> None:
        plain = Path("C:\\a\\b")
        assert _strip_windows_extended_prefix(plain) == plain

    def test_noop_for_posix_path(self) -> None:
        plain = Path("/a/b")
        assert _strip_windows_extended_prefix(plain) == plain


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

    def test_tolerates_inconsistent_extended_prefix_between_root_and_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """root/target 중 한쪽에만 확장 프리픽스가 붙어도 동일 경로로 인식해야 한다 (#506).

        실제 Windows 환경에서는 ``root.resolve()``와 ``target.resolve()``가 서로 다른
        시점에 호출되며, 동시 I/O로 한쪽만 핸들 기반 확장 경로(``\\\\?\\``)를 얻고
        다른 쪽은 폴백하는 비대칭이 발생할 수 있다 — 이를 monkeypatch로 재현한다.
        """
        root = tmp_path / "root"
        root.mkdir()
        target = root / "run1" / "silver" / "sales"
        target.mkdir(parents=True)
        real_resolve = Path.resolve

        def fake_resolve(self: Path, strict: bool = False) -> Path:
            resolved = real_resolve(self, strict=strict)
            if self == target:
                return Path("\\\\?\\" + str(resolved))
            return resolved

        monkeypatch.setattr(Path, "resolve", fake_resolve)

        ensure_within(root, target, label="silver directory")  # 예외 없음

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
