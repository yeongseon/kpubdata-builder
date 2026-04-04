# AGENTS.md — kpubdata-builder

## Mission

Implement KPubData Builder as the orchestration and artifact pipeline layer on top of `kpubdata`.

## Ground Rules

- Do not duplicate provider logic from `kpubdata`
- Keep build specs declarative
- Prefer deterministic behavior over magic
- Keep exporters pluggable
- Every build must emit a manifest
- Validation must fail early and clearly

## Priorities

1. spec models
2. validation flow
3. source execution using `kpubdata`
4. artifact model
5. markdown exporter
6. huggingface layout exporter
7. publish hooks

## Testing Expectations

- unit tests for spec validation
- golden tests for Markdown output
- manifest contract tests
- fixture-based source execution tests

---

## 이 프로젝트 이해하기

KPubData Builder는 `kpubdata` 사서가 가져온 원시 데이터를 사용자가 읽기 좋은 **책(보고서)이나 데이터셋 묶음으로 만들어주는 출판사**와 같습니다. 데이터를 수집하고, 검증하고, 원하는 형식으로 예쁘게 포장하는 과정을 담당합니다.

### 핵심 개념 용어 사전

| 용어 | 설명 |
| :--- | :--- |
| **BuildSpec** | 어떤 데이터를 어떻게 수집해서 어디로 보낼지 적힌 기획서 |
| **Artifact** | 빌드 과정을 통해 만들어진 최종 결과물 (파일 등) |
| **Manifest** | 빌드 결과물에 대한 상세 명세서 (버전, 생성일 등) |
| **Exporter** | 데이터를 특정 형식(Markdown, JSON, HuggingFace 등)으로 변환하는 도구 |
| **Publisher** | 완성된 결과물을 특정 장소(GitHub, HF Hub 등)에 올리는 도구 |
| **Golden Test** | 이전의 '완벽한 결과물'과 현재 결과물을 비교하여 변경 사항을 확인하는 테스트 |

### 이 프로젝트의 코드가 실행되는 흐름 (Pipeline)

```text
[BuildSpec] -> [Validate] -> [Execute (Fetch Data)] -> [Export (Formatting)] -> [Manifest (Metadata)]
      |            |                |                     |                     |
   기획서 확인      검증           데이터 가져오기           포맷 변환               명세서 생성
```

## AI 에이전트 코딩 가이드

### 좋은 프롬프트 예시
- "새로운 `CSVExporter`를 추가해줘. `exporters/base.py`를 참고해서 `ExportModel`을 구현해."
- "`BuildSpec` 모델에 데이터 필터링 조건을 추가하는 기능을 넣어줘."

### 에이전트 금지 사항
- **kpubdata 로직 중복 금지**: 데이터 파싱 로직은 `kpubdata`에 있어야 합니다. 여기서는 가져온 데이터를 다루기만 하세요.
- **불명확한 경로 사용 금지**: 파일 생성 경로는 항상 명확하게 정의되어야 합니다.
- **매니페스트 누락 금지**: 모든 빌드 결과물은 반드시 `manifest.json`을 포함해야 합니다.

### 에이전트 결과물 검증 체크리스트
- [ ] `uv run ruff check .`를 통과했는가?
- [ ] 새로운 Exporter에 대한 유닛 테스트를 작성했는가?
- [ ] Golden Test를 통해 출력 결과물이 의도대로 나오는지 확인했는가?

## 파일 구조 가이드

```text
src/kpubdata_builder/
├── exporters/       # 데이터 형식 변환 (Markdown, JSONL 등)
├── publishers/      # 결과물 업로드 (HF, GitHub 등)
├── spec.py          # 빌드 기획서(BuildSpec) 정의
├── validator.py     # 기획서 및 데이터 검증 로직
├── executor.py      # kpubdata를 사용하여 실제 데이터 수집
├── assembler.py     # 전체 빌드 과정을 오케스트레이션
├── artifact.py      # 생성된 결과물 모델
└── manifest.py      # 빌드 명세서 생성 로직
```

### 이 파일을 수정해야 할 때
- **데이터를 새로운 파일 형식으로 저장하고 싶을 때**: `exporters/`에 새 파일을 만듭니다.
- **결과물을 다른 곳에 자동으로 올리고 싶을 때**: `publishers/`에 새 로직을 추가합니다.
- **빌드 과정에 새로운 단계(Step)를 추가하고 싶을 때**: `assembler.py`를 수정합니다.

## Exporter 추가 가이드

### Exporter 개발 단계
1. `exporters/base.py`의 `BaseExporter` 클래스를 상속받습니다.
2. `export(self, artifacts: List[Artifact]) -> List[Path]` 메서드를 구현합니다.
3. 지원하는 포맷 이름을 클래스 변수로 정의합니다.
4. `tests/unit/test_exporters.py`에 테스트를 추가합니다.

### Golden Test란?
빌드 결과물이 텍스트(예: Markdown)인 경우, 코드가 바뀌어도 결과물의 형식이 유지되는지 확인하기 위해 미리 저장해둔 '정답 파일'과 현재 결과를 1:1로 비교하는 테스트 방식입니다.
