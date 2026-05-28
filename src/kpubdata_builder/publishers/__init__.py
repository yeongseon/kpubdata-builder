"""산출물 게시 도구 레지스트리 (#28).

Exporter가 생성한 파일을 외부/로컬 destination에 등록·업로드하는 publisher를
kind 문자열로 찾을 수 있게 한다. HuggingFace/GitHub 등 원격 대상은 후속
이슈에서 추가한다.

주요 구성:
    - PublishResult: 게시 결과 메타데이터
    - BasePublisher: publisher 추상 기반 클래스
    - LocalPublisher: 로컬 레지스트리 등록 publisher
    - PUBLISHER_REGISTRY: name -> publisher 인스턴스 매핑
"""

from __future__ import annotations

from .base import BasePublisher, PublishResult
from .local import LocalPublisher

PUBLISHER_REGISTRY: dict[str, BasePublisher] = {
    "local": LocalPublisher(),
}

__all__ = [
    "PUBLISHER_REGISTRY",
    "BasePublisher",
    "LocalPublisher",
    "PublishResult",
]
