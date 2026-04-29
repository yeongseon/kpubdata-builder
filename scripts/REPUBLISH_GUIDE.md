# Seoul Apartment Trades — Republish Guide

## 왜 재publish가 필요한가

현재 HuggingFace에 올라간 데이터에 다음 문제가 있습니다:

1. **en/ko 두 개 subset으로 분리됨** — en subset에 로마자 변환된 데이터가 있음
2. **로마자 변환 품질 불량** — `changsindong`, `hyoseong Jewelry City` 등
3. **단일 데이터셋으로 통합 필요** — 한글 원본 값 + 영어 컬럼명

## 재publish 절차

### 1. 기존 HF 데이터 삭제

```bash
# HF repo에서 기존 데이터 파일 삭제
huggingface-cli delete kpubdata/seoul-apartment-trades data/ --repo-type dataset
```

### 2. 새 데이터 생성 및 업로드

```bash
# 환경 변수 설정
export KPUBDATA_DATAGO_API_KEY="your-api-key"
export HF_TOKEN="your-hf-token"

# dry-run으로 먼저 확인
python scripts/publish_to_hf.py scripts/configs/seoul_apartment_trades.yaml --local-only --verbose

# 실제 업로드
python scripts/publish_to_hf.py scripts/configs/seoul_apartment_trades.yaml --verbose
```

### 3. 검증

- [ ] HF에서 데이터 viewer로 확인
- [ ] 한글 값이 그대로 유지되는지 확인 (neighborhood, apartment_name)
- [ ] subset이 하나만 있는지 확인 (en/ko 분리 없음)
- [ ] registration_date가 nullable로 올바르게 표시되는지 확인
- [ ] `load_dataset("kpubdata/seoul-apartment-trades")` 테스트

### 4. en subset 삭제 확인

HF에 `config` 디렉토리나 별도 config 파일이 있다면 삭제하여 subset 분리를 제거합니다.
