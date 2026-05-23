"""하위 호환용 shim — validate_spec는 spec.validator로 이동했다 (#44).

기존 ``from kpubdata_builder.validator import validate_spec`` import 경로를
유지하기 위한 재노출 모듈이다. 신규 코드는 ``kpubdata_builder.spec.validator``를
직접 사용한다.
"""

from __future__ import annotations

from .spec.validator import validate_spec

__all__ = ["validate_spec"]
