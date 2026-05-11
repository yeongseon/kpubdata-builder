---
license: cc-by-4.0
language:
- ko
- en
tags:
- real-estate
- housing-prices
- korea
- seoul
- tabular
- regression
- time-series
- apartment
- property
size_categories:
- 100K<n<1M
task_categories:
- tabular-regression
configs:
- config_name: en
  data_files: data/en/train.parquet
- config_name: ko
  data_files: data/ko/train.parquet
default_config_name: en
---

# Seoul Apartment Trades

Real apartment trade transaction records in Seoul, South Korea,
covering all 25 districts (gu) from January 2020 to December 2024 (60 months).

Sourced from the Ministry of Land, Infrastructure and Transport (MOLIT)
via data.go.kr public data API. This dataset provides a comprehensive
time-series view of the Seoul housing market, capturing the COVID-19
pandemic boom (2020-2021), the interest rate correction (2022),
market normalization (2023), and early recovery signals (2024).

The target variable for regression tasks is `deal_amount_10k_krw`
(transaction price in units of 10,000 KRW).


## Dataset Summary

- **Records**: 233,565
- **Features**: 14
- **Source**: [data.go.kr](https://www.data.go.kr/data/15126468/openapi.do)
- **HuggingFace Repo**: [kpubdata/seoul-apartment-trades](https://huggingface.co/datasets/kpubdata/seoul-apartment-trades)

## Statistics

| Feature | Mean | Std | Min | Max |
| :--- | ---: | ---: | ---: | ---: |
| `exclusive_area_m2` | 74.93 | 30.91 | 10.156 | 317.36 |
| `floor` | 9.41 | 6.34 | -3 | 68 |
| `build_year` | 2,002.16 | 10.75 | 1961 | 2025 |
| `deal_year` | 2,021.74 | 1.64 | 2020 | 2024 |
| `deal_month` | 6.25 | 3.16 | 1 | 12 |
| `deal_day` | 15.84 | 8.67 | 1 | 31 |
| `deal_amount_10k_krw` | 100,792.15 | 76,720.70 | 6000 | 2500000 |

## Features

| Feature | Description |
| :--- | :--- |
| `district_code` | 5-digit administrative district code (시군구코드) |
| `neighborhood` | Legal neighborhood name in Korean (법정동명, e.g. 역삼동) |
| `apartment_name` | Apartment complex name in Korean (아파트 단지명) |
| `exclusive_area_m2` | Exclusive use area in square meters (전용면적, m²) |
| `floor` | Floor number of the traded unit (거래 층수) |
| `build_year` | Year the building was constructed (건축년도) |
| `deal_year` | Year of the transaction (거래년도) |
| `deal_month` | Month of the transaction (거래월) |
| `deal_day` | Day of the transaction (거래일) |
| `deal_amount_10k_krw` | Transaction price in 10,000 KRW (거래금액, 만원 단위 — target variable) |
| `lot_number` | Lot number / land parcel ID (지번) |
| `deal_date` | Transaction date as YYYY-MM-DD string (거래일자, derived) |
| `registration_date` | Registration date at registry office (등기일자) |
| `apartment_seq` | Unique apartment complex identifier (아파트 일련번호) |

## Sample Data

| district_code | neighborhood | apartment_name | exclusive_area_m2 | floor | build_year | deal_year | deal_month | deal_day | deal_amount_10k_krw | lot_number | registration_date | apartment_seq | deal_date |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 11110 | 창신동 | 창신쌍용1 | 54.7 | 5 | 1992 | 2020 | 1 | 3 | 31000 | 702 | None | 11110-37 | 2020-01-03 |
| 11110 | 평창동 | 삼성 | 84.93 | 6 | 1998 | 2020 | 1 | 13 | 53000 | 596 | None | 11110-73 | 2020-01-13 |
| 11110 | 사직동 | 광화문스페이스본(106동) | 163.33 | 2 | 2008 | 2020 | 1 | 2 | 162000 | 9-1 | None | 11110-2204 | 2020-01-02 |
| 11110 | 명륜3가 | 한빛 | 59.73 | 4 | 1999 | 2020 | 1 | 10 | 34500 | 1-30 | None | 11110-27 | 2020-01-10 |
| 11110 | 창신동 | 창신쌍용1 | 106.62 | 1 | 1992 | 2020 | 1 | 19 | 68500 | 702 | None | 11110-37 | 2020-01-19 |

## Usage

```python
from datasets import load_dataset

ds = load_dataset("kpubdata/seoul-apartment-trades")
df = ds["train"].to_pandas()
print(df.head())
```

### Loading a specific config

```python
# English config (default): romanized apartment names and neighborhoods
ds_en = load_dataset("kpubdata/seoul-apartment-trades", "en")

# Korean config: original Korean text with Korean column names
ds_ko = load_dataset("kpubdata/seoul-apartment-trades", "ko")
```

## Source

This dataset was generated using [kpubdata](https://github.com/yeongseon/kpubdata)
from public data APIs on data.go.kr.

## Legal & Attribution

This dataset uses "아파트매매 실거래 상세 자료" (Apartment Trade Detailed Data)
published by the Ministry of Land, Infrastructure and Transport (국토교통부)
under Korea Open Government License Type 1 (공공누리 제1유형).

**Korean attribution (한국어 출처표시):**

> 본 저작물은 '국토교통부'에서 작성하여 공공누리 제1유형으로 개방한
> '아파트매매 실거래 상세 자료'를 이용하였으며, 해당 저작물은
> '국토교통부, https://www.data.go.kr/data/15126468/openapi.do'에서
> 무료로 다운받으실 수 있습니다.

**License:** [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/)
**Original source:** https://www.data.go.kr/data/15126468/openapi.do
**공공누리 정보:** https://www.kogl.or.kr/info/licenseType1.do

