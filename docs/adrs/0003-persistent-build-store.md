# ADR 0003 — 영속 Build 저장소: 파일시스템 스캔 대체

- 상태: 제안됨(Proposed)
- 관련 이슈: #309, #308, kpubdata-studio#102
- 관련 문서: [ARCHITECTURE.md](../../ARCHITECTURE.md), [BUILD_STATE.md](../../BUILD_STATE.md)

## 배경

`GET /builds`(`BuilderService.list_builds`)는 현재 `output_root` 아래 디렉터리를 직접 스캔한다.

```python
candidates = heapq.nlargest(
    limit,
    (d for d in self._output_root.iterdir() if d.is_dir()),
    key=lambda p: p.stat().st_mtime,
)
# 각 후보에서 manifest.json을 읽어 status/started_at/finished_at 조립
```

즉 빌드 이력의 **단일 진실원천이 파일시스템**이며, 목록 조회 시마다 디렉터리를 순회하고 각 `manifest.json`을 파싱한다.

## 문제

- **확장성**: 빌드 수 증가 시 `iterdir` + per-run `manifest.json` 파싱 비용이 선형 증가.
- **일관성**: 빌드 진행 중(모델 전환 시)에는 manifest가 아직 없어 상태 표현 불가. 현재는 `manifest.json` 없는 디렉터리를 건너뛴다.
- **동시성**: 여러 빌드/정리 작업이 동시에 디렉터리를 건드릴 때 경합.
- **상태 표현 한계**: ADR 0002의 비동기 job 상태(`queued`/`running`)를 파일만으로 표현하기 어렵다.

## 결정 필요 사항

1. 빌드 메타데이터 인덱스의 저장 방식.
2. 목록/조회/페이지네이션 계약.
3. run_id 생성/충돌 정책(현재 `validate_path_segment`로 경로 안전성만 검증).
4. 보존/정리(retention) 정책.

## 검토한 대안

### 대안 A — 현행 파일시스템 스캔 유지
- 장점: 무의존성, 단순, 이미 동작. 소규모에서 충분.
- 단점: 확장성·진행상태·동시성 한계(위 문제).

### 대안 B — 경량 SQLite 인덱스
- `builds` 테이블(run_id, status, started_at, finished_at, spec_digest, error)로 인덱싱.
- 장점: 쿼리/페이지네이션/상태전이 자연스러움, 단일 파일 무서버, 트랜잭션.
- 단점: manifest(파일)와 DB(인덱스) 이중 소스 → 동기화 규약 필요.

### 대안 C — JSON 인덱스 파일(`_index.json`)
- 장점: 의존성 없음, 사람이 읽기 쉬움.
- 단점: 동시 쓰기 잠금 필요, 대규모에서 여전히 비효율.

## 권고 (제안)

**대안 B(SQLite 인덱스)를 제안**하되, **manifest.json을 여전히 per-run 정본으로 유지**하고 SQLite는 **파생 인덱스(캐시)**로 둔다.

- 정본: `build/{run_id}/manifest.json` (감사·재현성, AGENTS.md '매니페스트 누락 금지' 준수).
- 인덱스: `output_root/_builds.sqlite` — 빌드 생성/상태전이 시 갱신, 유실 시 스캔으로 재구축 가능(권위 없음).
- `list_builds`는 인덱스를 우선 조회하고, 인덱스 부재 시 현행 스캔으로 폴백.

이 설계는 ADR 0002의 비동기 상태(`queued`/`running`)를 manifest 없이도 표현할 수 있게 한다.

## 영향

- `list_builds` 구현이 인덱스 우선 + 스캔 폴백으로 변경.
- 페이지네이션 계약(`?limit=`, 향후 `?cursor=`)을 `contract/builder-api.yaml`에 반영.
- retention: run 수/디스크 상한 초과 시 오래된 run 정리 정책을 별도 이슈로 분해.

## 미해결 질문

- SQLite 도입이 '무의존성 지향'과 충돌하는가? (표준 라이브러리 `sqlite3`로 무외부의존 가능)
- 인덱스와 manifest 불일치 시 조정(reconcile) 트리거 시점?
- run_id를 서버 생성(예: ULID)으로 표준화할지, 클라이언트 지정 허용을 유지할지?
