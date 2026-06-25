"""CSV 내보내기 도구 구현.

이 모듈은 ArtifactDataset의 레코드를 RFC 4180 스타일의 CSV 파일로 직렬화하는
exporter를 제공한다. 컬럼은 artifact.schema가 있으면 그 순서를, 없으면 레코드에서
처음 등장한 순서를 따른다. 콤마/따옴표/개행이 포함된 값은 stdlib csv가 자동으로
인용 처리한다.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import tempfile
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget, JsonValue
from .base import BaseExporter, ExportResult, ensure_output_dir


def _resolve_columns(artifact: ArtifactDataset) -> list[str]:
    """CSV 헤더로 사용할 컬럼 순서를 결정한다.

    schema가 선언되어 있으면 그 키 순서를 우선하고, 레코드에만 존재하는
    추가 필드도 뒤에 포함한다. 결과는 입력에 대해 결정적이다.
    """
    columns: dict[str, None] = {}
    if artifact.schema:
        for key in artifact.schema:
            columns.setdefault(key, None)
    for record in artifact.records:
        for key in record:
            columns.setdefault(key, None)
    return list(columns.keys())


# 스프레드시트가 수식으로 해석하는 선두 문자 집합.
# 셀 값이 이 문자로 시작하면 앞에 홑따옴표를 붙여 수식 실행을 막는다 (CSV 인젝션 대응, CWE-1236).
_FORMULA_TRIGGER_CHARS = frozenset("=+-@\t\r")


def _format_cell(value: JsonValue) -> str:
    """단일 셀 값을 CSV 문자열로 변환한다.

    None은 빈 문자열, bool은 소문자 JSON 표기, 중첩 list/dict는 결정적 JSON
    문자열로 직렬화한다. 그 외 스칼라는 str()로 변환한다.

    문자열이 스프레드시트 수식 트리거 문자(``=``, ``+``, ``-``, ``@``,
    탭, 캐리지 리턴)로 시작하면 앞에 홑따옴표 ``'``를 붙여 수식 실행을
    막는다(CSV 인젝션 대응, CWE-1236).  숫자·bool·None 등 비문자열 값은
    변경하지 않는다.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value
        if text and text[0] in _FORMULA_TRIGGER_CHARS:
            text = "'" + text
        return text
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class CsvExporter(BaseExporter):
    """레코드를 CSV로 기록하는 내보내기 도구.

    예시:
        >>> CsvExporter().name
        'csv'
    """

    @property
    def name(self) -> str:
        """내보내기 도구 이름을 반환한다."""
        return "csv"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """표준 레코드를 CSV 파일로 내보낸다.

        매개변수:
            artifact: CSV로 직렬화할 레코드 묶음.
            target: 출력 경로와 옵션을 담은 내보내기 대상.
            output_dir: 빌드 기준 출력 디렉터리.

        반환값:
            ExportResult: 생성된 CSV 파일 메타데이터.

        예외:
            ExportError: 파일 쓰기에 실패한 경우.
        """
        destination = ensure_output_dir(output_dir, target.output_path)
        columns = _resolve_columns(artifact)

        buffer = io.StringIO()
        if columns:
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(columns)
            for record in artifact.records:
                writer.writerow([_format_cell(record.get(column)) for column in columns])
        content = buffer.getvalue()

        try:
            fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp_name, destination)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        except OSError as exc:
            raise ExportError(f"Failed to export CSV artifact to {destination}: {exc}") from exc

        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )
