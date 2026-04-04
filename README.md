# KPubData Builder

KPubData Builder is the pipeline and artifact-building layer on top of `kpubdata`.

It turns normalized public data records into publishable artifacts such as:
- Markdown datasets and reports
- Hugging Face datasets
- JSONL / Parquet / CSV exports
- dataset cards and metadata manifests

## Product family

- `kpubdata`: access + parsing + format normalization core
- `kpubdata-builder`: dataset assembly + export pipeline
- `kpubdata-studio`: UI for configuring and running builder workflows
