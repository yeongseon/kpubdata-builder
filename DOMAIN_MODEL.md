# Domain Model — KPubData Builder

## Core Entities

### BuildSpec
Declarative configuration for producing an artifact.

Fields:
- `dataset_id`
- `title`
- `description`
- `sources`
- `transforms`
- `exports`
- `metadata`
- `publish`

### SourceRef
Reference to a `kpubdata` dataset query.

Fields:
- `provider`
- `dataset`
- `params`
- `normalization_mode`
- `alias`

### ArtifactDataset
The assembled logical dataset before export.

Fields:
- `records`
- `schema`
- `metadata`
- `provenance`
- `statistics`

### ExportTarget
A concrete artifact output definition.

Examples:
- markdown
- jsonl
- parquet
- huggingface_layout

### BuildManifest
Execution summary for auditing and reproducibility.

Fields:
- `build_id`
- `started_at`
- `finished_at`
- `inputs`
- `outputs`
- `warnings`
- `errors`
- `row_counts`
