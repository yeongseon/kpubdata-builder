# API Contract — KPubData Builder

## CLI Commands

### validate
Validates a build specification.

### preview
Runs a partial build and shows schema/sample output.

### build
Runs a full build and emits artifacts plus manifest.

### publish
Publishes artifacts to configured targets.

## Service-level Operations

Builder service should expose operations equivalent to:
- `validate_spec(spec_path)`
- `preview_build(spec_path)`
- `execute_build(spec_path)`
- `publish_build(spec_path, target=None)`

## Manifest Contract

Manifest output must include:
- `build_id`
- `spec_digest`
- `sources`
- `artifact_paths`
- `record_count`
- `warnings`
- `errors`
