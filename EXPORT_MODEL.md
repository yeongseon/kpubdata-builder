# Export Model — KPubData Builder

## Philosophy

Exporters consume a canonical artifact model and produce concrete files or publishable layouts.

They must not fetch source data directly.

## Canonical Export Input

Every exporter receives:
- artifact records
- metadata
- provenance
- schema summary
- optional statistics

## Built-in Exporters (MVP)

### Markdown
Produces:
- README-style Markdown
- schema table
- sample rows
- source provenance section

### JSONL
Produces:
- newline-delimited records

### Parquet
Produces:
- columnar dataset file

### Hugging Face Layout
Produces:
- `data/` files
- dataset card draft
- metadata manifest

## Publisher vs Exporter

- exporter = creates files/layout
- publisher = uploads or syncs to remote platform
