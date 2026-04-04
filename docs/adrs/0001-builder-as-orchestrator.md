# ADR 0001 — Builder as Orchestrator

## Decision
Treat `kpubdata-builder` as an orchestration layer, not a replacement for `kpubdata`.

## Rationale
Keeping source access in `kpubdata` avoids duplication and keeps responsibilities clear.
