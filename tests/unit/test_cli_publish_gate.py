"""CLI publish 가 서비스 publish 와 같은 정책을 따르는지 (#491 후속).

두 가지가 갈려 있었다. CLI 는 ``validate_spec(spec)`` 만 불러서 게시 전용
규칙(license 선언)을 건너뛰었고, ``rglob("*")`` 로 artifacts_dir 아래 **모든**
파일을 올렸다 — artifacts_dir 에 run 루트를 넘기면 bronze 원본과 BuildSpec
snapshot 까지 함께 게시됐다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.cli import _is_non_publishable


class TestOnlyDatasetArtifactsArePublished:
    def _paths(self, root: Path) -> tuple[list[str], list[str]]:
        published: list[str] = []
        skipped: list[str] = []
        for relative in (
            "gold/datago.x/table.parquet",
            "gold/datago.x/hf/README.md",
            "out/data.jsonl",
            "bronze/datago.x/raw.jsonl",
            "silver/datago.x/table.parquet",
            "manifest.json",
            "buildspec.yaml",
        ):
            target = skipped if _is_non_publishable(root / relative, root) else published
            target.append(relative)
        return published, skipped

    def test_gold_and_exports_are_published(self, tmp_path: Path) -> None:
        published, _ = self._paths(tmp_path)

        assert published == [
            "gold/datago.x/table.parquet",
            "gold/datago.x/hf/README.md",
            "out/data.jsonl",
        ]

    def test_source_layers_and_workspace_files_are_not(self, tmp_path: Path) -> None:
        """bronze 원본은 게시 대상이 아니고, 크기도 gold 와 비교가 안 된다."""
        _, skipped = self._paths(tmp_path)

        assert skipped == [
            "bronze/datago.x/raw.jsonl",
            "silver/datago.x/table.parquet",
            "manifest.json",
            "buildspec.yaml",
        ]

    def test_a_directory_named_like_a_layer_deeper_down_is_kept(self, tmp_path: Path) -> None:
        """최상위 계층 디렉터리만 본다 — gold 안의 'bronze' 라는 이름까지 막지 않는다."""
        assert not _is_non_publishable(tmp_path / "gold" / "bronze" / "x.parquet", tmp_path)


class TestTheLicenseGateApplies:
    """HTTP publish 가 막는 spec 을 CLI 로는 올릴 수 있었다."""

    _NO_LICENSE = (
        "dataset_id: dataset.sample\n"
        "title: Sample\n"
        "description: D\n"
        "sources:\n"
        "  - provider: datago\n"
        "    dataset: air_quality\n"
        "exports:\n"
        "  - kind: jsonl\n"
        "    output_path: out/data.jsonl\n"
    )

    def test_publishing_without_a_license_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from kpubdata_builder.cli import main

        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text(self._NO_LICENSE, encoding="utf-8")
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "data.jsonl").write_text("{}\n", encoding="utf-8")

        exit_code = main(
            [
                "publish",
                str(spec_path),
                "--target",
                "local",
                "--destination",
                str(tmp_path / "dest"),
                "--artifacts-dir",
                str(artifacts),
            ]
        )

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "license is required when publish=true" in captured.err
