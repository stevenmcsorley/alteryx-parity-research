# Production architecture

Status legend: **[built]** implemented and tested in this repository; **[planned]** designed here, not yet built.

```text
┌──────────────────────── Desktop editor [built: desktop/] ─────────────────┐
│ Tauri shell · React/TypeScript · canvas (pan/zoom, anchors, wires)        │
│ palette ← tools.list/catalog.list · property panel ← JSON Schema forms    │
│ results grid ← previews · run log ← events · problems ← workflow.validate │
└───────────────▲──────────────────────────────────────────────────────────┘
                │ JSON-RPC 2.0 over stdio (newline-delimited)   [built: flowengine/service.py]
┌───────────────┴──────────── flowengine (Python) ─────────────────────────┐
│ workflow document (schemas/workflow.schema.json)              [built]     │
│ planner: schema validation → tool resolution → config validation →       │
│          anchor/cardinality checks → cycle detection → static schema      │
│          inference (problems carry node id + stable code)     [built]     │
│ executor: deterministic topo order · fail-fast · cancellation ·          │
│          table release · JSONL events · run manifest + digests [built]    │
│ plugin registry: manifests (anchors, JSON-Schema config, side effects,   │
│          determinism, dependencies) · catalogue mapping ·                 │
│          entry-point discovery for third-party tools          [built]     │
│ kernels: 59 kernels (pure Python, columnar Table)            [built]      │
│ formula language: lexer/parser/type-checker/compiler          [built]     │
└──────────────────────────────────────────────────────────────────────────┘
        │                       │                          │
  file adapters [built:    Arrow interop, streaming,  user code [built: Python/R/commands,
  CSV/JSON/Parquet/Arrow/  spill, parallel, history,  isolated processes]; DB, cloud,
  yxdb-in]                 profiles [built]           geo adapters [planned]
```

## Decisions (ADRs)

| ID | Decision | Rationale | Consequence |
|---|---|---|---|
| ADR-001 | Engine core in Python now; kernels are the semantic reference implementation | Fastest path to evidence-backed semantics; trivially testable; no build toolchain | Throughput is modest (pure Python). Hot kernels move to an Arrow-backed backend behind the same `Tool.run` contract once their semantics are pinned by conformance cases |
| ADR-002 | Own columnar `Table` (schema + column lists), immutable by convention | Explicit types/nulls; interface small enough to back with Arrow later | `Table` is the only data type crossing kernel boundaries |
| ADR-003 | Materialised node-at-a-time execution, deterministic topological order, fail-fast | Reproducibility and simple diagnostics first | Streaming/spill come later behind the executor; kernels already avoid global state |
| ADR-004 | Static schema inference = run the kernel on empty tables (pure tools); side-effecting or data-dependent tools override `infer` | One implementation of schema rules; no drift between "validate" and "run" | Runtime asserts output schema equals inferred schema (catches kernel bugs) |
| ADR-005 | Tool configuration is JSON validated by a per-tool JSON Schema in the manifest | Editor property panels are generated from the same schema; validation is shared | Unsupported options are simply absent from the schema and rejected — no silent stubs |
| ADR-006 | Catalogue entries without kernels resolve to `tool-unavailable` at validation | Full catalogue stays visible; nothing pretends to work | Editor shows every catalogue tool with its real status from `catalog.list` |
| ADR-007 | Reference semantics are adopted only with evidence: a differential case id is cited at the code site (`[ref:<case>]`) | Prevents guessing; makes every parity rule auditable | Unverified policies are listed in `docs/ENGINE_SEMANTICS.md` |
| ADR-008 | Our own workflow format (`flowengine.workflow` v1); vendor `.yxmd` import is a separate adapter | Clean canonical model with migrations; importer can preserve unknown nodes | Importer is planned; harness already emits reference documents for testing |
| ADR-009 | Editor talks to the engine only through the JSON-RPC sidecar | Process isolation, crash containment, language independence, same API for CLI/tests | The service is the compatibility boundary; it is versioned (`protocol`) |
| ADR-010 | Determinism: injected clock, stable sorts, seeded randomness (when added), digests over canonical text | Reproducible runs and comparable manifests | `run_workflow(clock=...)`; manifests exclude timings from digests |

## Workflow model (built)

`schemas/workflow.schema.json`: `{format, formatVersion, name, constants, nodes[{id, tool, toolVersion, config, label, disabled, position, annotation}], edges[{from:{node,anchor}, to:{node,anchor}}]}`. `tool` may be a kernel id (`core.filter`) or a catalogue id (`preparation:filter`). Edge order defines connection order for multi-connection anchors (Union). `workflow_digest` hashes semantics only (layout excluded).

## Plugin system (built)

A tool is a class with a `ToolManifest` and `run(ctx, config, inputs) -> {anchor: Table}`:

- `inputs`/`outputs`: `Anchor(name, label, required, multiple)`.
- `config_schema`: JSON Schema (draft 2020-12); defaults applied before validation; `check_config` for semantic checks.
- `side_effects` (`none|reads-files|writes-files|network|process`), `deterministic`, `dependencies`, `catalog_ids`.
- `RunContext`: node id, base directory, injected clock, `warn/info` events, cancellation, browse sink, limits, constants.
- Discovery: built-in `flowengine.tools.*` modules export `TOOLS`; third parties register via the `flowengine.tools` entry-point group. Duplicate tool ids or catalogue claims are rejected at registration.
- Versioning: nodes may pin `toolVersion`; a major-version mismatch is a validation problem.

## Execution and observability (built)

Events (`run-started`, `node-started`, `node-completed`, `node-skipped`, `node-failed`, `warning`, `info`, `run-cancelled`, `run-completed`) are kept in the result, mirrored to an optional JSONL file, the `flowengine` logger and an optional listener. The run manifest records engine/Python versions, workflow digest, per-node per-anchor row counts, field lists and canonical output digests, warnings and the error. Errors have stable codes: `document-invalid`, `tool-unavailable`, `config-invalid`, `schema-invalid`, `expression-error`, `anchor-*`, `cycle`, `data-error`, `cancelled`, `internal-error`.

## Visual editor (built; see docs/DESKTOP.md)

- **Shell:** Tauri (Rust) hosting a React/TypeScript UI; the engine runs as a sidecar process (`flowengine serve`).
- **Canvas:** node graph with typed anchors from manifests; connection validation mirrors planner rules (single vs multiple anchors); keyboard-first selection/move/connect; undo/redo as a command log over the workflow document.
- **Palette:** `catalog.list` — all 323 entries by category with status badges; unavailable tools are visible but not droppable into runnable state.
- **Property panel:** generated from each tool's JSON Schema (rjsf-style), with expression fields using the formula grammar (field completion from `workflow.validate` schemas).
- **Results:** per-anchor previews (`workflow.run` previews), schema view, row counts on wires from `node-completed` events, problems list navigates to node ids.
- **Accessibility:** all canvas actions keyboard reachable; status never conveyed by colour alone; screen-reader names from manifest display names.
- **Next protocol additions:** streaming events (`run.start` + `run.event` notifications), cancellation (`run.cancel`), paged results (`results.page`).

## Planned scale-out

Arrow-backed `Table` and DataFusion/Polars kernels for relational tools; spill-to-disk for sort/join/summarise; streaming executor for unbounded inputs; process-sandboxed R/Python adapters; database pushdown for In-Database tools; credential vault (OS keychain) for connectors. Each replacement must pass the same unit tests and differential cases.

## Milestone 2 additions (built)

| ID | Decision |
|---|---|
| ADR-011 | Compatibility profiles (`flowengine/profiles.py`) name a reference build and engine mode; kernels branch only on evidenced switches; the profile is recorded per run |
| ADR-012 | Streaming executes eligible linear segments in batches through the same kernels (row-local or explicit stateful streams); manifests are identical to batch runs |
| ADR-013 | Intermediate tables live in a `TableStore` with a memory budget and chunked spill files; peak memory is still bounded below by the largest single operation (external algorithms are future work) |
| ADR-014 | Parallelism batches ready nodes; outputs are identical for any worker count and event order is deterministic per worker count |
| ADR-015 | User code runs in separate processes with explicit per-run permission (`allow_processes`), scrubbed environments, time limits and named pinned Python environments |
| ADR-016 | Desktop: Tauri shell + React UI; engine as a JSON-RPC sidecar; stdio in the app, loopback HTTP/SSE for browser tests |
| ADR-017 | Interop is lossless by construction: unknown nodes and settings are preserved with their original XML and surfaced in reports and validation |
