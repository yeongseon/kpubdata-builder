"""BuildSpec 데이터 모델 (Medallion 재구성: 기존 spec.py에서 분리).

이 모듈은 빌드 선언을 표현하는 불변 데이터 클래스와 JSON 호환 타입 별칭만
정의한다. YAML 로딩/파싱은 loader.py, 검증은 validator.py에 분리되어 있다.

주요 구성:
    - SourceRef: 원본 데이터 소스 참조
    - ExportTarget: 출력 대상 정의
    - BuildSpec: 전체 빌드 선언 모델
"""

from __future__ import annotations

import re
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


#: 지원하는 SourceRef.kind 값 (#498). 알 수 없는 kind는 loader가 즉시 거부한다.
SOURCE_KINDS: tuple[str, ...] = ("public_api", "file", "url")

#: url kind에서 허용하는 HTTP method (#498 P0 — GET, Auth=None만 지원).
SOURCE_URL_METHODS: tuple[str, ...] = ("GET",)

#: file kind에서 허용하는 업로드 포맷 (#498 P0). Excel/ZIP은 범위 밖이다.
SOURCE_FILE_FORMATS: tuple[str, ...] = ("csv", "json", "jsonl", "parquet")

#: url kind에서 허용하는 응답 포맷 (#498 P0). 지정하지 않으면 Content-Type로 추론한다.
SOURCE_URL_FORMATS: tuple[str, ...] = ("json", "jsonl", "csv")

#: 서버가 발급하는 upload_id 형태 (#498). ``POST /uploads`` 만 이 형태의 id를
#: 만든다 — 사용자가 임의 문자열을 upload_id로 넣어 filesystem/조회를 조작할 수
#: 없도록 loader/validator가 이 패턴으로 형태를 고정한다.
UPLOAD_ID_PATTERN: re.Pattern[str] = re.compile(r"^upl_[a-f0-9]{32}$")


@dataclass(frozen=True)
class SourceRef:
    """Canonical 소스 참조 — Public API / File / URL 세 kind를 표현한다 (#498).

    ``kind`` 로 세 가지 소스를 구분한다. 세 kind는 서로 다른 field 조합을 쓰지만
    ``alias``/``schema`` 는 공통이다(공통 alias/schema 정책, #498).

    - ``"public_api"``(기본값, 하위 호환): 기존 kpubdata provider/dataset 참조.
      ``provider``/``dataset``/``params`` 를 쓴다. kind를 생략한 기존 BuildSpec은
      항상 이 kind로 해석된다 — 기존 Public API BuildSpec 동작은 그대로 유지된다.
    - ``"file"``: 사전에 ``POST /uploads`` 로 업로드된 파일을 가리킨다. 로컬
      파일시스템 경로 대신 서버가 발급한 불투명한 ``upload_id`` 만 참조한다 —
      filename/path를 직접 참조하지 않는다(경로 주입 방지).
    - ``"url"``: 안전한 GET(Auth=None) HTTP(S) 소스를 가리킨다. arbitrary header나
      POST/PUT/PATCH는 계약에 없다 — 필드 자체가 없어 표현할 수 없다.

    속성:
        provider: provider 식별자. ``kind="public_api"`` 에서만 사용.
        dataset: dataset 식별자. ``kind="public_api"`` 에서만 사용.
        params: list 호출에 전달할 원시 파라미터. ``kind="public_api"`` 에서만 사용.
        alias: 조립 단계에서 사용할 사용자 정의 소스 이름 (모든 kind 공통).
        schema: 소스 스키마 계약. None 이면 Silver 검증을 생략한다 (모든 kind 공통,
            하위 호환, #437).
        kind: ``"public_api"``(기본) | ``"file"`` | ``"url"``.
        upload_id: 업로드된 파일의 서버 발급 식별자. ``kind="file"`` 에서만 사용.
        format: 파일/응답 파싱 포맷. ``kind="file"`` 은 필수(csv/json/jsonl/parquet),
            ``kind="url"`` 은 선택(json/jsonl/csv, 생략 시 Content-Type로 추론).
        encoding: 텍스트 디코딩에 쓸 인코딩. ``kind="file"`` 에서만 의미가 있다
            (parquet 제외 — 바이너리 포맷이라 인코딩이 없다). 기본값 ``"utf-8"``.
        endpoint: 안전하게 fetch할 절대 URL. ``kind="url"`` 에서만 사용.
        method: HTTP method. ``kind="url"`` 에서만 사용하며 P0는 ``"GET"`` 만
            허용한다.
    """

    provider: str = ""
    dataset: str = ""
    params: dict[str, JsonValue] = field(default_factory=dict)
    alias: str = ""
    schema: SchemaContract | None = None
    kind: str = "public_api"
    upload_id: str = ""
    format: str = ""
    encoding: str = "utf-8"
    endpoint: str = ""
    method: str = "GET"


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
class RangeRule:
    """숫자 컬럼의 최소/최대 범위 규칙 (#486).

    자유형 Python/eval 대신 typed rule로 표현한다. min/max는 포함(inclusive)
    경계다. null 값은 range 위반으로 계산하지 않는다 — missing/null 여부는
    ``max_null_ratio``가 별도로 담당한다(역할 분리).

    속성:
        column: 대상 컬럼명.
        min: 허용 최소값(포함). None이면 하한을 검사하지 않는다.
        max: 허용 최대값(포함). None이면 상한을 검사하지 않는다.
        severity: 위반 시 심각도 — ``"warn"``(기본) | ``"fail"``.
    """

    column: str
    min: float | None = None
    max: float | None = None
    severity: str = "warn"


@dataclass(frozen=True)
class CompareColumnsRule:
    """두 컬럼 간 비교 규칙 (#486).

    자유형 expression/eval을 금지하고, 제한된 operator 집합만 허용한다.
    두 컬럼 모두 null이 아닌 행만 평가 대상이다(비교 불가능한 행을 자동
    통과로 세지 않는다).

    속성:
        left: 왼쪽 컬럼명.
        operator: ``eq``/``ne``/``gt``/``gte``/``lt``/``lte`` 중 하나.
        right: 오른쪽 컬럼명.
        severity: 위반 시 심각도 — ``"warn"``(기본) | ``"fail"``.
    """

    left: str
    operator: str
    right: str
    severity: str = "warn"


@dataclass(frozen=True)
class QualityPolicy:
    """데이터 품질 임계 정책 (#446, QG-3; range/compare_columns/severity는 #486).

    Silver 통계(row_count/null_counts/duplicate_rate)와 테이블 자체(range/
    compare_columns)에 대한 임계. 초과 시 위반으로 구조화된 QualityCheckResult로
    보고하고(quality.evaluator), severity에 따라 WARN(계속 진행) 또는 FAIL(Gold
    진입 전 소스 실패)로 게이트한다.

    기존 3개 필드(max_duplicate_rate/max_null_ratio/min_rows)의 타입과 기본
    severity(``"warn"``)는 #446 시절 그대로 유지한다 — 하위 호환. 명시적 FAIL이
    필요하면 대응하는 ``*_severity`` 필드를 함께 선언한다.

    속성:
        max_duplicate_rate: 허용 최대 중복 행 비율 (0.0~1.0). 초과 시 위반.
        max_duplicate_rate_severity: 위반 시 심각도. 기본 ``"warn"``.
        max_null_ratio: 컬럼별 허용 최대 null 비율 (``{컬럼명: 비율}``).
        max_null_ratio_severity: 컬럼별 위반 심각도 override (``{컬럼명: severity}``).
            선언되지 않은 컬럼은 기본 ``"warn"``.
        min_rows: 최소 행 수. 미만 시 위반.
        min_rows_severity: 위반 시 심각도. 기본 ``"warn"``.
        range: 컬럼별 최소/최대 범위 규칙 목록 (#486).
        compare_columns: 컬럼 간 비교 규칙 목록 (#486).
    """

    max_duplicate_rate: float | None = None
    max_duplicate_rate_severity: str = "warn"
    max_null_ratio: dict[str, float] = field(default_factory=dict)
    max_null_ratio_severity: dict[str, str] = field(default_factory=dict)
    min_rows: int | None = None
    min_rows_severity: str = "warn"
    range: tuple[RangeRule, ...] = ()
    compare_columns: tuple[CompareColumnsRule, ...] = ()


@dataclass(frozen=True)
class JoinSpec:
    """두 source를 결합하는 equi-join 계약 (#506).

    구조(참조 alias 존재, type/severity 어휘)는 spec.validator가 파싱 직후 검증한다.
    join key의 실제 존재 여부와 dtype 호환성은 파싱 시점엔 알 수 없다(Silver 스키마가
    나와야 확인 가능) — 그 부분은 spec.validator가 아니라 orchestrator의 빌드 파이프라인
    검증 게이트(런타임)에서 확인한다. 완료조건 문서의 "validate 단계에서 확인"은 이
    구조 검증과 파이프라인 게이트를 합쳐 부르는 표현으로 읽어야 한다.

    속성:
        left: 왼쪽 source의 alias (BuildSpec.sources[].alias 참조).
        right: 오른쪽 source의 alias.
        left_key: 왼쪽 테이블의 join key 컬럼명.
        right_key: 오른쪽 테이블의 join key 컬럼명.
        type: join 종류. "inner" | "left" (초기 범위, #506).
        on_duplicate_key: 양쪽 key 모두 중복 값을 가져 many-to-many로 행이
            폭증할 수 있을 때의 처리. "warn"(기본, manifest에 경고만 기록) |
            "fail"(빌드 실패). QualityPolicy의 severity 관례를 따른다.
    """

    left: str
    right: str
    left_key: str
    right_key: str
    type: str = "inner"
    on_duplicate_key: str = "warn"


@dataclass(frozen=True)
class CompositionSpec:
    """여러 source를 하나의 Gold dataset으로 조립하는 계약 (#506).

    초기 범위는 두 source 단일 equi-join으로 제한한다(3개 이상 join graph는
    제외 범위) — 그래서 리스트가 아니라 단일 JoinSpec만 받는다.

    속성:
        name: 결합 결과 Gold dataset 이름. gold/{name}/ 출력 디렉터리 세그먼트로도
            쓰이므로 다른 source의 output key(alias 또는 provider.dataset)와
            겹치면 안 된다.
        join: 결합에 사용할 단일 JoinSpec.
    """

    name: str
    join: JoinSpec


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
        composition: 두 source를 join해 하나의 Gold dataset으로 조립하는 계약.
            None이면 기존과 동일하게 source별 독립 Gold만 생성한다 (하위 호환, #506).

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
    composition: CompositionSpec | None = None

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
    "CompositionSpec",
    "ExportTarget",
    "JoinSpec",
    "JsonPrimitive",
    "JsonValue",
    "SOURCE_FILE_FORMATS",
    "SOURCE_KINDS",
    "SOURCE_URL_FORMATS",
    "SOURCE_URL_METHODS",
    "SourceRef",
    "SplitSpec",
    "UPLOAD_ID_PATTERN",
]
