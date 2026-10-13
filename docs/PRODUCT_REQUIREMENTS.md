# Product requirements: full catalogue parity

**Scope:** an independently implemented, desktop-first visual analytics product capable of reproducing the useful documented behaviour of *every named tool* in the reference Alteryx Designer catalogue. The goal is feature parity, not visual duplication of protected branding or code. Scope is comprehensive; delivery is staged, with acceptance testing tool by tool.

## Target product surfaces

### Desktop workbench

- Unlimited workflow tabs; typed, searchable, favourite-able tool palette organised by the complete category catalogue, with honest unsupported indicators until implementation.
- Infinite canvas with mouse/keyboard pan/zoom, snapping, align/distribute, horizontal and vertical flow, select/multiselect, reusable containers, annotations and undo/redo.
- Tool nodes with multiple labelled input/output anchors and topology validation. Wire inspection, data preview, lineage, deterministic node IDs, disabled/selected states.
- Dynamic configuration editor driven by typed schemas; validated expressions, autocomplete and field-aware suggestions.
- Run, cancel, debug, step/view intermediate output, memory and duration metrics, warnings/error navigation, partial failures and repeatable logs.
- Results grid: per-output anchor, schema and data profiles, histograms, null/unique counts, sort/filter, copy/export, sampled and full result modes.
- Accessibility: keyboard-first editing, screen reader naming, contrast, scaling; no dependence on colour-only states.
- Themes and localisation framework; own product branding and original assets.

### Workflow execution engine

- DAG execution with typed schemas, streaming and materialised boundaries; nested workflows and macros; cycle diagnostics and side-effect ordering.
- Explicit CSV/TSV, Excel, JSON, XML, Parquet, Arrow, database and network I/O adapters with defensive parsing and predictable failure modes.
- Type system: integer widths, float, fixed-point, booleans, binary, strings, dates/datetimes/time zones, geospatial and complex/null values. Explicit coercion and locale semantics.
- Formula DSL with typed AST, validation, unit tests, built-in function registry, namespaces and documented behavioural differences.
- Relational ops (join/union/aggregate/pivot/window), string/date parsing, fuzzy matching, spatial/geocoding, statistical/numerical, ML, time-series, optimisation, image/text processing and external integrations.
- Memory budget, spilling, parallelism, cancellation, checkpointability, deterministic execution policy, confidential fields and provenance.
- Plugin API that adds tools without hardcoding editor/engine implementation for each tool.

### All catalogued tool groups

No category is excluded: In/Out; Preparation; Join; Parse; Transform; In-Database; Reporting; Documentation; Spatial; Interface; Data Investigation; Predictive; AB Testing; Time Series; Predictive Grouping; Prescriptive; Connectors; Address; Demographic Analysis; deprecated Behavior Analysis; Calgary; Developer; Laboratory; Machine Learning; Text Mining; Computer Vision; AI Tools; Connect; SDK Examples.

**Vendor/API/data distinction:** tools invoking 3rd-party services, proprietary geocoding datasets, subscription ML services, licensed demographic/behavioral datasets or deprecated services require independently authorised substitutes and clear user-supplied credentials. A dummy response does not count. If exact behaviour cannot legally or technically be reproduced, mark `blocked`, document the non-equivalence and preserve the catalogue entry.

### Workflow interoperability

- Recognise named Alteryx extensions: `.yxmd` workflow, `.yxmc` macro, `.yxwz` analytic app, `.yxzp` packaged workflow, `.yxi` installed package, `.yxwg` workflow group, `.yxdb` data file; investigate `.yxft`, `.indbc`, `.cydb` and others where relevant.
- Read and preserve unknown nodes and configuration XML on import; do not silently discard; only claim functional compatibility after verified round-trip checks.
- Store our own canonical format and versioned migrations. Export target-native format only if legally supportable and technically verified.
- Develop native import adapters separately from format documentation / observed samples. Support macros, parameterised analytic apps, workflow templates and packaging references.

### AI capability (additive, not part of parity claim)

- Natural-language -> proposed workflow, with previews of node configurations and permission gates before execution.
- Explain why each tool is present and what data it accesses; typed error diagnosis, performance suggestions, self-serve documentation.
- Generate test cases, uncover edge cases and highlight under-specified tool contracts; mathematical outputs always computed by the deterministic engine.
- On-prem and approved remote model providers with opt-in transfer controls; never upload customer data by default.

## Data security, operations and enterprise readiness

- Local-first, optional encrypted credential vault. Secrets never serialized into workflow exports, logs or VCS.
- Least-privilege connector scopes, DCM-style connection abstractions, signed plugin distributions, sandboxed user scripts, network egress controls and audit events.
- Reproducible builds, SBOM, dependency scans, pinned versions, crash reporting opt-in, metrics off by default.
- Windows 11 first. Linux/macOS support must pass own release gates.
- Benchmarks: peak memory, p50/p95 run time, warm/cold startup, spill recovery, row count correctness and I/O throughput across low/medium/large datasets.

## Definition of done per tool

1. Official source identity and version recorded.
2. Config and node-anchor specification complete, with validation/errors and required dependencies.
3. Independently implemented tool running real data (not a mock).
4. Unit, property, integration, failure and serialization tests passing.
5. Evidence-based output comparison completed when permitted; numerical tolerance defined and justified.
6. Performance and security requirements satisfied for the tool class.
7. Documentation and sample workflow committed; no unacknowledged compatibility gaps.

## Cross-cutting release gates

- 100% of the pinned tool catalogue **accounted for** is not 100% parity. To claim full parity, every entry must be verified or transparently published as an exception; a release with exceptions is not called full parity.
- No tool implementation earns verification purely because the canvas icon renders or no error occurs.
- No hidden vendor cloud dependencies, trial/licence bypass, invented statistical results or undisclosed customer-data routes.
