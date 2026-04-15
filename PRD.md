# PRD — KPubData Builder

## 1. Product Summary

KPubData Builder is a dataset artifact pipeline that consumes normalized records from `kpubdata`
and produces publishable outputs such as Markdown, Hugging Face datasets, JSONL, Parquet, and metadata artifacts.

It is not a low-level API wrapper.
It is not a UI product.
It is the orchestration and build layer between ingestion and publication.

## 2. Problem

Users can fetch public data, but turning fetched records into reusable, documented, versionable
dataset artifacts is repetitive and inconsistent.

Typical pain points:
- every dataset project reinvents metadata and file layout
- export logic is tightly coupled to fetch logic
- Markdown, dataset cards, and Hugging Face upload logic are mixed together
- dataset builds are hard to reproduce
- there is no stable build manifest for auditing what was created

## 3. Goals

### Primary goals
- Define datasets declaratively through a build spec
- Assemble one or more sources into deterministic artifacts
- Generate publication-ready outputs
- Keep provenance and source metadata
- Support local preview before publication

### Non-goals
- Replacing `kpubdata` as the ingestion core
- Acting as an interactive end-user UI
- Solving all domain schema harmonization
- Becoming a workflow engine for arbitrary ETL

## 4. Target Users

### 4.1 Developer / OSS maintainer
Wants to build data artifacts from Korean public data with reproducible configuration.

### 4.2 Researcher / analyst
Wants to convert raw public data into notebook-friendly and shareable datasets.

### 4.3 Data curator
Wants to publish Markdown pages, Hugging Face datasets, and metadata cards consistently.

## 5. Product Principles

- Declarative over ad hoc scripts
- Deterministic builds over implicit mutation
- Provenance first
- Exporters are pluggable
- Build manifests are first-class artifacts
- Human-readable and machine-readable outputs should coexist

## 6. User Stories

- As a maintainer, I want to define a dataset in YAML/TOML so builds are reproducible.
- As a maintainer, I want to combine multiple sources into one artifact.
- As a curator, I want to preview records and generated Markdown before publishing.
- As a curator, I want validation errors before upload.
- As a curator, I want to export to Markdown, JSONL, Parquet, and Hugging Face.
- As a curator, I want build outputs to include metadata and provenance.

## 7. Functional Requirements

### FR-1 Build specification
The system must accept a declarative build specification with:
- dataset identity
- source definitions
- selection and mapping rules
- normalization toggles
- export targets
- metadata fields
- publication options

### FR-2 Source execution
The system must call `kpubdata` to fetch records from one or more datasets.

### FR-3 Assembly
The system must support:
- pass-through single-source datasets
- merge / union of compatible sources
- derived fields
- filtering
- ordering
- column selection
- dataset-level metadata enrichment

### FR-4 Validation
The system must validate:
- required build spec fields
- exporter-specific requirements
- missing credentials
- empty dataset outputs (configurable fail/warn)
- invalid metadata

### FR-5 Export
The system must support at minimum:
- Markdown
- JSONL
- Parquet
- Hugging Face dataset export package layout

### FR-6 Build manifest
The system must emit a manifest containing:
- build ID
- timestamps
- source inputs
- row counts
- schema summary
- output artifact locations
- warnings/errors

### FR-7 CLI
The system must provide a CLI for local and CI use.

## 8. Success Metrics

- one build spec can generate at least 3 artifact types
- build is deterministic for same spec + same source snapshot
- manifest is emitted for every run
- at least 2 exporters ship in MVP
- source provenance is visible in final outputs

## 9. MVP Scope

### In
- YAML build spec
- local filesystem outputs
- Markdown exporter
- Hugging Face layout exporter
- JSONL/Parquet exporters
- manifest generation
- preview command
- validation command

### Out
- multi-user collaboration
- browser UI
- remote workflow scheduler
- automatic schema harmonization across unrelated domains

---

## 관련 문서

### 이 저장소 내 문서
| 문서 | 설명 |
| :--- | :--- |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 시스템 아키텍처 설계 |
| [ROADMAP.md](./ROADMAP.md) | 프로젝트 로드맵 |

### KPubData Product Family
| 저장소 | 문서 | 설명 |
| :--- | :--- | :--- |
| [kpubdata](https://github.com/yeongseon/kpubdata) | [PRD.md](https://github.com/yeongseon/kpubdata/blob/main/PRD.md) | Core 제품 요구사항 |
| [kpubdata-studio](https://github.com/yeongseon/kpubdata-studio) | [PRD.md](https://github.com/yeongseon/kpubdata-studio/blob/main/PRD.md) | Studio 제품 요구사항 |
