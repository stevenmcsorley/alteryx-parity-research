# Flowbench desktop application

Tauri 2 (Rust) shell + React 18/TypeScript UI. The UI never imports engine code: it talks JSON-RPC 2.0 to
`python -m flowengine serve` (stdio sidecar in the desktop build; loopback HTTP + Server-Sent Events in the
browser build used for automated UI tests). Source: `desktop/`.

![Example workflow after a run](images/flowbench-example.png)

## Features (all wired to the real engine)

* **Canvas:** drag tools from the palette, typed and labelled input/output anchors, connection rules
  (single vs multi-connection anchors, no self-loops, no cycles), pan/zoom/minimap, box and multi-select,
  containers (group/ungroup, disable), comments, undo/redo, copy/paste/duplicate, navigator, auto-layout for
  documents without positions, live run state per node, row counts on anchors and connections.
* **Palette:** all 323 catalogue entries, search, category filter, "implemented only", status badges derived
  from the evidence ledger (Ready / Ref-tested / Ref-diff / In progress / Unavailable / Blocked) and the reason a
  tool cannot be used.
* **Configuration:** forms generated from each tool's JSON Schema; field pickers per input anchor; expression
  editor with field insertion; Text Input grid; live validation (problems name the node); anchor table;
  incoming/outgoing schema previews; compatibility profile selection.
* **Execution and results:** run (F5), cancel (Esc), rerun; live events; results grid per node/anchor with filter
  and export; field metadata; messages (problems, import report, execution log with node focus); performance
  (per-node durations, wall time, spills); run history (SQLite).
* **Files:** open/save `*.flow.json`; import `.yxmd/.yxmc/.yxwz/.yxzp` (report shown); export `.yxmd` for
  supported tools (lists what cannot be exported).
* **Safety:** workflows containing Python, R or Run Command tools ask for permission before each run.

## Keyboard

| Keys | Action |
|---|---|
| F5 / Esc | Run / cancel |
| Ctrl+S, Ctrl+Shift+S, Ctrl+O, Ctrl+N | Save, save as, open, new |
| Ctrl+Z, Ctrl+Y (Ctrl+Shift+Z) | Undo, redo |
| Ctrl+A, Ctrl+C, Ctrl+V, Ctrl+D, Delete | Select all, copy, paste, duplicate, delete |
| Ctrl+G | Group selection in a container |
| Enter on a palette item | Add the tool |

## Build and run

Prerequisites: Python 3.12 with `pip install -r requirements.txt`, Node 22, Rust (MSVC), WebView2.

```powershell
cd desktop
npm ci
npx tauri dev                 # desktop app with hot reload
npx tauri build               # release exe + NSIS installer (src-tauri/target/release/bundle/nsis)
# browser build against a separately started engine:
python -m flowengine serve --http 8765        # from the repository root
npm run dev
```

The installer bundles the `flowengine` package, schemas and catalogue as resources. It does **not** bundle a
Python runtime: the app starts `python` (override with `FLOWENGINE_PYTHON`) and shows an "engine is not
reachable" screen with instructions if Python or the requirements are missing. Bundling an embedded Python
and code signing are open release tasks (MILESTONE_2_REPORT.md).

## Tests

* `npx vitest run`: model conversion, undo/redo, connection rules, grouping, run-result guards, status labels,
  CSV export (10 tests).
* `npx playwright test app.spec`: 9 end-to-end scenarios against the real engine: palette honesty,
  building/configuring/connecting/running from scratch, validation feedback, the example workflow with golden
  output, undo/redo/grouping, cancellation, save/reopen round trip, `.yxmd` import, code-tool permission.
* `cargo test` (src-tauri): sidecar protocol parsing and a real engine round trip.
