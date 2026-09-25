"""Gold artifact 가 디렉터리인 경우의 publish 준비 (#491 후속).

``kind: huggingface`` export 는 파일 하나가 아니라 레이아웃 디렉터리(README.md +
data/)를 만들고, manifest 의 output path 도 그 디렉터리를 가리킨다. 그런데
``resolve_gold_artifacts`` 가 ``is_file()`` 로만 검사해서 정상적으로 끝난 빌드가
``artifact_missing`` 으로 막혔다 — Studio 의 "huggingface export → publish" 흐름이
통째로 불가능했다.

우회하면 더 나빠진다. gold 파일을 개별로 올리면 YAML front matter 가 없는 gold
README 가 올라가서 HF 카드에 license 메타가 빠진다. 제대로 된 카드는 HF export
레이아웃 안에만 있다.
"""

from __future__ import annotations

from pathlib import Path

from kpubdata_builder.service.publish import (
    PublishIssue,
    ResolvedArtifacts,
    resolve_gold_artifacts,
)

_RUN = "run-hf"
_SOURCE = "datago.sample"


def _manifest(*output_paths: Path) -> dict[str, object]:
    return {
        "inputs": [_SOURCE],
        "outputs": [str(path) for path in output_paths],
    }


def _gold_dir(tmp_path: Path) -> Path:
    gold = tmp_path / _RUN / "gold" / _SOURCE
    gold.mkdir(parents=True)
    return gold


class TestDirectoryGoldArtifacts:
    def test_a_huggingface_layout_directory_resolves(self, tmp_path: Path) -> None:
        gold = _gold_dir(tmp_path)
        layout = gold / "hf"
        (layout / "data").mkdir(parents=True)
        (layout / "README.md").write_text("---\nlicense: cc-by-4.0\n---\n", encoding="utf-8")
        (layout / "data" / "train.parquet").write_bytes(b"parquet")

        resolved = resolve_gold_artifacts(tmp_path, _RUN, _manifest(layout))

        assert isinstance(resolved, ResolvedArtifacts), resolved
        assert resolved.paths == (layout,)

    def test_files_and_directories_can_be_mixed(self, tmp_path: Path) -> None:
        gold = _gold_dir(tmp_path)
        layout = gold / "hf"
        layout.mkdir()
        (layout / "README.md").write_text("x", encoding="utf-8")
        single = gold / "data.jsonl"
        single.write_text("{}\n", encoding="utf-8")

        resolved = resolve_gold_artifacts(tmp_path, _RUN, _manifest(layout, single))

        assert isinstance(resolved, ResolvedArtifacts), resolved
        assert set(resolved.paths) == {layout, single}

    def test_a_path_that_exists_as_neither_is_still_missing(self, tmp_path: Path) -> None:
        """존재하지 않는 경로는 여전히 fail-closed 여야 한다."""
        gold = _gold_dir(tmp_path)
        ghost = gold / "never-written"

        issue = resolve_gold_artifacts(tmp_path, _RUN, _manifest(ghost))

        assert isinstance(issue, PublishIssue)
        assert issue.code == "artifact_missing"
