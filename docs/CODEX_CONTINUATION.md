# Codex continuation record

Updated: 2026-10-14

## Repository state

- Branch: `milestone-3-macro-interface-parity`
- PR #4: https://github.com/stevenmcsorley/alteryx-parity-research/pull/4 (open; do not merge without owner approval)
- Latest pushed checkpoint before current local changes: `780c580` (`Implement Arrange transform kernel`).
- The next local commit contains a narrow imported Action extension; preserve it.

## Delivered implementation IDs

- `preparation:data-cleanse-pro`: independent Data Cleanse Pro kernel (commit `1761365`), three attributed tests, not reference verified.
- `transform:arrange`: independent Arrange kernel (commit `780c580`), three attributed tests, not reference verified; native desktop schema configuration is available, but original XML translation is unsupported.
- `interface:action`: prior native Action editor and imported Filter UpdateValue path retained. Current local work adds imported `UpdateValue / Simple` targeting a Formula with exactly one `FormulaField`; multi-field ambiguity is rejected. Local structure evidence is documented, but no Designer GUI behavior claim is made.

## Verification

- Before current Action changes: Python suite 179 run, zero failed, one skipped (local R absent); status/roadmap generators and repository validation passed.
- Current Action-specific interop suite: 12 passed, including formula update and ambiguous target rejection.
- Full Python suite after current Action changes: 181 run, zero failed, one skipped.
- Generated status after current work: 323 catalogue entries; 63 registered kernels; 62 implemented, 2 in progress, 259 not started; 32 partial reference entries; zero fully verified; E1/E2 are 74/75 each with the existing Sort locale failure.
- CI for `780c580` is still running; do not report it as passing until complete.

## Exact Action behavior coverage and limits

- Imported Action: single connected question, unconditional UpdateValue/Simple targeting Filter Expression or a single FormulaField expression. The workflow is modified only on a private per-run copy.
- No imported Designer app runner differential. Formula target path was structurally inspected in an installed sample; execution tests are project-authored Flowbench tests only.
- Condition-driven, UpdateValueFormula, Dynamic, UpdateInput, UpdateMap, UpdateSelect, UpdateRawXml, container/detour, multiple-question and ambiguous multi-Formula actions remain unsupported and should warn/preserve XML.

## Exact next implementation action

Inspect `join:fuzzy-match` contract, installed sample configs and runtime data prerequisites. Separate public documented modes from observed XML structure. Because official docs identify reference-runtime matching resources (phonetic, frequency and nickname tables) and do not define full scoring output semantics, implement only a bounded mode whose behavior can be independently specified and tested, with explicit unsupported configs; do not call it Alteryx parity. Add desktop usability, null/Unicode/duplicate/tie/empty cases, then proceed to remaining Developer and Interface tools.
