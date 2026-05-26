"""scheduled dataset update workflow(#70)의 퍼블리시 워크플로 구조를 검증한다.

GitHub Actions 러너 없이 실행 결과를 검증할 수 없으므로, 워크플로 YAML이
파싱되고 재사용/수동 트리거·퍼블리시 스크립트 호출·시크릿 가드를 갖췄는지
구조적으로 확인한다. 데이터셋별 cron 스케줄 전략은 DATA_FRESHNESS.md에 정의된다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

_ROOT = Path(__file__).parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "publish-dataset.yml"


def _load_workflow() -> dict[Any, Any]:
    return cast(dict[Any, Any], yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8")))


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML(YAML 1.1)은 bare `on:` 키를 boolean True로 파싱한다(GitHub Actions 관용).
    raw = workflow.get("on", workflow.get(True))
    return cast(dict[str, Any], raw)


def test_workflow_file_exists_and_parses() -> None:
    assert _WORKFLOW.is_file()
    assert isinstance(_load_workflow(), dict)


def test_supports_reusable_and_manual_triggers() -> None:
    triggers = _triggers(_load_workflow())

    assert "workflow_call" in triggers
    assert "workflow_dispatch" in triggers
    # 재사용 호출은 config 입력을 요구한다.
    assert "config" in triggers["workflow_call"]["inputs"]


def test_publish_step_invokes_publish_script_with_guard() -> None:
    workflow = _load_workflow()
    steps = workflow["jobs"]["publish"]["steps"]
    run_blocks = "\n".join(step.get("run", "") for step in steps)

    assert "scripts/publish_to_hf.py" in run_blocks
    # 시크릿 미설정 시 라이브 실행을 건너뛰는 가드가 있어야 한다.
    assert "KPUBDATA_DATAGO_API_KEY" in run_blocks


def test_data_freshness_policy_doc_exists() -> None:
    # 스케줄 전략의 단일 소스 문서가 존재해야 한다.
    assert (_ROOT / "DATA_FRESHNESS.md").is_file()
