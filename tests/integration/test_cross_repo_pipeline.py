"""kpubdata → Builder 전 구간 E2E (yeongseon/kpubdata#282).

기존 cross-repo 검증은 **형태(shape)** 만 봤다 — `test_kpubdata_client_protocol.py`는
`kpubdata.Client`에 `.dataset().list().items`가 있는지 구조만 확인하고,
`test_studio_contract.py`는 가짜 클라이언트로 Builder 응답 wire 형태만 고정한다.
그래서 kpubdata가 실제로 반환하는 레코드가 Bronze→Silver→Gold를 통과하는지는
어느 레포의 CI에서도 확인되지 않았다.

이 테스트는 **실제 `kpubdata.Client`** 를 Builder에 주입해 그 공백을 메운다.
네트워크와 API 키는 kpubdata의 replay 전송(`KPUBDATA_MODE=replay`)으로 대체한다 —
기록된 fixture를 재생하므로 결정적이고, 실 API 키가 필요 없다. 검증되는 경로:

    BuildSpec(wire) → dispatch(POST /build) → orchestrator → Bronze
      → kpubdata.Client → provider spec 실행기 → replay fixture
      → Silver → Gold → export → manifest

fixture는 kpubdata 레포의 `tests/fixtures/`에 있고 배포 wheel에는 포함되지 않는다.
따라서 `KPUBDATA_REPLAY_DIR`가 가리키는 fixture 디렉터리가 있을 때만 실행하고,
없으면 skip한다 — builder 단독 CI와 로컬 실행은 영향을 받지 않는다. 이 변수를
설정해 실제로 돌리는 곳은 `.github/workflows/cross-repo-contract.yml`이다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from kpubdata_builder.service.app import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.spec import JsonValue

# 데이터셋 선택 조건 두 가지:
#
# 1. **한 페이지로 끝나야 한다** (totalCount 22 ≤ page_size 100). Builder Bronze는
#    kpubdata Dataset을 보면 `list_all()`로 페이지를 끝까지 도는데, 2페이지를
#    요청하면 기록된 fixture가 없어 replay가 실패한다.
# 2. **혼합 타입 컬럼이 없어야 한다.** spec이 `integer`로 선언한 필드에 숫자가 아닌
#    값이 섞여 있으면 kpubdata가 일부만 int로 캐스팅해 한 컬럼에 int/str이 공존하고,
#    Builder Silver가 이를 거부한다(yeongseon/kpubdata#452). apt_trade/sh_trade/
#    ultra_srt_ncst가 여기 걸린다 — 이 테스트가 처음 돌 때 그 문제를 잡아냈다.
_DATASET = "air_station"
_PARAMS: dict[str, JsonValue] = {
    "station": "강남구",
    "term": "daily",
    "page": 1,
    "page_size": 100,
}
_EXPECTED_ROWS = 22


def _replay_dir() -> Path | None:
    raw = os.environ.get("KPUBDATA_REPLAY_DIR", "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


requires_replay_fixtures = pytest.mark.skipif(
    _replay_dir() is None,
    reason=(
        "KPUBDATA_REPLAY_DIR이 kpubdata 레포의 tests/fixtures를 가리켜야 한다 "
        "(cross-repo-contract 워크플로가 설정한다)"
    ),
)


@pytest.fixture
def real_kpubdata_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BuilderService:
    """실제 kpubdata.Client를 쓰는 BuilderService — 전송만 replay로 바꾼다."""
    replay_dir = _replay_dir()
    assert replay_dir is not None  # skipif가 보장한다
    monkeypatch.setenv("KPUBDATA_MODE", "replay")
    monkeypatch.setenv("KPUBDATA_REPLAY_DIR", str(replay_dir))

    kpubdata = pytest.importorskip("kpubdata")
    # replay는 인증 파라미터를 매칭에서 제외하므로 키 값 자체는 의미가 없다.
    client = kpubdata.Client(provider_keys={"datago": "replay-dummy"})
    return BuilderService(output_root=tmp_path, client_factory=lambda **_kwargs: client)


def _spec_yaml() -> str:
    spec: dict[str, JsonValue] = {
        "dataset_id": "dataset.cross_repo_smoke",
        "title": "Cross-repo smoke",
        "description": "kpubdata replay fixture를 Builder 파이프라인 전 구간에 태운다.",
        "sources": [
            {
                "provider": "datago",
                "dataset": _DATASET,
                "params": _PARAMS,
                "alias": "measurements",
            }
        ],
        "exports": [{"kind": "jsonl", "output_path": "out/data.jsonl"}],
    }
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)


@requires_replay_fixtures
class TestCrossRepoPipeline:
    def test_real_client_records_flow_through_to_manifest(
        self, real_kpubdata_service: BuilderService, tmp_path: Path
    ) -> None:
        response = dispatch(
            real_kpubdata_service,
            "POST",
            "/build",
            {"spec": _spec_yaml(), "run_id": "cross-repo"},
        )

        assert isinstance(response, ServiceResponse)
        # 실패 시 원인을 바로 보여준다 — cross-repo 회귀는 메시지가 곧 진단이다.
        assert response.status_code == 200, response.body
        assert response.body["status"] == "ok"

        outcomes = response.body["outcomes"]
        assert isinstance(outcomes, list) and len(outcomes) == 1
        outcome = outcomes[0]
        assert isinstance(outcome, dict)
        assert outcome["status"] == "ok", outcome
        assert outcome["error"] is None

        manifest_path = tmp_path / "cross-repo" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        # kpubdata가 실제로 돌려준 레코드 수가 파이프라인 끝까지 보존돼야 한다 —
        # 어댑터가 응답 파싱을 바꾸면(items_path 변경 등) 여기서 먼저 깨진다.
        row_counts = manifest["row_counts"]
        assert isinstance(row_counts, dict)
        assert sum(int(value) for value in row_counts.values()) == _EXPECTED_ROWS, row_counts

    def test_exported_rows_carry_real_provider_fields(
        self, real_kpubdata_service: BuilderService, tmp_path: Path
    ) -> None:
        response = dispatch(
            real_kpubdata_service,
            "POST",
            "/build",
            {"spec": _spec_yaml(), "run_id": "cross-repo-export"},
        )
        assert isinstance(response, ServiceResponse)
        assert response.status_code == 200, response.body

        # export는 Gold 단계 아래 source alias 디렉터리에 기록된다.
        exported = tmp_path / "cross-repo-export" / "gold" / "measurements" / "out" / "data.jsonl"
        assert exported.exists(), "jsonl export가 기록되지 않았다"
        rows = [json.loads(line) for line in exported.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == _EXPECTED_ROWS

        # 대기측정 API의 실제 필드명이 그대로 실려야 한다. kpubdata가 필드를 리네임하거나
        # 응답 구조를 바꾸면 Studio 화면이 깨지는데, 그 회귀를 여기서 잡는다.
        assert {"dataTime", "pm10Value", "khaiValue"} <= set(rows[0]), sorted(rows[0])
