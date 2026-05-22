# 작업 계획

## 범위
- `kpubdata-builder`를 Python 패키지 저장소로 초기화한다.
- 도구, CI, 품질 게이트에 대해 `kpubdata` 프로젝트의 관례를 따른다.
- 아키텍처에 맞는 필수 패키지 스텁, 테스트, 문서를 추가한다.

## 영향 받는 모듈
- 프로젝트 메타데이터/설정: `pyproject.toml`, `.gitignore`, `LICENSE`, `.github/workflows/ci.yml`, `CHANGELOG.md`
- 저장소 루트와 `docs/adrs/`의 제품 문서
- `src/kpubdata_builder/` 아래 소스 패키지
- `tests/unit/` 아래 단위 테스트

## 위험 요소
- 엄격한 mypy 모드에서 불완전하거나 타입이 약한 스텁이 실패할 수 있다.
- 파일 배치가 어긋나면 Ruff import 순서 및 포맷 검사가 실패할 수 있다.
- 패키지 데이터 마커나 wheel 패키지 경로가 잘못되면 빌드가 실패할 수 있다.

## 검증 단계
- `uv sync --extra dev`
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run mypy src`
- `uv run pytest`
- `uv run python -m build`

---

## 관련 문서

### 이 저장소 내 문서
| 문서 | 설명 |
| :--- | :--- |
| [ROADMAP.md](./ROADMAP.md) | 프로젝트 로드맵 |
| [PRD.md](./PRD.md) | 제품 요구사항 정의서 |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 시스템 아키텍처 설계 |
