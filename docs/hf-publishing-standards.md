# HuggingFace 데이터셋 퍼블리싱 표준 규칙

이 문서는 kpubdata-builder를 통해 한국 공공데이터를 HuggingFace Hub에 반복적으로 퍼블리싱할 때 따라야 할 **표준 규칙**을 정의한다.

스크립트 사용법과 Config YAML 스키마는 [publishing.md](./publishing.md)를 참고한다.

---

## 1. 목적 및 범위

kpubdata-builder는 `kpubdata`가 수집한 원시 공공데이터를 **정제 → 검증 → 패키징 → 배포**하는 빌드 엔진이다. HuggingFace Hub에 퍼블리싱하는 것은 이 파이프라인의 최종 단계(Publish)에 해당한다.

이 문서는 다음을 표준화한다:

- 데이터셋 네이밍
- 법적 요건 및 라이선스
- 언어 규칙
- Dataset Card 필수 구성
- 데이터 품질 기준
- Config 작성 규칙
- 퍼블리싱 전 체크리스트

---

## 2. 데이터셋 네이밍 컨벤션

### HF Repo ID 형식

```
kpubdata/{scope}-{subject}-{type}
```

| 요소 | 설명 | 예시 |
| :--- | :--- | :--- |
| `scope` | 지역 범위 | `seoul`, `korea`, `busan`, `gyeonggi` |
| `subject` | 데이터 주제 | `apartment`, `weather`, `population`, `base-rate` |
| `type` | 데이터 종류 | `trades`, `prices`, `forecast`, `migration` |

### 규칙

- **영어만** 사용한다.
- **kebab-case**를 사용한다 (`seoul-apartment-trades`, NOT `seoulApartmentTrades`).
- scope가 전국인 경우 `korea`를 사용한다.
- scope가 특정 주제에 불필요한 경우(예: 기준금리) `{subject}` 또는 `{scope}-{subject}`로 축약할 수 있다.

### 예시

| 데이터 | Repo ID |
| :--- | :--- |
| 서울 아파트 매매 실거래가 | `kpubdata/seoul-apartment-trades` |
| 한국은행 기준금리 | `kpubdata/korea-base-rate` |
| 전국 대기오염 정보 | `kpubdata/korea-air-quality` |
| 부산 인구이동 | `kpubdata/busan-population-migration` |

---

## 3. 법적 요건 및 라이선스

### 법적 근거

한국 공공데이터의 재배포는 **공공데이터의 제공 및 이용 활성화에 관한 법률**(공공데이터법)에 의해 보호된다.

- **제26조**: 공공데이터를 이용하고자 하는 자는 공공기관이나 공공데이터 포털에서 제공받을 수 있다.
- **제17조**: 비공개 정보 또는 제3자 권리 침해 데이터만 제외된다.

### 공공누리 → HuggingFace 라이선스 매핑

| 공공누리 유형 | 조건 | HuggingFace 라이선스 |
| :--- | :--- | :--- |
| **제1유형** | 출처표시 | `cc-by-4.0` |
| **제2유형** | 출처표시 + 상업적 이용 금지 | `cc-by-nc-4.0` |
| **제3유형** | 출처표시 + 변경 금지 | `cc-by-nd-4.0` |
| **제4유형** | 출처표시 + 상업적 이용 금지 + 변경 금지 | `cc-by-nc-nd-4.0` |

> **주의**: HuggingFace는 라이선스 값을 **소문자만** 허용한다. `cc-by-4.0` ✅, `CC-BY-4.0` ❌

### 퍼블리싱 전 법적 확인 사항

1. **이용허락범위 확인**: data.go.kr 해당 API 상세 페이지에서 "이용허락범위" 항목 확인
2. **공공누리 유형 확인**: 제1유형~제4유형 중 어느 것인지 확인
3. **개인정보 포함 여부 확인**: 개인 식별 가능 정보가 포함되지 않았는지 확인
4. **제3자 권리 확인**: 해당 데이터에 제3자 저작권이 포함되지 않았는지 확인

### 출처표시 필수 문구

Dataset Card의 Legal & Attribution 섹션에 반드시 포함해야 하는 **한국어 원문**:

```
본 저작물은 '{기관명}'에서 '{작성연도}' 작성하여 공공누리 제{N}유형으로 개방한
'{데이터명}'을 이용하였으며, 해당 저작물은 '{기관명}, {원본URL}'에서
무료로 다운받으실 수 있습니다.
```

**영어 번역**도 병기한다:

```
This dataset uses '{Data Name}' published by {Agency Name} under
Korea Open Government License Type {N} (공공누리 제{N}유형).
Original data is available at {Source URL}.
```

### 선례

data.go.kr 공공데이터를 HuggingFace에 퍼블리싱한 기존 사례:

| 데이터셋 | 라이선스 | 출처 |
| :--- | :--- | :--- |
| `transitgrid/kr_subway_station_ridership_daily` | CC-BY-4.0 | data.go.kr API |
| `whybe-choi/ko-vdr-train-public-v1.0` | CC-BY-4.0 | 한국 공공기관 문서 |
| `chaannwooff/Dartdoc` | CC-BY-4.0 | DART OpenAPI (금융 데이터) |

---

## 4. 언어 규칙

HuggingFace는 글로벌 플랫폼이므로, 퍼블리싱 산출물은 **영어를 기본 언어**로 한다.

| 항목 | 언어 | 예시 |
| :--- | :--- | :--- |
| Dataset Card (README.md) | **영어** | "Real transaction prices for apartment sales..." |
| 컬럼명 | **영어** (snake_case) | `deal_amount_10k_krw`, `district_code` |
| 컬럼 설명 (features) | **영어 + (한국어)** 병기 | `"Transaction price in 10,000 KRW (거래금액, 만원 단위)"` |
| 출처표시 섹션 | **한국어 원문 + 영어 번역** | 위 출처표시 문구 참조 |
| 태그 | **영어** | `real-estate`, `housing-prices`, `korea` |
| Config YAML 주석 | 한국어 또는 영어 | `# 강남구`, `# Gangnam-gu` |

### 컬럼 설명 작성 패턴

```yaml
features:
  - name: district_code
    description: "5-digit administrative district code (시군구코드)"
  - name: deal_amount_10k_krw
    description: "Transaction price in 10,000 KRW (거래금액, 만원 단위)"
  - name: neighborhood
    description: "Legal neighborhood name in Korean (법정동명, e.g. 역삼동)"
```

패턴: `{영어 설명} ({한국어 원본명})`

---

## 5. Dataset Card 필수 섹션

모든 HuggingFace 데이터셋의 README.md는 다음 섹션을 포함해야 한다.

### 5.1 YAML Front Matter (필수)

```yaml
---
license: cc-by-4.0
language:
- ko
tags:
- real-estate
- housing-prices
- korea
- seoul
- tabular
- regression
- time-series
size_categories:
- 100K<n<1M
task_categories:
- tabular-regression
---
```

필수 필드:

| 필드 | 설명 |
| :--- | :--- |
| `license` | 공공누리 매핑에 따른 라이선스 (소문자) |
| `language` | 데이터 언어 코드 (`ko`) |
| `tags` | 검색용 태그 (영어) |
| `size_categories` | 레코드 수 범위 |
| `task_categories` | ML 태스크 유형 (해당 시) |

### 5.2 본문 섹션 (필수)

| 섹션 | 내용 | 언어 |
| :--- | :--- | :--- |
| **Title** | 데이터셋 제목 | 영어 |
| **Dataset Description** | 1~2 문단 요약 | 영어 |
| **Dataset Summary** | 레코드 수, 피처 수, 출처, 기간, 지역 범위 | 영어 |
| **Features** | 컬럼별 이름, 타입, 설명 테이블 | 영어 + (한국어) |
| **Statistics** | 수치 컬럼 통계 (mean, std, min, max) | 영어 |
| **Sample Data** | 상위 5행 테이블 | - |
| **Usage** | `datasets` 라이브러리 코드 스니펫 | 영어 |
| **Data Collection** | 수집 방법, API 정보, kpubdata 사용 | 영어 |
| **Legal & Attribution** | 출처표시 문구 (한국어 원문 + 영어 번역), 라이선스, 원본 링크 | 한/영 병기 |
| **Limitations** | 알려진 제한사항, 커버리지 공백, 업데이트 주기 | 영어 |
| **Citation** | BibTeX 항목 | 영어 |

### 5.3 Dataset Description 작성 가이드

첫 문단에 다음 정보를 포함한다:

- 데이터가 무엇인지 (what)
- 어디서 온 데이터인지 (source)
- 어떤 범위를 커버하는지 (scope: 지역, 기간)
- 어떤 ML 태스크에 적합한지 (use case)

예시:

```markdown
This dataset contains real apartment trade transaction records in Seoul, South Korea,
covering all 25 districts from January 2020 to December 2024 (60 months).
Sourced from the Ministry of Land, Infrastructure and Transport (MOLIT) via data.go.kr,
it provides a comprehensive time-series view of the Seoul housing market across
the COVID-19 boom, 2022 correction, and subsequent recovery phases.
```

### 5.4 Citation BibTeX 템플릿

```bibtex
@dataset{kpubdata_{dataset_id}_{year},
  title  = {{Dataset Title}},
  author = {{kpubdata}},
  year   = {{collection_year}},
  url    = {https://huggingface.co/datasets/kpubdata/{dataset-name}},
  note   = {Sourced from data.go.kr under Korea Open Government License Type {N}}
}
```

---

## 6. 데이터 품질 기준

### 최소 요건

| 기준 | 최소값 | 권장값 | 근거 |
| :--- | :--- | :--- | :--- |
| 레코드 수 | 10,000건 | 50,000건 이상 | California Housing: ~20,640건 |
| 시계열 깊이 | 12개월 | 36개월 이상 | 최소 1개 시장 사이클 커버 |
| 결측치 비율 | 컬럼별 50% 미만 | 컬럼별 10% 미만 | - |

### 필수 처리

- **타입 일관성**: 모든 컬럼에 `dtypes` 명시 (str, int, float, int_comma)
- **결측치 처리**: 빈 문자열, `"-"`, `"N/A"` 등은 `null`로 통일
- **의미 없는 레코드 제거**: 거래금액 0, 면적 0 등 명백히 잘못된 데이터 필터링
- **결측치 비율 공개**: Dataset Card에 컬럼별 null 비율 명시

### 시계열 데이터 추가 기준

- **최소 36개월** 권장: 계절성과 시장 사이클을 커버해야 함
- **정권/정책 변화 시점** 문서화: 부동산 규제 변경, 금리 인상 등 주요 이벤트 기록
- Dataset Card의 Limitations 섹션에 **시계열 분할 권고** 포함:

```markdown
For time-series modeling, use time-based splits (not random splits)
to avoid data leakage. Recommended split: train ≤ 2023, test = 2024.
```

---

## 7. Config YAML 작성 규칙

Config YAML 스키마 자체는 [publishing.md](./publishing.md#config-yaml-스키마)를 참고한다. 여기서는 **표준 준수를 위한 작성 규칙**만 정의한다.

### fetch_params

- 지역 코드(`LAWD_CD`)에는 **주석으로 지역명**을 표기한다.
- 대량 파라미터는 스크립트로 생성하되, 생성된 YAML에도 주석을 유지한다.

```yaml
fetch_params:
  - LAWD_CD: "11110"    # 종로구
    DEAL_YMD: "202001"
  - LAWD_CD: "11140"    # 중구
    DEAL_YMD: "202001"
```

### column_mapping

- API가 제공하는 모든 **유용한 필드**를 포함한다.
- 영어 컬럼명은 **snake_case**를 사용한다.
- 단위가 있는 컬럼은 **단위를 컬럼명에 포함**한다: `deal_amount_10k_krw`, `exclusive_area_m2`

### features

- 설명은 **`{영어 설명} ({한국어 원본명})`** 패턴을 따른다.
- target variable이 있으면 설명에 명시한다: `"(target variable for regression)"`

### license

- **반드시 소문자**로 작성한다: `cc-by-4.0` ✅
- HuggingFace `_validate_yaml()`이 대문자를 거부한다.

### card.description

- **영어**로 작성한다.
- 데이터의 범위 (지역, 기간)를 명시한다.
- 출처 기관을 영어 정식 명칭으로 표기한다.

---

## 8. 퍼블리싱 전 체크리스트

새 데이터셋을 퍼블리싱하기 전, 또는 기존 데이터셋을 업데이트하기 전에 다음을 확인한다.

### 법적 확인

- [ ] data.go.kr 해당 API 페이지에서 **이용허락범위** 확인
- [ ] **공공누리 유형** 확인 및 라이선스 매핑 완료
- [ ] **개인정보** 포함 여부 확인 (포함 시 퍼블리싱 금지)
- [ ] **출처표시 문구** 한국어 원문 + 영어 번역 Dataset Card에 포함

### 데이터 품질

- [ ] `--local-only`로 **로컬 테스트** 완료
- [ ] 레코드 수 **최소 10,000건** 이상
- [ ] 모든 컬럼에 **dtype 지정** 완료
- [ ] **결측치 비율** 확인 및 Dataset Card에 명시
- [ ] 의미 없는 레코드(금액 0 등) **필터링** 완료
- [ ] **샘플 데이터** 5행 검수 (값이 합리적인지 확인)

### Dataset Card

- [ ] **영어**로 작성 완료
- [ ] 모든 **필수 섹션** 포함 (§5.2 참조)
- [ ] 컬럼 설명에 **한국어 원본명 병기**
- [ ] **YAML front matter** 필수 필드 포함
- [ ] **Citation** BibTeX 항목 포함

### 프로세스

- [ ] feature branch에서 config 변경사항 **커밋**
- [ ] **PR** 생성 후 리뷰
- [ ] PR merge 후 퍼블리싱 실행

---

## 9. 버전 관리

### 수집 메타데이터

Dataset Card에 다음을 반드시 명시한다:

- **수집 일자**: 데이터를 API에서 가져온 날짜
- **데이터 범위**: 시작 ~ 종료 기간, 지역 범위
- **kpubdata 버전**: 수집에 사용한 kpubdata 패키지 버전

```markdown
## Data Collection

- **Collection date**: 2025-04-26
- **Time range**: 2020-01 to 2024-12
- **Geographic scope**: Seoul, all 25 districts
- **kpubdata version**: 0.1.x
- **API calls**: 1,500 (25 districts × 60 months)
```

### 업데이트 정책

- 기존 데이터셋을 **덮어쓰기** 전, HF repo의 이전 상태를 기록한다 (commit hash 또는 수집 일자).
- 데이터 범위가 확장되면 Dataset Card의 수집 메타데이터를 함께 업데이트한다.
- 스키마(컬럼 구조)가 변경되면 Dataset Card의 Features 테이블과 Breaking Changes 섹션을 업데이트한다.

---

## 관련 문서

| 문서 | 설명 |
| :--- | :--- |
| [publishing.md](./publishing.md) | 퍼블리싱 스크립트 사용법 및 Config YAML 스키마 |
| [공공누리 이용허락 안내](https://www.kogl.or.kr/info/license.do) | 공공누리 유형별 이용 조건 |
| [공공데이터법](https://www.law.go.kr/법령/공공데이터의제공및이용활성화에관한법률) | 법률 원문 |
| [HuggingFace Dataset Card Guide](https://huggingface.co/docs/hub/datasets-cards) | HF 공식 Dataset Card 작성 가이드 |
