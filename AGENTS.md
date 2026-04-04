# AGENTS.md — kpubdata-builder

## Mission

Implement KPubData Builder as the orchestration and artifact pipeline layer on top of `kpubdata`.

## Ground Rules

- Do not duplicate provider logic from `kpubdata`
- Keep build specs declarative
- Prefer deterministic behavior over magic
- Keep exporters pluggable
- Every build must emit a manifest
- Validation must fail early and clearly

## Priorities

1. spec models
2. validation flow
3. source execution using `kpubdata`
4. artifact model
5. markdown exporter
6. huggingface layout exporter
7. publish hooks

## Testing Expectations

- unit tests for spec validation
- golden tests for Markdown output
- manifest contract tests
- fixture-based source execution tests
