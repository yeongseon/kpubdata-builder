"""재사용 가능한 빌드 템플릿 렌더링 (#14).

자주 쓰는 빌드 패턴을 `_template` 메타 블록 + ``{{ param }}`` 플레이스홀더를 가진
YAML 템플릿으로 정의하고, 파라미터만 바꿔 완성된 BuildSpec YAML을 생성한다.
외부 의존성 없이 stdlib 정규식 치환을 사용한다.

주요 함수:
    - render_template: 템플릿 + 파라미터 → 완성된 YAML 문자열
    - load_template: 템플릿을 렌더링해 BuildSpec으로 로드
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from ..errors import SpecLoadError
from .loader import parse_spec
from .models import BuildSpec

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _effective_params(template_meta: dict[str, object], params: dict[str, str]) -> dict[str, str]:
    """선언된 파라미터 기본값에 사용자 파라미터를 덮어써 최종 값을 만든다."""
    effective: dict[str, str] = {}
    declared = template_meta.get("parameters", {})
    if isinstance(declared, dict):
        for name, meta in declared.items():
            if isinstance(meta, dict) and "default" in meta:
                effective[str(name)] = str(meta["default"])
    for name, value in params.items():
        effective[name] = str(value)
    return effective


def render_template(path: str | Path, params: dict[str, str]) -> str:
    """템플릿 YAML을 파라미터로 렌더링해 완성된 YAML 문자열을 반환한다.

    매개변수:
        path: 템플릿 YAML 경로.
        params: 플레이스홀더에 채울 파라미터(선언된 기본값을 덮어쓴다).

    반환값:
        str: `_template` 블록이 제거되고 플레이스홀더가 치환된 YAML.

    예외:
        SpecLoadError: 파일 로드 실패, 최상위가 매핑이 아님, 또는 값이 없는
            플레이스홀더가 남은 경우.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
        loaded = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as exc:
        raise SpecLoadError(f"Failed to load template from {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise SpecLoadError(f"Failed to render template {path}: top-level YAML must be a mapping")

    data: dict[str, object] = dict(loaded)
    template_meta = data.pop("_template", {})
    meta = template_meta if isinstance(template_meta, dict) else {}
    effective = _effective_params(meta, params)

    # Substitute in the data structure directly to avoid YAML structure corruption.
    # Then re-dump so the output is valid YAML regardless of substitution values.
    missing: list[str] = []

    def _substitute_in_value(value: object) -> object:
        if isinstance(value, str):
            def _replace(match: re.Match[str]) -> str:
                name = match.group(1)
                if name not in effective:
                    missing.append(name)
                    return match.group(0)
                return effective[name]

            return _PLACEHOLDER.sub(_replace, value)
        if isinstance(value, list):
            return [_substitute_in_value(item) for item in value]
        if isinstance(value, dict):
            return {k: _substitute_in_value(v) for k, v in value.items()}
        return value

    substituted = _substitute_in_value(data)

    if missing:
        unique = ", ".join(sorted(set(missing)))
        raise SpecLoadError(f"Missing template parameter(s): {unique}")

    rendered = yaml.safe_dump(substituted, allow_unicode=True, sort_keys=False)
    return rendered


def load_template(path: str | Path, params: dict[str, str]) -> BuildSpec:
    """템플릿을 렌더링한 뒤 BuildSpec으로 파싱한다.

    매개변수:
        path: 템플릿 YAML 경로.
        params: 플레이스홀더 파라미터.

    반환값:
        BuildSpec: 렌더링·파싱된 빌드 명세.

    예외:
        SpecLoadError: 렌더링 또는 파싱 실패 시.
    """
    rendered = render_template(path, params)
    loaded = yaml.safe_load(rendered)
    if not isinstance(loaded, dict):
        raise SpecLoadError(f"Rendered template {path} is not a mapping")
    return parse_spec(loaded)


__all__ = ["load_template", "render_template"]
