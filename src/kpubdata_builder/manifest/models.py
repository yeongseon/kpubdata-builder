"""빌드 매니페스트 데이터 모델 (Medallion 재구성: 기존 manifest.py에서 분리).

이 모듈은 빌드 실행의 입력/출력/경고/오류/행 수 같은 감사 정보를 담는
불변 데이터 클래스만 정의한다. 디스크 기록은 writer.py가 담당한다.

주요 구성:
    - BuildManifest: 실행 요약 데이터 클래스
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class BuildManifest:
    """빌드 감사를 위한 실행 요약 산출물.

    속성:
        build_id: 실행 식별자.
        started_at: 실행 시작 시각.
        finished_at: 실행 종료 시각.
        inputs: 입력 파일 또는 소스 식별자 목록.
        outputs: 생성된 결과물 경로 목록.
        warnings: 경고 메시지 목록.
        errors: 실패 또는 부분 실패 메시지 목록.
        row_counts: 단계별 또는 산출물별 레코드 수 요약.
    """

    build_id: str
    started_at: datetime
    finished_at: datetime
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    row_counts: dict[str, int] = field(default_factory=dict)


__all__ = ["BuildManifest"]
