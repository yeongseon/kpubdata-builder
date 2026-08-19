"""기록된 manifest.json에서 run의 종단 상태를 읽는 단일 정본 규칙 (#481).

manifest.json이 run 결과의 정본이므로(ADR 0003), "이 manifest는 성공/실패/취소
중 무엇인가"를 판정하는 규칙도 manifest 패키지가 소유한다. BuildIndex 재구축
(``store.rebuild_index``), ``GET /builds`` 파일시스템 폴백, dataset grouping
(``service.datasets``)이 모두 이 함수를 재사용해, 같은 manifest가 경로에 따라
다른 상태로 읽히는 드리프트를 없앤다.
"""

from __future__ import annotations

# 인덱스/wire가 공유하는 run 종단 상태 어휘(``store.build_index.BuildStatus``와
# 같은 값). 새 값을 만들지 않는다. 파생 인덱스 계층을 manifest 계층에서 역참조
# 하지 않기 위해 import 대신 이 모듈 내부 상수로 두고, 공개 API로 노출하지도
# 않는다 — 외부에 필요한 것은 판정 함수 하나뿐이다.
_KNOWN_MANIFEST_STATUSES: frozenset[str] = frozenset({"ok", "failed", "cancelled"})


def status_from_manifest(manifest: dict[str, object], *, fallback_status: str | None = None) -> str:
    """manifest 하나에서 run의 종단 상태를 결정한다 (``ok``/``failed``/``cancelled``).

    판정 순서:
        1. manifest에 명시적 ``status``(#481, additive)가 있으면 그대로 쓴다 —
           특히 ``cancelled``는 ``errors``만으로는 복원할 수 없다(취소된 run은
           errors가 비어 있을 수 있다).
        2. legacy manifest(이 필드가 없음)는 기존대로 ``errors`` 유무에서
           파생한다 — 하위 호환 동작이 바뀌지 않는다.
        3. 그마저도 없으면, 파생 index가 이미 알고 있던 ``fallback_status``가
           ``cancelled``일 때만 그 값을 보존한다(파생 index 값이 정본을
           덮어쓰지는 않는다).

    매개변수:
        manifest: 파싱된 manifest.json 매핑.
        fallback_status: 파생 BuildIndex가 알고 있던 상태(선택).

    반환값:
        ``"ok"`` | ``"failed"`` | ``"cancelled"``.
    """
    explicit_status = manifest.get("status")
    if isinstance(explicit_status, str) and explicit_status in _KNOWN_MANIFEST_STATUSES:
        return explicit_status
    if manifest.get("errors"):
        return "failed"
    return "cancelled" if fallback_status == "cancelled" else "ok"


__all__ = ["status_from_manifest"]
