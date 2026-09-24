"""Upload 도메인 서비스 (#596, providers 에 이어 두 번째 조각).

``ProvidersService`` 와 같은 형태다 — **자기 의존성만 받고**, wire 계약은 건드리지
않는다. upload 도메인이 실제로 쓰는 것은 저장소 하나뿐이다.

저장소를 객체가 아니라 **호출 가능한 provider** 로 받는 이유가 있다: ``UploadRepository``
는 처음 필요할 때만 SQLite 파일을 만든다(#498). 생성자에서 저장소를 미리 받아버리면
upload 를 한 번도 쓰지 않는 워크스페이스에도 ``.service/uploads.sqlite3`` 가 생긴다 —
지연 생성이 실제로 지연되도록 호출 시점에 가져온다.
"""

from __future__ import annotations

from collections.abc import Callable

from kpubdata_builder.ingestion import IngestionError, parse_tabular_bytes
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.spec.models import SOURCE_FILE_FORMATS
from kpubdata_builder.uploads import UploadMetadata, UploadRepository

RepositoryProvider = Callable[[], UploadRepository]


def upload_metadata_body(metadata: UploadMetadata) -> dict[str, JsonValue]:
    """UploadMetadata를 wire JSON으로 변환한다 (#498). content는 절대 포함하지 않는다."""
    return {
        "upload_id": metadata.upload_id,
        "format": metadata.format,
        "encoding": metadata.encoding,
        "size_bytes": metadata.size_bytes,
        "original_filename": metadata.original_filename,
        "created_at": metadata.created_at,
    }


class UploadsService:
    """``kind="file"`` source 가 참조하는 업로드의 생성·조회·삭제 (#498)."""

    def __init__(self, *, repository: RepositoryProvider) -> None:
        self._repository = repository

    def create_upload(
        self,
        raw: bytes,
        *,
        format: str,  # noqa: A002 - 계약 필드명과 맞춘다
        encoding: str,
        original_filename: str | None,
        principal: Principal,
    ) -> ServiceResponse:
        """업로드 content를 저장하고 즉시 파싱 가능한지 검증한다 (#498).

        저장은 owner_id로 격리된다 — 나중에 BuildSpec의 ``kind="file"`` source가
        이 upload_id를 참조하려면 같은 principal이어야 한다(``build``/``preview``의
        resolver가 다시 확인한다). 파싱 가능성은 여기서 fail-fast로 확인한다 —
        나중에 build 시점에야 손상된 파일임을 알게 되는 것을 피한다.
        """
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        if format not in SOURCE_FILE_FORMATS:
            return ServiceResponse(
                400,
                {"error": f"format must be one of {SOURCE_FILE_FORMATS}, got {format!r}"},
            )
        try:
            _ = parse_tabular_bytes(raw, format=format, encoding=encoding)
        except IngestionError as exc:
            return ServiceResponse(400, {"error": str(exc)})
        try:
            metadata = self._repository().put(
                principal.owner_id,
                content=raw,
                format=format,
                encoding=encoding,
                original_filename=original_filename,
            )
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})
        return ServiceResponse(200, upload_metadata_body(metadata))

    def get_upload(self, upload_id: str, *, principal: Principal) -> ServiceResponse:
        """현재 principal 소유 업로드의 안전한 메타데이터만 반환한다 (content 제외)."""
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        metadata = self._repository().get_metadata(principal.owner_id, upload_id)
        if metadata is None:
            return ServiceResponse(404, {"error": f"upload not found: {upload_id}"})
        return ServiceResponse(200, upload_metadata_body(metadata))

    def delete_upload(self, upload_id: str, *, principal: Principal) -> ServiceResponse:
        """현재 principal 소유 업로드만 삭제한다."""
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        deleted = self._repository().delete(principal.owner_id, upload_id)
        if not deleted:
            return ServiceResponse(404, {"error": f"upload not found: {upload_id}"})
        return ServiceResponse(200, {"upload_id": upload_id, "deleted": True})


__all__ = ["UploadsService", "upload_metadata_body"]
