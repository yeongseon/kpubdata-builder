# Task Plan

## Scope
- Initialize `kpubdata-builder` as a Python package repository.
- Mirror `kpubdata` project conventions for tooling, CI, and quality gates.
- Add required architecture-aligned package stubs, tests, and documentation.

## Touched modules
- Project metadata/config: `pyproject.toml`, `.gitignore`, `LICENSE`, `.github/workflows/ci.yml`, `CHANGELOG.md`
- Product docs in repo root and `docs/adrs/`
- Source package under `src/kpubdata_builder/`
- Unit tests under `tests/unit/`

## Risks
- Strict mypy mode may fail on incomplete or weakly typed stubs.
- Ruff import ordering and formatting can fail if file layout diverges.
- Build may fail if package data markers or wheel package path are incorrect.

## Validation steps
- `uv sync --extra dev`
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run mypy src`
- `uv run pytest`
- `uv run python -m build`

---

## 📚 관련 문서

### 이 저장소 내 문서
| 문서 | 설명 |
| :--- | :--- |
| [ROADMAP.md](./ROADMAP.md) | 프로젝트 로드맵 |
| [PRD.md](./PRD.md) | 제품 요구사항 정의서 |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 시스템 아키텍처 설계 |

