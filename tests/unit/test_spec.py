"""BuildSpec 관련 데이터 클래스의 최소 생성 동작을 검증한다."""

from __future__ import annotations

from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef


def test_build_spec_instantiation() -> None:
    """최소 유효값만으로 BuildSpec을 생성할 수 있어야 한다."""
    # 데이터 클래스 기본 생성 경로가 깨지지 않았는지 확인한다.
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind="jsonl", output_path="out/data.jsonl"),),
    )

    assert spec.dataset_id == "dataset.sample"
