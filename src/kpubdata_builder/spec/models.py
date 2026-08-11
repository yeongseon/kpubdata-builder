"""BuildSpec 데이터 모델 (Medallion 재구성: 기존 spec.py에서 분리).

이 모듈은 빌드 선언을 표현하는 불변 데이터 클래스와 JSON 호환 타입 별칭만
정의한다. YAML 로딩/파싱은 loader.py, 검증은 validator.py에 분리되어 있다.

주요 구성:
    - SourceRef: 원본 데이터 소스 참조
    - ExportTarget: 출력 대상 정의
    - BuildSpec: 전체 빌드 선언 모델
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class SchemaContract:
    """소스 스키마 계약 — Silver 검증/정규화 규칙 (#437).

    BuildSpec 의 ``sources[].schema`` 선언이 이 모델로 파싱되고,
    orchestrator/preview 가 build_silver_dataset 의 인자로 전달한다.
    이전까지는 게이트가 존재했지만 통과 조건이 없었다 (항상 ok).

    속성:
        required: 필수 컬럼 목록. validate_table 의 required_columns 로 전달.
        dtypes: 컬럼별 기대 dtype (문자열, ``_NAMED_DTYPES`` 키). validate_table 의
            column_dtypes 로 전달.
        casts: 정규화 시 적용할 컬럼별 캐스팅 (문자열). normalize_table 의 casts 로
            전달. 캐스팅으로 인한 null 손실은 audit=True 로 감지돼 TabularError 로
            표면화된다 (#188).
    """

    required: tuple[str, ...] = ()
    dtypes: dict[str, str] = field(default_factory=dict)
    casts: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceRef:
    """kpubdata의 정규화된 소스 쿼리를 가리키는 참조.

    속성:
        provider: provider 식별자.
        dataset: dataset 식별자.
        params: list 호출에 전달할 원시 파라미터.
        alias: 조립 단계에서 사용할 사용자 정의 소스 이름.
        schema: 소스 스키마 계약. None 이면 Silver 검증을 생략한다 (하위 호환, #437).
    """

    provider: str
    dataset: str
    params: dict[str, JsonValue] = field(default_factory=dict)
    alias: str = ""
    schema: SchemaContract | None = None


@dataclass(frozen=True)
class ExportTarget:
    """빌드를 위한 구체적인 내보내기 대상 정의.

    속성:
        kind: exporter 레지스트리 키.
        output_path: output_dir 기준 상대 출력 경로.
        options: exporter 전용 선택 옵션.
    """

    kind: str
    output_path: str
    options: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class SplitSpec:
    """데이터셋을 명명된 분할로 나누는 방법 정의 (#38).

    속성:
        mode: 분할 방식. "ratio"는 비율 기반(train/val/test), "key"는 컬럼 값
            기반(연도/지역/카테고리)으로 나눈다.
        ratios: ratio 모드에서 분할 이름 → 비율 매핑 (합이 1.0이어야 한다).
        key: key 모드에서 분할 기준이 되는 컬럼 이름.
        seed: ratio 모드의 결정적 셔플 시드.
    """

    mode: str
    ratios: dict[str, float] = field(default_factory=dict)
    key: str = ""
    seed: int = 0


@dataclass(frozen=True)
class PiiPolicy:
    """PII 검출 정책 (#441, QG-1).

    속성:
        mode: ``"block"``(기본, 검출 시 빌드 실패) | ``"warn"``(manifest 경고만) |
            ``"allow"``(통과). ``publish=True`` 스펙에서는 ``allow``를 금지한다.
        allow_columns: 모드와 무관하게 스캔을 건너뛸 컬럼명 목록(오탐 해제용).
    """

    mode: str = "block"
    allow_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualityPolicy:
    """데이터 품질 임계 정책 (#446, QG-3).

    Silver 통계(row_count/null_counts/duplicate_rate)에 대한 임계. 초과 시
    위반으로 보고한다(orchestrator 가 warn/fail 처리).

    속성:
        max_duplicate_rate: 허용 최대 중복 행 비율 (0.0~1.0). 초과 시 위반.
        max_null_ratio: 컬럼별 허용 최대 null 비율 (``{컬럼명: 비율}``).
        min_rows: 최소 행 수. 미만 시 위반.
    """

    max_duplicate_rate: float | None = None
    max_null_ratio: dict[str, float] = field(default_factory=dict)
    min_rows: int | None = None


@dataclass(frozen=True)
class BuildSpec:
    """데이터셋 산출물을 위한 선언적 빌드 명세.

    속성:
        dataset_id: 데이터셋의 전역 식별자.
        title: 사람이 읽는 제목.
        description: 빌드 목적과 데이터 설명.
        sources: 입력 소스 목록.
        exports: 출력 대상 목록.
        metadata: 산출물에 실을 임의 메타데이터.
        publish: 빌드 후 게시까지 수행할지 여부.
        splits: 데이터셋 분할 정의. 없으면 분할하지 않는다.
        pii: PII 스캔 정책. None이면 스캔을 생략한다 (하위 호환, #441).
        license: 데이터셋 라이선스/이용허락범위 (SPDX 식별자 또는 자유 텍스트).
            ``publish=True`` 시 반드시 선언해야 한다 (#443). kpubdata 가 라이선스
            메타데이터를 제공하지 않으므로 사용자 명시 선언만이 출처이다.
        quality: 데이터 품질 임계 정책. None이면 검사를 생략한다 (하위 호환, #446).

    예시:
        >>> BuildSpec.from_yaml("specs/sample.yaml")
    """

    dataset_id: str
    title: str
    description: str
    sources: tuple[SourceRef, ...]
    exports: tuple[ExportTarget, ...]
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    publish: bool = False
    splits: SplitSpec | None = None
    pii: PiiPolicy | None = None
    license: str | None = None
    quality: QualityPolicy | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> BuildSpec:
        """YAML 파일에서 BuildSpec을 로드한다.

        .. deprecated::
            Use ``load_spec(path)`` directly to keep model and I/O separate.

        매개변수:
            path: YAML 파일 경로.

        반환값:
            BuildSpec: 파싱 완료된 불변 명세 객체.
        """
        import warnings

        warnings.warn(
            "BuildSpec.from_yaml() is deprecated, use load_spec() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        # models <-> loader 순환 import를 피하기 위한 지연 import.
        from .loader import load_spec

        return load_spec(Path(path))


__all__ = [
    "BuildSpec",
    "ExportTarget",
    "JsonPrimitive",
    "JsonValue",
    "SourceRef",
    "SplitSpec",
]
