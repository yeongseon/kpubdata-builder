"""JSONL 내보내기 도구 스텁 구현.

이 모듈은 ArtifactDataset의 레코드를 줄바꿈 구분 JSON(JSONL) 형식으로
직렬화하는 기본 exporter를 제공한다.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from .base import BaseExporter, ExportResult, ensure_output_dir


class JsonlExporter(BaseExporter):
    """레코드를 줄바꿈 구분 JSON으로 기록하는 내보내기 도구.

    예시:
        >>> JsonlExporter().name
        'jsonl'
    """

    @property
    def name(self) -> str:
        """내보내기 도구 이름을 반환한다."""
        return "jsonl"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """표준 레코드를 JSONL 파일로 내보낸다.

        매개변수:
            artifact: JSONL로 직렬화할 레코드 묶음.
            target: 출력 경로와 옵션을 담은 내보내기 대상.
            output_dir: 빌드 기준 출력 디렉터리.

        반환값:
            ExportResult: 생성된 JSONL 파일 메타데이터.

        예외:
            ExportError: 파일 쓰기에 실패한 경우.
        """
        destination = ensure_output_dir(output_dir, target.output_path)
        try:
            fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    for record in artifact.records:
                        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                        f.write("\n")
                os.replace(tmp_name, destination)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        except OSError as exc:
            raise ExportError(f"Failed to export JSONL artifact to {destination}: {exc}") from exc

        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )
