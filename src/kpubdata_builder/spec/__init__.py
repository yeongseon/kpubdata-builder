"""BuildSpec 모델·로더 패키지 (Medallion 재구성).

models와 loader의 공개 심볼을 re-export 한다. validate_spec은 exporters에
의존하여 순환 import를 유발할 수 있으므로 여기서 노출하지 않고
``kpubdata_builder.spec.validator`` 서브모듈에서 직접 가져온다.

주요 구성:
    - SourceRef / ExportTarget / BuildSpec: 선언적 빌드 명세 모델
    - load_spec / parse_spec: YAML 로드 및 구조 파싱 진입점
"""

from __future__ import annotations

from .loader import load_spec, parse_spec
from .models import BuildSpec, ExportTarget, JsonPrimitive, JsonValue, SourceRef
from .template import load_template, render_template

__all__ = [
    "BuildSpec",
    "ExportTarget",
    "JsonPrimitive",
    "JsonValue",
    "SourceRef",
    "load_spec",
    "load_template",
    "parse_spec",
    "render_template",
]
