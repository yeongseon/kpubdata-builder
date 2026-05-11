---
license: cc-by-4.0
language:
- ko
tags:
- real-estate
- housing-prices
- korea
- tabular
- regression
size_categories:
- 1K<n<10K
---

# Korean Apartment Trades (아파트매매 실거래가)

Real transaction prices for apartment sales in South Korea,
sourced from the Ministry of Land, Infrastructure and Transport (MOLIT)
via data.go.kr public API.

This dataset is the Korean equivalent of the California Housing dataset —
a tabular regression benchmark where the target variable is the transaction
price (`deal_amount_10k_krw`, unit: 10,000 KRW).


## Dataset Summary

- **Records**: 3,221
- **Features**: 12
- **Source**: [data.go.kr](https://www.data.go.kr)
- **HuggingFace Repo**: [kpubdata/korean-apartment-trades](https://huggingface.co/datasets/kpubdata/korean-apartment-trades)

## Statistics

| Feature | Mean | Std | Min | Max |
| :--- | ---: | ---: | ---: | ---: |
| `exclusive_area_m2` | 88.81 | 38.65 | 12.1 | 273.96 |
| `floor` | 10.51 | 7.62 | 1 | 64 |
| `build_year` | 2,002.56 | 13.06 | 1975 | 2024 |
| `deal_year` | 2,024.00 | 0.00 | 2024 | 2024 |
| `deal_month` | 4.22 | 1.65 | 1 | 6 |
| `deal_day` | 16.43 | 8.95 | 1 | 31 |
| `deal_amount_10k_krw` | 239,345.01 | 127,466.58 | 16500 | 1150000 |

## Features

| Feature | Description |
| :--- | :--- |
| `district_code` | 시군구 코드 (5-digit administrative district code) |
| `neighborhood` | 읍면동 이름 (e.g. 개포동, 역삼동) |
| `apartment_name` | 아파트 단지명 |
| `exclusive_area_m2` | 전용면적 (m², exclusive use area) |
| `floor` | 거래 층수 |
| `build_year` | 건축년도 |
| `deal_year` | 거래년도 |
| `deal_month` | 거래월 |
| `deal_day` | 거래일 |
| `deal_amount_10k_krw` | 거래금액 (만원 단위, target variable for regression) |
| `lot_number` | 지번 |
| `deal_date` | 거래일자 (YYYY-MM-DD, derived) |

## Sample Data

| district_code | neighborhood | apartment_name | exclusive_area_m2 | floor | build_year | deal_year | deal_month | deal_day | deal_amount_10k_krw | lot_number | deal_date |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 11680 | 세곡동 | 강남데시앙파크 | 84.95 | 2 | 2011 | 2024 | 1 | 31 | 110000 | 525 | 2024-01-31 |
| 11680 | 대치동 | 쌍용대치아파트1동,2동,3동,5동,6동 | 128.03 | 4 | 1983 | 2024 | 1 | 23 | 308000 | 66 | 2024-01-23 |
| 11680 | 일원동 | 수서 | 50.82 | 9 | 1992 | 2024 | 1 | 30 | 92000 | 711 | 2024-01-30 |
| 11680 | 일원동 | 래미안 개포 루체하임 | 59.99 | 9 | 2018 | 2024 | 1 | 23 | 185000 | 741 | 2024-01-23 |
| 11680 | 개포동 | 디에이치아너힐즈 | 59.8732 | 17 | 2019 | 2024 | 1 | 30 | 219000 | 1281 | 2024-01-30 |

## Usage

```python
from datasets import load_dataset

ds = load_dataset("kpubdata/korean-apartment-trades")
df = ds["train"].to_pandas()
print(df.head())
```

## Source

This dataset was generated using [kpubdata](https://github.com/yeongseon/kpubdata)
from public data APIs on data.go.kr.
