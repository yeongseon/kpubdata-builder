"""HTTP 전송과 독립적인 서비스 응답 모델."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..spec import JsonValue


@dataclass(frozen=True)
class ServiceResponse:
    """상태 코드와 JSON 직렬화 가능한 응답 본문."""

    status_code: int
    body: dict[str, JsonValue]


@dataclass(frozen=True)
class FileResponse:
    """파일 서빙 응답."""

    status_code: int
    file_path: Path
    filename: str
