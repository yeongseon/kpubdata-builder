"""내보내기 도구 레지스트리와 플러그인 등록 API (#13).

이 모듈은 kind 문자열 → exporter 인스턴스 매핑을 보관하고, 제3자 exporter를
두 가지 방식으로 등록할 수 있게 한다.

방식 A (코드 내 등록):
    register_exporter(MyExporter())

방식 B (entry points 자동 발견):
    외부 패키지가 pyproject.toml에 entry point를 선언하면, 명시적으로
    load_entry_point_exporters()를 호출해 발견·등록한다 (import 시 임의의
    서드파티 코드를 실행하지 않도록 자동 로드는 하지 않는다).

        [project.entry-points."kpubdata_builder.exporters"]
        csv = "my_package:CsvExporter"
"""

from __future__ import annotations

from importlib.metadata import entry_points

from .base import BaseExporter

EXPORTER_ENTRY_POINT_GROUP = "kpubdata_builder.exporters"

EXPORTER_REGISTRY: dict[str, BaseExporter] = {}


def register_exporter(exporter: BaseExporter, *, override: bool = False) -> None:
    """exporter 인스턴스를 그 name으로 레지스트리에 등록한다.

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


def get_exporter(name: str) -> BaseExporter:
    """등록된 exporter를 kind 이름으로 조회한다.

    매개변수:
        name: exporter kind 문자열.

    반환값:
        BaseExporter: 등록된 인스턴스.

    예외:
        KeyError: 등록되지 않은 이름인 경우.
    """
    if name not in EXPORTER_REGISTRY:
        raise KeyError(f"unknown exporter kind: {name!r}; registered: {sorted(EXPORTER_REGISTRY)}")
    return EXPORTER_REGISTRY[name]


def load_entry_point_exporters(*, override: bool = False) -> list[str]:
    """entry point 그룹에서 외부 exporter 플러그인을 발견·등록한다.

    각 entry point는 BaseExporter 인스턴스 또는 인자 없이 생성 가능한 클래스를
    가리켜야 한다. 클래스면 인스턴스화한 뒤 등록한다.

    매개변수:
        override: 기존 등록을 덮어쓸지 여부.

    반환값:
        list[str]: 등록된 exporter 이름 목록(이름 순).
    """
    registered: list[str] = []
    for entry_point in entry_points(group=EXPORTER_ENTRY_POINT_GROUP):
        loaded = entry_point.load()
        exporter = loaded() if isinstance(loaded, type) else loaded
        if not isinstance(exporter, BaseExporter):
            raise TypeError(f"entry point {entry_point.name!r} did not resolve to a BaseExporter")
        register_exporter(exporter, override=override)
        registered.append(exporter.name)
    return sorted(registered)


__all__ = [
    "EXPORTER_ENTRY_POINT_GROUP",
    "EXPORTER_REGISTRY",
    "get_exporter",
    "load_entry_point_exporters",
    "register_exporter",
]
