"""File/URL ingestion 오류 계층 (#498)."""

from __future__ import annotations

from ..errors import BuildError


class IngestionError(BuildError):
    """file/url source의 fetch 또는 파싱이 실패했음을 나타낸다 (#498).

    SSRF 차단, 응답 크기 초과, 빈/손상된 content, 지원하지 않는 형식 등
    사용자에게 그대로 보여줘도 안전한 메시지만 담는다 — raw 응답 본문이나
    내부 스택은 포함하지 않는다.
    """


__all__ = ["IngestionError"]
