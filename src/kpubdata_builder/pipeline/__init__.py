"""파이프라인 오케스트레이션 패키지 (Medallion 재구성 골격).

Bronze → Silver → Gold → Export → Manifest 흐름을 묶는 orchestrator와
BuildContext가 이 패키지에 들어온다 (issue #48). 현재는 디렉터리 구조로
Medallion 파이프라인의 진입점 위치를 드러내기 위한 골격이다.
"""

from __future__ import annotations

__all__: list[str] = []
