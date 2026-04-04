# KPubData-Builder 기여 가이드 (CONTRIBUTING.md)

KPubData-Builder 프로젝트에 기여하고 싶으신가요? 환영합니다! 이 프로젝트는 KPubData에서 가져온 데이터를 다양한 형식(CSV, JSON, SQL 등)으로 가공하고 내보내는 역할을 합니다.

## 1. 환영 인사 및 프로젝트 소개

KPubData 패밀리 소개:
- **kpubdata**: 핵심 라이브러리 (데이터 수집)
- **kpubdata-builder**: 데이터를 내보내고 가공하는 엔진
- **kpubdata-studio**: 시각화 웹 대시보드

이 레포지토리(`kpubdata-builder`)는 데이터를 정제하거나 특정 파일 포맷으로 변환하는 기능을 개발하는 곳입니다.

## 2. 개발 환경 설정 (Python)

### Step 1: Python 설치 (3.10+)
컴퓨터에 Python이 설치되어 있어야 합니다. [공식 사이트](https://www.python.org/)나 `pyenv`를 사용해 설치해 주세요.

### Step 2: uv 도구 설치
`uv`는 프로젝트 관리에 필요한 도구들을 한 번에 관리합니다.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Step 3: 프로젝트 Clone
GitHub에서 이 레포지토리를 **Fork**한 뒤, 터미널에서 명령어를 입력하세요.

```bash
git clone https://github.com/YOUR_USERNAME/kpubdata-builder.git
cd kpubdata-builder
```

### Step 4: 개발 환경 구축 및 확인
아래 명령어를 실행해 보세요.

```bash
# 의존성 설치
uv sync --extra dev

# 테스트 실행 (전체 기능 확인)
uv run pytest

# 코드 스타일 검사 (린트)
uv run ruff check .

# 타입 체크 (타입이 올바른지 검사)
uv run mypy src
```

## 3. 기여 시작하기 (Git 워크플로)

1. **이슈 선택**: GitHub Issues에서 `good first issue` 라벨이 붙은 이슈를 골라보세요. "이슈 작업하겠습니다"라고 댓글을 남겨주시면 좋습니다.
2. **브랜치 만들기**: `feat/issue-번호-간단설명` 형식으로 이름을 정해 보세요.
   - 예: `feat/issue-15-add-parquet-exporter`
3. **코드 수정**: 코드를 수정한 뒤 커밋 메시지를 작성합니다.
   - `feat: Parquet 파일 내보내기 기능 추가`
   - `fix: CSV 변환 시 한글 깨짐 현상 수정`
4. **Push**: 내 GitHub 레포지토리에 올립니다.
   - `git push origin feat/issue-15-add-parquet-exporter`
5. **PR 보내기**: GitHub 웹사이트에서 "Pull Request"를 생성하세요.

## 4. 코딩 컨벤션

- **정적 타입**: 모든 함수는 타입 힌트를 포함해야 합니다. `Any`는 피해주세요.
- **포맷팅**: `uv run ruff format .`으로 코드 스타일을 자동으로 정리하세요.
- **가독성**: 변수 이름은 누구나 알 수 있도록 명확하게 지어주세요.

## 5. 첫 번째 내보내기 도구(Exporter) 추가하기

KPubData-Builder에 새로운 파일 형식을 추가해 봅시다.

1. `src/kpubdata_builder/exporters/` 폴더로 이동합니다.
2. `BaseExporter` 클래스를 상속받는 새로운 클래스를 만듭니다.
3. 데이터를 파일로 저장하는 로직을 구현합니다.
4. `tests/` 폴더에 테스트 파일을 작성해 기능이 잘 작동하는지 확인합니다.

## 6. PR 체크리스트

PR을 작성할 때 제목을 `[#이슈번호] 간단한 설명`으로 작성해 주세요.

- [ ] 로컬 테스트(`uv run pytest`)가 모두 성공했나요?
- [ ] 린트(`uv run ruff check .`)에서 오류가 없나요?
- [ ] 변경 사항을 잘 설명하는 테스트 코드가 포함되었나요?

## 7. 질문과 도움

작업하다 막히면 주저하지 말고 GitHub Issues에 질문하세요! 모르는 것을 물어보는 것은 기여의 아주 중요한 시작입니다. "어디서부터 시작해야 할까요?", "이 에러는 왜 발생할까요?" 같은 질문 모두 환영합니다.
