"""kpubdata SourceClient Protocol 적합성 테스트 (#226).

builder의 Bronze 단계는 kpubdata 호환 클라이언트에 다음 구조 계약을 요구한다:

    client.dataset(source_key).list(**params).items -> Iterable[dict]

이 테스트는 (1) 임의 객체가 Protocol을 구조적으로 만족할 수 있음을 고정하고,
(2) 실제 kpubdata 패키지가 설치돼 있으면 kpubdata.Client가 같은 구조 계약을
만족하는지(메서드/속성 존재 수준에서) 확인한다. 네트워크 호출은 하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze.build import SourceClient


class _ConformingResult:
    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return [{"id": "1"}]


class _ConformingDataset:
    def list(self, **params: JsonValue) -> _ConformingResult:
        return _ConformingResult()


class _ConformingClient:
    def dataset(self, source_key: str) -> _ConformingDataset:
        return _ConformingDataset()


class TestProtocolIsSatisfiable:
    def test_in_memory_client_satisfies_protocol(self) -> None:
        # SourceClient는 runtime_checkable이 아닐 수 있으므로, mypy(정적)와 별개로
        # 구조적 적합성을 런타임에서 호출 경로로 확인한다.
        client: SourceClient = _ConformingClient()
        dataset = client.dataset("datago.air_quality")
        result = dataset.list(region="seoul")
        items = list(result.items)
        assert items == [{"id": "1"}]

    def test_protocol_call_chain_shapes(self) -> None:
        client = _ConformingClient()
        # builder가 호출하는 정확한 경로: dataset(key).list(**params).items.
        assert hasattr(client, "dataset")
        dataset = client.dataset("k")
        assert hasattr(dataset, "list")
        result = dataset.list()
        assert hasattr(result, "items")
        for record in result.items:
            assert isinstance(record, dict)


class TestRealKpubdataClientConformance:
    def test_real_client_structurally_conforms(self) -> None:
        # 실제 kpubdata 패키지가 import 가능할 때만 실행. 네트워크 호출은 없다 —
        # 클래스/메서드 시그니처 수준의 구조 적합성만 확인한다.
        kpubdata = pytest.importorskip("kpubdata")

        client_cls = getattr(kpubdata, "Client", None)
        assert client_cls is not None, "kpubdata.Client must exist"

        # Client.dataset(source_key)가 존재해야 한다.
        assert hasattr(client_cls, "dataset"), "kpubdata.Client must expose .dataset()"
        assert callable(client_cls.dataset)

        # dataset(...)이 반환하는 타입에 .list(**params)가 있어야 한다.
        dataset_cls = client_cls.dataset.__annotations__.get("return")
        if isinstance(dataset_cls, str):
            # 문자열 어노테이션이면 client 모듈 네임스페이스에서 해석한다.
            import inspect

            module = inspect.getmodule(client_cls)
            assert module is not None
            dataset_cls = getattr(module, dataset_cls, None)
        assert dataset_cls is not None, "Client.dataset return type must be resolvable"
        assert hasattr(dataset_cls, "list"), "Dataset must expose .list()"
        assert callable(dataset_cls.list)

        # list(...)이 반환하는 타입에 .items가 있어야 한다(builder가 소비하는 속성).
        list_return = dataset_cls.list.__annotations__.get("return")
        if isinstance(list_return, str):
            import inspect

            module = inspect.getmodule(dataset_cls)
            assert module is not None
            list_return = getattr(module, list_return, None)
        assert list_return is not None, "Dataset.list return type must be resolvable"
        assert hasattr(list_return, "items"), "list() result must expose .items"
