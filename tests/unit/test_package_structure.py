"""Medallion 디렉터리 재구성(#44)이 만든 패키지 레이아웃을 잠그는 테스트.

flat 모듈(spec.py, validator.py, manifest.py)이 Medallion 구조의 패키지로
이동했는지, 공개 표면이 그대로 유지되는지, stages/silver·gold·pipeline
골격이 import 가능한지 검증한다.
"""

from __future__ import annotations

import importlib

import pytest


def _module(name: str) -> object:
    return importlib.import_module(name)


class TestSpecPackage:
    def test_models_submodule_exposes_dataclasses(self) -> None:
        models = _module("kpubdata_builder.spec.models")
        assert hasattr(models, "BuildSpec")
        assert hasattr(models, "SourceRef")
        assert hasattr(models, "ExportTarget")
        assert hasattr(models, "JsonValue")

    def test_loader_submodule_exposes_loaders(self) -> None:
        loader = _module("kpubdata_builder.spec.loader")
        assert hasattr(loader, "load_spec")
        assert hasattr(loader, "parse_spec")

    def test_validator_submodule_exposes_validate_spec(self) -> None:
        validator = _module("kpubdata_builder.spec.validator")
        assert hasattr(validator, "validate_spec")

    def test_spec_package_reexports_models_and_loaders(self) -> None:
        spec = _module("kpubdata_builder.spec")
        for name in (
            "BuildSpec",
            "SourceRef",
            "ExportTarget",
            "JsonValue",
            "load_spec",
            "parse_spec",
        ):
            assert hasattr(spec, name), name


class TestManifestPackage:
    def test_models_submodule_exposes_build_manifest(self) -> None:
        models = _module("kpubdata_builder.manifest.models")
        assert hasattr(models, "BuildManifest")

    def test_writer_submodule_exposes_writers(self) -> None:
        writer = _module("kpubdata_builder.manifest.writer")
        assert hasattr(writer, "manifest_writer")
        assert hasattr(writer, "write_manifest")

    def test_manifest_package_reexports(self) -> None:
        manifest = _module("kpubdata_builder.manifest")
        for name in ("BuildManifest", "manifest_writer", "write_manifest"):
            assert hasattr(manifest, name), name


class TestMedallionSkeleton:
    @pytest.mark.parametrize(
        "package",
        [
            "kpubdata_builder.pipeline",
            "kpubdata_builder.stages",
            "kpubdata_builder.stages.bronze",
            "kpubdata_builder.stages.silver",
            "kpubdata_builder.stages.gold",
        ],
    )
    def test_stage_packages_importable(self, package: str) -> None:
        assert _module(package) is not None


class TestPublicSurfacePreserved:
    def test_top_level_reexports_unchanged(self) -> None:
        pkg = _module("kpubdata_builder")
        for name in (
            "BuildSpec",
            "SourceRef",
            "ExportTarget",
            "BuildManifest",
            "manifest_writer",
            "validate_spec",
        ):
            assert hasattr(pkg, name), name


class TestBackwardCompatShim:
    def test_flat_validator_path_reexports_spec_validator(self) -> None:
        from kpubdata_builder import validator
        from kpubdata_builder.spec import validator as spec_validator

        assert validator.validate_spec is spec_validator.validate_spec

    def test_spec_and_manifest_are_packages(self) -> None:
        for name in ("kpubdata_builder.spec", "kpubdata_builder.manifest"):
            module = _module(name)
            assert hasattr(module, "__path__"), f"{name} should be a package"
