# Why kpubdata-builder

## Problem

Korea's [data.go.kr](https://www.data.go.kr) hosts thousands of valuable public datasets.
But for global researchers and developers, they're practically invisible:

1. API requires Korean government service key registration (in Korean)
2. Responses are XML with Korean field names
3. No documentation in English
4. No standard format — no Parquet, no HuggingFace, no Kaggle

## What We Do

kpubdata-builder is the pipeline that turns raw Korean public API data
into publish-ready HuggingFace Datasets.

```
[data.go.kr API] → [kpubdata SDK] → [builder pipeline] → [HuggingFace Dataset]
```

The goal: **한국 공공데이터를 전세계 누구나 `load_dataset()` 한 줄로 쓸 수 있게 만든다.**

(Make Korean public data accessible to anyone worldwide with a single `load_dataset()` call.)

## Design Decisions

### Keep Korean text values

Neighborhood and building names stay in Korean. Romanization is lossy and inconsistent
(`효성주얼리시티` → `hyoseong Jewelry City`? No.). English column names are sufficient
for programmatic access. Domain context is explained in dataset cards.

### One config = one dataset

A YAML config fully defines what gets published: source, column mapping, types, filters, output.
No undeclared columns, no surprises. Config is the single source of truth.

### Validation gates

Publish fails if the output schema drifts from the config. Specifically:
- Undeclared columns in output → **fail**
- Declared columns missing from output → **fail**
- 100% null columns → **warning**

### No feature engineering

We publish clean raw government data, not derived analytics tables.
Users add lat/lon, subway distances, interest rates, etc. themselves — just like Kaggle.
The dataset's job is source fidelity and accessibility, not ML-readiness.

### Source fidelity over convenience

- Original values preserved (no translation, no romanization of data)
- Null handling: standardized null tokens, but no imputation
- Filtering: only obviously invalid records removed (e.g. price = 0)

## Naming Convention

HuggingFace datasets follow: `kpubdata/{scope}-{subject}-{type}`

Examples:
- `kpubdata/seoul-apartment-trades` — Seoul apartment sale transactions
- `kpubdata/korea-air-quality-hourly` — Nationwide hourly air quality
- `kpubdata/busan-bus-ridership` — Busan bus passenger counts

## Quality Standards

From [hf-publishing-standards.md](./hf-publishing-standards.md):

- Minimum 10,000 records (recommended 50,000+)
- Time series: minimum 36 months coverage
- Types declared and enforced
- Dataset card with English descriptions + Korean context
- Legal attribution (공공누리 → CC mapping)
- Pre-publish checklist with local dry-run

## Relationship to Other Projects

| Project | Role |
|---|---|
| **kpubdata** | Python SDK — handles API auth, pagination, response parsing |
| **kpubdata-builder** | Build pipeline — fetch, transform, validate, publish |
| **kpubdata-studio** | Visual workbench UI (future) — inspect, preview, export |
| **HuggingFace kpubdata org** | Published datasets — the end product users consume |
