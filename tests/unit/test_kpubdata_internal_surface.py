"""``verify/runner.py`` 가 의존하는 kpubdata **내부** 심볼을 고정한다.

builder 의 verify 는 kpubdata 의 ``make verify`` 를 재구현하면서 공개 API 가 아닌
것들을 직접 import 한다 — executor 의 함수들, spec 모델, transport, config. 이들은
``kpubdata.__all__`` 에 없으므로 minor 릴리스에서 이름이나 위치가 바뀌어도
kpubdata 쪽에서는 파괴적 변경이 아니다.

그 경계를 없애는 것은 두 저장소에 걸친 설계 결정이라 여기서 하지 않는다. 대신
**언제 깨지는지를 앞당긴다** — 업그레이드 후 실제 verify 실행 중에 ImportError 를
보는 대신, CI 에서 무엇이 움직였는지 이름을 붙여 실패하게 한다.

심볼을 추가로 쓰기 시작하면 이 목록에도 넣는다. 목록에 없는 의존은 이 테스트가
지켜 주지 않는다.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib

import pytest

_RUNNER = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "kpubdata_builder"
    / "verify"
    / "runner.py"
)

#: (모듈, 이름, 기대 시그니처 또는 None)
_PINNED: tuple[tuple[str, str, str | None], ...] = (
    ("kpubdata.core.executor", "SpecExecutor", None),
    (
        "kpubdata.core.executor",
        "check_payload_error",
        "(spec: 'SpecDefinition', payload: 'dict[str, object]') -> 'None'",
    ),
    (
        "kpubdata.core.executor",
        "extract_items",
        "(spec: 'SpecDefinition', payload: 'dict[str, object]') -> 'list[dict[str, object]]'",
    ),
    (
        "kpubdata.core.executor",
        "extract_total_count",
        "(spec: 'SpecDefinition', payload: 'dict[str, object]') -> 'int | None'",
    ),
    ("kpubdata.core.spec", "SpecDefinition", None),
    ("kpubdata.core.spec", "ExampleSpec", None),
    ("kpubdata.transport.http", "HttpTransport", None),
    ("kpubdata.config", "KPubDataConfig", None),
)


class TestThePinnedSymbolsStillExist:
    @pytest.mark.parametrize(("module_name", "symbol", "signature"), _PINNED)
    def test_symbol_is_importable(
        self, module_name: str, symbol: str, signature: str | None
    ) -> None:
        module = importlib.import_module(module_name)

        assert hasattr(module, symbol), (
            f"{module_name}.{symbol} 이 사라졌다. builder 의 verify 가 이걸 직접 쓴다 — "
            "kpubdata 쪽에서는 공개 API 가 아니므로 파괴적 변경이 아니다."
        )

    @pytest.mark.parametrize(("module_name", "symbol", "signature"), _PINNED)
    def test_signature_is_unchanged(
        self, module_name: str, symbol: str, signature: str | None
    ) -> None:
        if signature is None:
            pytest.skip("클래스는 시그니처를 고정하지 않는다 — 존재만 확인한다")
        obj = getattr(importlib.import_module(module_name), symbol)

        assert str(inspect.signature(obj)) == signature


class TestTheListMatchesWhatTheRunnerActuallyImports:
    """목록이 낡으면 이 테스트가 지켜 주는 범위가 조용히 줄어든다."""

    def _imported_internals(self) -> set[tuple[str, str]]:
        tree = ast.parse(_RUNNER.read_text(encoding="utf-8"))
        found: set[tuple[str, str]] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if not node.module.startswith("kpubdata."):
                continue
            # 공개 예외/모델은 kpubdata.__all__ 에 있으므로 고정 대상이 아니다.
            if node.module == "kpubdata.exceptions" or node.module == "kpubdata.core.models":
                continue
            for alias in node.names:
                found.add((node.module, alias.name))
        return found

    def test_every_internal_import_is_pinned(self) -> None:
        pinned = {(module, symbol) for module, symbol, _ in _PINNED}

        unpinned = self._imported_internals() - pinned

        assert not unpinned, (
            f"runner.py 가 고정되지 않은 kpubdata 내부 심볼을 쓴다: {sorted(unpinned)}. "
            "_PINNED 에 추가하라 — 그러지 않으면 업그레이드가 CI 가 아니라 실행 중에 깨진다."
        )
