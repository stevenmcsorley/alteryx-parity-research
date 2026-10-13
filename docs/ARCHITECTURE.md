# Technical architecture proposal

```text
Desktop (Tauri + React/TypeScript + node graph library)
     | schema/config API, canvas, intermediate rows, diagnostics
     v
Workflow and plugin service (Rust or Python orchestration; versioned JSON contracts)
     | graph validation -> typed plan -> scheduled execution -> lineage
     v
Execution engine (Rust / Apache Arrow + DataFusion + DuckDB/Polars adapters)
     |             |                |               |
   typed data   expressions      plugin host       credential vault
     |             |                |               |
 file / DB IO   AST / funcs      geo/ML/R/Python    scoped connectors
     v
Results/cache/spill + provenance ledger + evidence-backed conformance harness
```

The technology above is a proposed design, not a completed implementation or proof of parity. Consider independent licensing and ABI consequences of each library before adopting.

## Canonical workflow model

- `Workflow`: `{id, formatVersion, nodes, edges, metadata, enginePolicy, variables}`
- `Node`: `{id, toolId, toolVersion, display, configuration, inputPorts, outputPorts}`
- `Edge`: `{from: {node, port}, to: {node, port}, schemaPolicy}`
- `ToolManifest`: `{id, category, displayName, configSchema, inputAnchors, outputAnchors, resources, sideEffects, dependencies, deterministic}`
- `RecordBatch`: Arrow-style columns, explicit schema and record order metadata.
- `RunEvent`: `{runId, nodeId, eventType, started, completed, severity, counts, provenance}`.

## Semantic engineering decisions to research, never guess

- Input schema drift; coercion precedence; string normalisation; null vs empty; truncation; numeric overflow and rounding; timezone and daylight saving.
- Join duplicate multiplication, order stability, surrogate IDs, case-sensitive equality, locale collations, wildcard/range filter semantics.
- Expression evaluation order, short-circuiting, implicit cast differences, null-propagation, error and warning anchors.
- Row packetisation, buffering and parallelism; source-order reliance; deterministic sampling seeds; cancellation cleanup.
- Spatial CRS units, geodesic/spherical distance, polygon boundaries, routing API data provenance and licensed datasets.
- Stat algorithms, estimator variants, default solvers/hyperparameters, convergence, confidence intervals, pseudo-random seeds.
- Report rendering, type formatting, embedded fonts, charts, layout units and output fidelity.

## Internal boundaries

1. `ui/`: original React components; pure graph/editor model; no business logic in widgets.
2. `engine/`: typed execution IR and operator catalogue; no secrets or vendor UI code.
3. `adapters/`: transport/protocol and data-format plugins; side-effect classification.
4. `tool-contracts/`: machine-readable schemas and behavioural specifications, one per catalogue tool.
5. `conformance/`: input fixtures, reference outputs, compare engine, test reports and provenance.
6. `docs/`: reference mapping, unknowns, rights checks and acceptance decisions.

## Testing strategy

- Golden exact matches for strings/integers/boolean/schemas when semantics are known.
- Tolerance-based floating-point checks for algorithms with documented numerical error thresholds; distribution-level evaluation where stochastic output is legitimately expected.
- Import/export round trips and split/merge benchmarks; property-based tests for type/null combinatorics.
- Version-specific behaviour flags instead of arbitrary branches inserted after observing failing fixtures.
- Adapters that need paid services are contract-tested with user-owned sandbox services or permitted test accounts; do not invent responses.

## Scalability and extensibility

Separate tool discovery from execution. Plugin registration should allow multiple versions of the same tool; workflow opening selects required semantic version. A tool may be documented and catalogued but not yet available; UI must visibly label it unavailable and execution must fail clearly rather than acting as a stub.
