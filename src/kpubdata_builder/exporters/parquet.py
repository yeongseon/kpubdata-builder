"""Parquet 내보내기 도구 구현.

이 모듈은 ArtifactDataset의 레코드를 열(columnar) 기반 Parquet 파일로
직렬화하는 exporter를 제공한다. 직렬화는 저장소가 이미 사용하는 polars의
네이티브 Parquet writer에 위임하므로 별도의 pyarrow 의존성이 필요하지 않다.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from ..tabular.convert import records_to_dataframe
from .base import BaseExporter, ExportResult, ensure_output_dir


def _build_frame(artifact: ArtifactDataset) -> pl.DataFrame:
    """artifact를 Parquet 직렬화용 DataFrame으로 변환한다.

    레코드가 있으면 레코드에서 컬럼 구조를 추론하고, 레코드가 비어 있어도
    schema가 선언되어 있으면 0행이지만 컬럼 이름을 보존한 빈 테이블을 만든다.
    """
    if artifact.records:
        return records_to_dataframe(list(artifact.records))
    if artifact.schema:
        return pl.DataFrame(schema={name: pl.Utf8 for name in artifact.schema})
    return pl.DataFrame()


class ParquetExporter(BaseExporter):
    """레코드를 Parquet로 기록하는 내보내기 도구.

    예시:
        >>> ParquetExporter().name
        'parquet'
    """

    @property
    def name(self) -> str:
        """내보내기 도구 이름을 반환한다."""
        return "parquet"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """표준 레코드를 Parquet 파일로 내보낸다.

        매개변수:
            artifact: Parquet로 직렬화할 레코드 묶음.
            target: 출력 경로와 옵션을 담은 내보내기 대상.
            output_dir: 빌드 기준 출력 디렉터리.

        반환값:
            ExportResult: 생성된 Parquet 파일 메타데이터.

        예외:
            ExportError: 파일 쓰기에 실패한 경우.
        """
        destination = ensure_output_dir(output_dir, target.output_path)
        frame = _build_frame(artifact)
        try:
            frame.write_parquet(destination)
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise ExportError(f"Failed to export Parquet artifact to {destination}: {exc}") from exc

        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )
