"""Gold 단계(#47): SilverDataset → export-ready GoldPackage + persist 검증."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import polars as pl
import pytest

from kpubdata_builder.spec import ExportTarget, JsonValue
from kpubdata_builder.stages.bronze.models import BronzeArtifact, utc_now
from kpubdata_builder.stages.gold import (
    ExportPlan,
    GoldPackage,
    build_gold_package,
    persist_gold_package,
)
from kpubdata_builder.stages.silver import SilverDataset, build_silver_dataset


def _silver(records: tuple[Mapping[str, JsonValue], ...]) -> SilverDataset:
    bronze = BronzeArtifact(
        source_key="datago.apt_trade",
        raw_records=tuple(dict(record) for record in records),
        fetched_at=utc_now(),
    )
    return build_silver_dataset(bronze)


class TestBuildGoldPackage:
    def test_packages_silver_table_with_export_plan(self) -> None:
        silver = _silver(({"id": "1", "amount": 1000}, {"id": "2", "amount": 2500}))
        exports = (ExportTarget(kind="jsonl", output_path="data.jsonl"),)

        package = build_gold_package(silver, dataset_name="apt_trade", exports=exports)

        assert isinstance(package, GoldPackage)
        assert package.dataset_name == "apt_trade"
        assert package.table.to_dicts() == silver.table.to_dicts()
        assert isinstance(package.export_plan, ExportPlan)
        assert package.export_plan.targets == exports
        assert package.source_silver == "datago.apt_trade"

    def test_empty_export_plan_when_no_targets(self) -> None:
        package = build_gold_package(_silver(({"id": "1"},)), dataset_name="d")

        assert package.export_plan.targets == ()


class TestPersistGoldPackage:
    def test_writes_parquet_and_package_json(self, tmp_path: Path) -> None:
        silver = _silver(({"id": "1", "amount": 1000}, {"id": "2", "amount": 2500}))
        package = build_gold_package(
            silver,
            dataset_name="apt_trade",
            exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
        )

        result = persist_gold_package(package, output_root=tmp_path, run_id="run1")

        assert result.table_path.exists()
        assert result.package_path.exists()
        assert pl.read_parquet(result.table_path).to_dicts() == package.table.to_dicts()

        meta = cast(
            dict[str, JsonValue], json.loads(result.package_path.read_text(encoding="utf-8"))
        )
        assert meta["dataset_name"] == "apt_trade"
        assert meta["row_count"] == 2
        export_plan = cast(dict[str, JsonValue], meta["export_plan"])
        targets = cast(list[dict[str, JsonValue]], export_plan["targets"])
        assert targets[0]["kind"] == "jsonl"
        assert meta["source_silver"] == "datago.apt_trade"

    def test_rejects_unsafe_dataset_name(self, tmp_path: Path) -> None:
        package = build_gold_package(_silver(({"id": "1"},)), dataset_name="../escape")

        with pytest.raises(ValueError, match="dataset_name"):
            _ = persist_gold_package(package, output_root=tmp_path, run_id="run1")

    def test_rejects_unsafe_run_id(self, tmp_path: Path) -> None:
        package = build_gold_package(_silver(({"id": "1"},)), dataset_name="apt_trade")

        with pytest.raises(ValueError, match="run_id"):
            _ = persist_gold_package(package, output_root=tmp_path, run_id="../escape")
