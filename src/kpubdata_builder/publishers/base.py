"""원격/로컬 산출물 게시를 위한 기본 게시 도구 계약 (#28).

Exporter / Publisher 책임 경계:
    - Exporter: 파일 또는 구조를 **생성**한다 (kpubdata_builder.exporters).
    - Publisher: 생성된 산출물을 외부/로컬 destination에 **업로드/등록**한다.

이 모듈은 publisher 구현이 따라야 할 최소 인터페이스와, 게시 결과를 보고하는
PublishResult 값 객체를 정의한다.

주요 구성:
    - PublishResult: 게시 결과 메타데이터
    - BasePublisher: publisher 추상 기반 클래스
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PublishResult:
    """게시 결과 메타데이터.

    속성:
        publisher: 게시를 수행한 publisher 식별자.
        reference: 게시 위치 참조 (로컬 경로, URL, 레지스트리 ID 등).
        artifact_count: 게시된 산출물 개수.
        status: 게시 상태 ("ok" 등).
    """

    publisher: str
    reference: str
    artifact_count: int
    status: str = "ok"


class BasePublisher(ABC):
    """게시 백엔드를 위한 추상 인터페이스.

    구현체는 name 식별자와 publish 메서드를 제공해야 한다. publish는 파일을
    생성하지 않으며(그것은 Exporter의 책임), 이미 생성된 산출물 경로를 받아
    destination에 등록/업로드하고 PublishResult를 반환한다.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """게시 도구 식별자를 반환한다."""

    @property
    def expects_directory(self) -> bool:
        """publish가 개별 파일이 아닌 디렉터리(레이아웃)를 입력으로 기대하는지 여부.

        기본값은 ``False``로, 호출자는 개별 파일 경로들을 전달한다. Kaggle처럼
        ``dataset-metadata.json``이 포함된 디렉터리 단위를 요구하는 publisher는
        이를 ``True``로 재정의한다 (#176).
        """
        return False

    @abstractmethod
    def publish(self, artifact_paths: tuple[Path, ...], *, destination: str) -> PublishResult:
        """생성된 산출물 경로를 지정한 destination에 게시한다.

        매개변수:
            artifact_paths: 게시 대상 파일 경로 튜플.
            destination: 게시 대상 식별자 (로컬 경로, 원격 repo id 등).

        반환값:
            PublishResult: 게시 결과 메타데이터.
        """


__all__ = ["BasePublisher", "PublishResult"]
