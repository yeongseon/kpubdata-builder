"""내보내기 도구 레지스트리와 플러그인 등록 API (#13, #325).

이 모듈은 kind 문자열 → exporter 팩토리/인스턴스 매핑을 보관하고, 제3자 exporter를
세 가지 방식으로 등록할 수 있게 한다.

방식 A (factory 등록, ADR 0004 권고):
    register_exporter_factory("csv", CsvExporter)

방식 B (인스턴스 등록, 레거시 호환):
    register_exporter_instance(CsvExporter())

방식 C (entry points 자동 발견):
    외부 패키지가 pyproject.toml에 entry point를 선언하면, 명시적으로
    load_entry_point_exporters()를 호출해 발견·등록한다 (import 시 임의의
    서드파티 코드를 실행하지 않도록 자동 로드는 하지 않는다).

        [project.entry-points."kpubdata_builder.exporters"]
        csv = "my_package:CsvExporter"
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points

from .base import BaseExporter

EXPORTER_ENTRY_POINT_GROUP = "kpubdata_builder.exporters"

# kind -> (factory 함수, 인스턴스 캐시)
# factory는 호출할 때마다 새 인스턴스를 반환해야 한다.
ExporterFactory = Callable[[], BaseExporter]
_EXPORTER_FACTORIES: dict[str, ExporterFactory] = {}

# 레거시 호환을 위한 인스턴스 레지스트리 (ADR 0004 이전 방식)
EXPORTER_REGISTRY: dict[str, BaseExporter] = {}


def register_exporter_factory(
    kind: str, factory: ExporterFactory, *, override: bool = False
) -> None:
    """exporter 팩토리를 kind 문자열로 레지스트리에 등록한다 (#325).

    팩토리는 호출할 때마다 새 exporter 인스턴스를 반환해야 한다.
    이 방식은 ADR 0004의 권고안을 따르며, 중복 등록은 기본적으로 거부된다.

    매개변수:
        kind: exporter 식별자 (예: "csv", "parquet").
        factory: 인자 없이 BaseExporter 인스턴스를 반환하는 callable.
        override: 같은 kind가 이미 있을 때 덮어쓸지 여부.

    예외:
        ValueError: 같은 kind가 이미 등록되어 있고 override가 False인 경우.

    예시:
        >>> register_exporter_factory("csv", CsvExporter)
        >>> exporter = get_exporter("csv")
    """
    if kind in _EXPORTER_FACTORIES and not override:
        raise ValueError(f"exporter kind {kind!r} is already registered")
    _EXPORTER_FACTORIES[kind] = factory


def register_exporter_instance(exporter: BaseExporter, *, override: bool = False) -> None:
    """exporter 인스턴스를 그 name으로 레지스트리에 등록한다.

    .. deprecated::
        ADR 0004에 따라 register_exporter_factory 사용을 권장한다.
        이 함수는 하위 호환성을 위해 유지된다.

    매개변수:
        exporter: 등록할 BaseExporter 인스턴스.
        override: 같은 이름이 이미 있을 때 덮어쓸지 여부.

    예외:
        ValueError: 같은 이름이 이미 등록되어 있고 override가 False인 경우.
    """
    name = exporter.name
    if name in EXPORTER_REGISTRY and not override:
        raise ValueError(f"exporter {name!r} is already registered")
    EXPORTER_REGISTRY[name] = exporter


# 하위 호환성을 위한 별칭
register_exporter = register_exporter_instance


def get_exporter(name: str) -> BaseExporter:
    """등록된 exporter를 kind 이름으로 조회한다.

    factory 레지스트리를 우선 조회하고, 없으면 인스턴스 레지스트리(레거시)를
    조회한다.

    매개변수:
        name: exporter kind 문자열.

    반환값:
        BaseExporter: 등록된 인스턴스 (factory에서 새로 생성하거나 캐스된 인스턴스).

    예외:
        KeyError: 등록되지 않은 이름인 경우.
    """
    # factory 레지스트리 우선 (ADR 0004)
    if name in _EXPORTER_FACTORIES:
        return _EXPORTER_FACTORIES[name]()
    # 레거시 인스턴스 레지스트리 fallback
    if name in EXPORTER_REGISTRY:
        return EXPORTER_REGISTRY[name]
    registered = sorted(set(_EXPORTER_FACTORIES) | set(EXPORTER_REGISTRY))
    raise KeyError(f"unknown exporter kind: {name!r}; registered: {registered}")


def load_entry_point_exporters(*, override: bool = False) -> list[str]:
    """entry point 그룹에서 외부 exporter 플러그인을 발견·등록한다.

    각 entry point는 BaseExporter 인스턴스 또는 인자 없이 생성 가능한 클래스를
    가리켜야 한다. 클래스면 factory로 등록하고, 인스턴스면 인스턴스 레지스트리에
    등록한다.

    매개변수:
        override: 기존 등록을 덮어쓸지 여부.

    반환값:
        list[str]: 등록된 exporter 이름 목록(이름 순).
    """
    registered: list[str] = []
    for entry_point in entry_points(group=EXPORTER_ENTRY_POINT_GROUP):
        loaded = entry_point.load()
        if isinstance(loaded, type):
            # 클래스: factory로 등록 (ADR 0004 권고)
            if not issubclass(loaded, BaseExporter):
                raise TypeError(
                    f"entry point {entry_point.name!r} did not resolve to a BaseExporter subclass"
                )
            register_exporter_factory(entry_point.name, loaded, override=override)
            registered.append(entry_point.name)
        else:
            # 인스턴스: 레거시 방식으로 등록
            exporter = loaded
            if not isinstance(exporter, BaseExporter):
                raise TypeError(
                    f"entry point {entry_point.name!r} did not resolve to a BaseExporter"
                )
            register_exporter_instance(exporter, override=override)
            registered.append(exporter.name)
    return sorted(registered)


def clear_exporter_registry() -> None:
    """모든 exporter 등록을 초기화한다 (#325).

    주로 테스트에서 사용한다. 테스트 간 exporter 등록 누수를 방지하기 위해
    factory와 인스턴스 레지스트리를 모두 비운다.
    """
    _EXPORTER_FACTORIES.clear()
    EXPORTER_REGISTRY.clear()


__all__ = [
    "EXPORTER_ENTRY_POINT_GROUP",
    "EXPORTER_REGISTRY",
    "clear_exporter_registry",
    "get_exporter",
    "load_entry_point_exporters",
    "register_exporter",
    "register_exporter_factory",
    "register_exporter_instance",
]
