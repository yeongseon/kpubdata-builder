# Architecture — KPubData Builder

## 1. Role

KPubData Builder sits between `kpubdata` and publication targets.

```text
Public APIs
  -> kpubdata
  -> kpubdata-builder
  -> Markdown / HF / files
```

## 2. Architectural Principle

Builder is an orchestrator.

It should not:
- reimplement provider adapters
- own public API access logic
- become a general-purpose ETL framework

It should:
- load a build spec
- fetch via `kpubdata`
- assemble records
- validate outputs
- export artifacts
- emit a manifest

## 3. Layers

### 3.1 Spec Layer
Parses and validates dataset build specifications.

### 3.2 Execution Layer
Coordinates source fetches and transformations.

### 3.3 Assembly Layer
Produces a dataset artifact model.

### 3.4 Export Layer
Converts assembled artifacts to concrete outputs.

### 3.5 Publication Layer
Pushes outputs to destinations such as Hugging Face.

## 4. Major Components

- `spec_loader`
- `validator`
- `source_executor`
- `assembler`
- `artifact_model`
- `exporters`
- `publishers`
- `manifest_writer`

## 5. Boundary with kpubdata

### kpubdata responsibilities
- provider adapters
- public API access
- raw response handling
- parsing and format normalization

### kpubdata-builder responsibilities
- dataset definition
- artifact assembly
- export generation
- publication workflow

## 6. Boundary with Studio

Builder must expose stable machine interfaces that Studio can call:
- validate spec
- preview build
- execute build
- inspect manifest
- list exporters
