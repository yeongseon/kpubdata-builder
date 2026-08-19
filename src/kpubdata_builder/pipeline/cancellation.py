"""Pipeline이 이해하는 최소한의 협력적 취소(cooperative cancellation) 계약 (#481).

ADR 0008이 권고한 "단계 경계에서 취소 플래그 점검 + 부분 산출물 보존"을 위해
필요한 것은 두 가지뿐이다.

1. **narrow probe**: pipeline은 "지금 취소가 요청되었는가"와 "지금부터 정상
   종료로 확정해도 되는가"만 알면 된다. HTTP/Principal/job registry 같은
   service 개념을 pipeline domain으로 역주입하지 않는다 — 그래서 이 모듈은
   ``kpubdata_builder.service``를 전혀 import하지 않고, service 쪽 구현
   (``service.jobs``의 run 단위 cancellation state)이 이 Protocol을 구조적으로
   만족한다.
2. **control-flow signal**: 취소는 실패가 아니다. 일반 ``RuntimeError``나 기존
   ``BuildError`` 계층을 재사용하면 ``_run_source_pipeline``의 공통 except가
   이를 "이 소스 실패"로 해석해 취소를 failure로 위장하게 된다. 그래서 별도
   내부 예외를 쓰되, 이 예외는 HTTP wire나 manifest로 절대 새어나가지 않는다
   (메시지도 고정 문자열이며 경로/스택/원본 예외를 담지 않는다).

``commit()``은 "point of no return"이다. pipeline이 마지막 안전 경계를 지나
manifest finalize에 진입하기 직전에 정확히 한 번 호출하며, 이후 도착하는 취소
요청은 거절된다 — 이 latch가 없으면 ``cancelling`` 상태의 job이 그대로
``succeeded``로 끝나거나(상태 머신 위반), 이미 성공 manifest를 쓴 run이
``cancelled``로 뒤집히는(manifest와 job 상태의 모순) 경쟁이 생긴다.
"""

from __future__ import annotations

from typing import Protocol


class BuildCancelled(Exception):
    """안전한 stage 경계에서 취소가 관찰되었음을 알리는 내부 control-flow 신호.

    ``BuildError`` 계층에 넣지 않는다 — 취소는 build 실패가 아니며, 기존
    실패 처리(``except Exception`` → outcome "failed")에 섞이면 안 된다.
    메시지는 호출부가 채우지 않는 고정 문자열이다(경로/원본 예외 없음).
    """

    def __init__(self) -> None:
        super().__init__("build cancelled at a safe stage boundary")


class CancellationProbe(Protocol):
    """pipeline이 필요로 하는 최소 협력적 취소 인터페이스.

    구현체는 thread-safe해야 한다 — 여러 source worker 스레드(#247)가 동시에
    ``cancel_requested()``를 호출한다. 구현체는 run 하나에만 바인딩되어야
    하며, 한 run의 취소가 다른 run에 영향을 주면 안 된다.
    """

    def cancel_requested(self) -> bool:
        """이 run에 취소가 요청되었는지. 상태를 바꾸지 않는다(순수 조회)."""
        ...

    def commit(self) -> bool:
        """정상 종료(성공/실패 manifest 기록)로 확정할 수 있으면 True.

        True를 반환하면 이후 취소 요청은 거절된다(point of no return).
        False면 이미 취소가 요청된 것이므로 호출부는 취소 경로로 가야 한다.
        여러 번 호출해도 같은 값을 반환한다(idempotent).
        """
        ...


def raise_if_cancelled(probe: CancellationProbe | None) -> None:
    """안전 경계에서 호출하는 표준 점검. 취소 요청이 있으면 ``BuildCancelled``.

    ``probe``가 ``None``이면(동기 ``POST /build``, CLI 직접 호출) 아무 것도
    하지 않는다 — 취소 개념이 없는 호출자는 기존 동작을 100% 유지한다.
    """
    if probe is not None and probe.cancel_requested():
        raise BuildCancelled


__all__ = ["BuildCancelled", "CancellationProbe", "raise_if_cancelled"]
