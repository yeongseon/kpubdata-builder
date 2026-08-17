"""업로드 메타데이터 모델 (#498)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UploadMetadata:
    """저장된 업로드의 안전한(secret-free) 메타데이터.

    원시 content는 별도로 ``get_content()`` 를 통해서만 얻는다 — 목록/조회
    응답에는 절대 포함하지 않는다.

    속성:
        upload_id: 서버가 발급한 불투명한 식별자(``upl_<hex32>``). 사용자가
            지정한 filename/path가 아니다.
        format: 업로드 시점에 검증된 포맷(csv/json/jsonl/parquet).
        encoding: 업로드 시점에 검증된 인코딩(parquet은 의미 없음).
        size_bytes: content 크기.
        original_filename: 사용자가 보낸 원본 파일명(표시 전용, sanitize됨).
            파일시스템 경로로 절대 쓰이지 않는다.
        created_at: ISO-8601 UTC 생성 시각.
    """

    upload_id: str
    format: str
    encoding: str
    size_bytes: int
    original_filename: str | None
    created_at: str
