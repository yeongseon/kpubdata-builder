"""빌드 매니페스트 데이터 모델 (Medallion 재구성: 기존 manifest.py에서 분리).

이 모듈은 빌드 실행의 입력/출력/경고/오류/행 수 같은 감사 정보를 담는
불변 데이터 클래스만 정의한다. 디스크 기록은 writer.py가 담당한다.

주요 구성:
    - BuildManifest: 실행 요약 데이터 클래스
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..quality.models import QualityCheckResult, SchemaDriftFinding
from .environment import BuildEnvironment
from .provenance import SourceProvenance
from .schema_summary import SchemaSummary

# 매니페스트 직렬화 형식의 버전. 형식이 호환 불가하게 바뀌면 major를 올려, 소비자가
# 알 수 없는 형식을 안전하게 거부하거나 호환 계층을 분기할 수 있게 한다 (#211).
MANIFEST_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class BuildManifest:
    """빌드 감사를 위한 실행 요약 산출물.

    속성:
        build_id: 실행 식별자.
        started_at: 실행 시작 시각.
        finished_at: 실행 종료 시각.
        schema_version: 매니페스트 형식 버전 (semver). 기본값 MANIFEST_SCHEMA_VERSION.
        inputs: 입력 파일 또는 소스 식별자 목록.
        outputs: 생성된 결과물 경로 목록.
        warnings: 경고 메시지 목록.
        errors: 실패 또는 부분 실패 메시지 목록.
        row_counts: 단계별 또는 산출물별 레코드 수 요약.
        schema_summaries: 소스(산출물) 키별 스키마 요약. row_counts와 동일한 키를 사용한다.
        provenance: 소스별 상세 출처(fetch 시각/파라미터/레코드 수/체크섬) 목록.
        build_environment: 빌드를 생성한 실행 환경(Python/kpubdata/builder 버전).
        inputs_fingerprint: 입력 데이터 전체의 재현성 지문 ("sha256:..."). 입력이 없으면 None.
        created_by: 빌드를 요청한 principal의 display/legacy 라벨(#388). 사람이
            읽는 표시용이며 ownership 판정에는 하위 호환 폴백으로만 쓰인다.
        owner_id: canonical stable persistent owner identity (#505, additive).
            ``service.auth.compute_owner_id()``로 계산되며 raw claim(sub/email
            등)을 복원할 수 없다. legacy manifest는 이 필드가 없다 — reader는
            부재를 "owner_id 미지원 run"으로 해석해야 하며, ownership 판정은
            ``created_by``/label 비교로 폴백해야 한다(``principal_owns()`` 참조).
        quality_results: source_key별 구조화된 QualityCheckResult 목록 (#486, additive).
            PASS를 포함한 실제로 평가된 check만 담는다. legacy manifest는 이 필드가
            없다 — reader는 부재를 "미평가"(0건)로 해석해야 하며 "전부 PASS"로
            해석하면 안 된다.
        schema_drift: source_key별 구조화된 SchemaDriftFinding 목록 (#486, additive).
            drift 자체는 deterministic 감지 결과이며 PASS/WARN/FAIL 게이트에는
            관여하지 않는다.
    """

    build_id: str
    started_at: datetime
    finished_at: datetime
    schema_version: str = MANIFEST_SCHEMA_VERSION
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    row_counts: dict[str, int] = field(default_factory=dict)
    schema_summaries: dict[str, SchemaSummary] = field(default_factory=dict)
    provenance: tuple[SourceProvenance, ...] = ()
    build_environment: BuildEnvironment | None = None
    inputs_fingerprint: str | None = None
    created_by: str | None = None
    owner_id: str | None = None
    quality_results: dict[str, tuple[QualityCheckResult, ...]] = field(default_factory=dict)
    schema_drift: dict[str, tuple[SchemaDriftFinding, ...]] = field(default_factory=dict)


__all__ = ["MANIFEST_SCHEMA_VERSION", "BuildManifest"]
