"""Hugging Face Hub 레이아웃 내보내기 도구 (#9).

이 모듈은 ArtifactDataset을 Hugging Face Hub 업로드 규격의 디렉터리 레이아웃으로
내보낸다. 데이터 파일(data/), 데이터셋 카드(README.md, YAML front matter 포함),
메타데이터(dataset_infos.json)를 생성한다. 실제 업로드는 수행하지 않는다.

레이아웃::

    {output_path}/
    ├── data/
    │   └── train-00000-of-00001.{parquet|jsonl}
    ├── README.md
    └── dataset_infos.json
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import yaml

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from ..tabular.convert import records_to_dataframe
from .base import BaseExporter, ExportResult

_SUPPORTED_FORMATS = ("parquet", "jsonl")


def _resolve_format(target: ExportTarget) -> str:
    """target.options에서 데이터 파일 형식을 결정한다(기본 parquet)."""
    raw = target.options.get("format", "parquet")
    fmt = raw if isinstance(raw, str) else "parquet"
    if fmt not in _SUPPORTED_FORMATS:
        raise ExportError(
            f"Unsupported huggingface data format: {fmt!r}. "
            f"Supported: {', '.join(_SUPPORTED_FORMATS)}"
        )
    return fmt


def _write_data_file(artifact: ArtifactDataset, data_dir: Path, fmt: str) -> Path:
    """data/ 아래에 단일 shard 데이터 파일을 기록하고 경로를 반환한다."""
    data_path = data_dir / f"train-00000-of-00001.{fmt}"
    if fmt == "parquet":
        records_to_dataframe(list(artifact.records)).write_parquet(data_path)
    else:
        content = "\n".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) for record in artifact.records
        )
        _ = data_path.write_text(f"{content}\n" if content else "", encoding="utf-8")
    return data_path


def _render_card(artifact: ArtifactDataset) -> str:
    """YAML front matter + Markdown 본문의 데이터셋 카드를 만든다."""
    metadata = artifact.metadata
    front_matter: dict[str, object] = {
        "language": [metadata.get("language", "ko")],
        "pretty_name": metadata.get("title", "dataset"),
    }
    if metadata.get("license"):
        front_matter["license"] = metadata["license"]
    front_yaml = yaml.safe_dump(front_matter, allow_unicode=True, sort_keys=True).strip()

    title = metadata.get("title", "dataset")
    description = metadata.get("description", "")
    body = [f"---\n{front_yaml}\n---", "", f"# {title}", ""]
    if description:
        body += [description, ""]
    body += ["## 출처", ""]
    if artifact.provenance:
        body += [f"- {entry}" for entry in artifact.provenance]
    else:
        body.append("공공데이터포털 (data.go.kr)")
    return "\n".join(body) + "\n"


def _dataset_infos(artifact: ArtifactDataset) -> dict[str, object]:
    """HF dataset_infos.json에 실을 메타데이터를 구성한다."""
    return {
        "features": dict(artifact.schema),
        "num_examples": len(artifact.records),
        "provenance": list(artifact.provenance),
        "metadata": dict(artifact.metadata),
    }


class HuggingFaceExporter(BaseExporter):
    """ArtifactDataset을 Hugging Face Hub 레이아웃으로 내보낸다.

    예시:
        >>> HuggingFaceExporter().name
        'huggingface'
    """

    @property
    def name(self) -> str:
        """내보내기 도구 이름을 반환한다."""
        return "huggingface"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """ArtifactDataset을 HF 디렉터리 레이아웃으로 내보낸다.

        매개변수:
            artifact: 내보낼 산출물.
            target: output_path(레이아웃 루트)와 options(format)을 담은 대상.
            output_dir: 빌드 기준 출력 디렉터리.

        반환값:
            ExportResult: 레이아웃 루트 디렉터리와 총 바이트 크기.

        예외:
            ExportError: 지원하지 않는 형식이거나 파일 쓰기에 실패한 경우.
        """
        fmt = _resolve_format(target)
        hf_dir = output_dir / target.output_path
        data_dir = hf_dir / "data"
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            data_path = _write_data_file(artifact, data_dir, fmt)
            readme_path = hf_dir / "README.md"
            _ = readme_path.write_text(_render_card(artifact), encoding="utf-8")
            infos_path = hf_dir / "dataset_infos.json"
            _ = infos_path.write_text(
                json.dumps(_dataset_infos(artifact), ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise ExportError(f"Failed to export Hugging Face layout to {hf_dir}: {exc}") from exc

        total_size = sum(path.stat().st_size for path in (data_path, readme_path, infos_path))
        # HF exporter returns the layout directory (not a single file) since it
        # produces multiple files. Consumers should use output_path as a directory.
        return ExportResult(output_path=hf_dir, file_size=total_size, format=self.name)
