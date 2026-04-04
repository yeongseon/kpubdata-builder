# KPubData Builder — Korea Public Data Builder

**KPubData Builder (Korea Public Data Builder)** is the dataset artifact pipeline
that sits on top of [`kpubdata`](https://github.com/yeongseon/kpubdata).

It turns normalized Korea public data records into publishable artifacts such as:

- Markdown datasets and reports
- Hugging Face datasets
- JSONL / Parquet / CSV exports
- Dataset cards and metadata manifests

## Product family

| Package | Role |
|---|---|
| [`kpubdata`](https://github.com/yeongseon/kpubdata) | Korea Public Data access + parsing + normalization core |
| [`kpubdata-builder`](https://github.com/yeongseon/kpubdata-builder) | Dataset assembly + export pipeline |
| [`kpubdata-studio`](https://github.com/yeongseon/kpubdata-studio) | Visual interface for authoring and running builds |
