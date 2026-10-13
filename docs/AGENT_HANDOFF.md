# Codex / Claude Code handoff

Give this text to the coding agent from the repository root.

---

You are the lead engineer implementing a full-scope, independently coded, desktop-first data analytics workflow system functionally modelled on the complete published Alteryx Designer Desktop tool catalogue. Do not misrepresent the present repository as containing a running application or completed reverse engineering.

1. Read `README.md`, every `docs/*.md`, and validate `data/catalog.json`. Pinned target is the Designer 2026.1.4.126 reference build, while the `/current` catalogue may drift.
2. Do not remove any of the 323 category/tool entries. Treat duplicate category appearances as one potential implementation with two catalogue presences, to be confirmed.
3. Where network is available, run `python scripts/sync_official_catalog.py` to cross-check manually transcribed XML tool IDs and roles and enrich per-tool public help URLs. Verify the merged catalogue, check diffs and explicitly resolve any parser/source drift. Do not silently drop entries.
4. Use REA only for software and techniques whose analysis is authorised. The user has not provided such authorisation merely by wanting a competitor alternative. Do not inspect Alteryx restricted binaries, circumvent licensing or import decompiled code into the application without an explicit documented rights basis.
5. For each tool, create an independently authored, precise contract documenting configuration, input/output anchor schema, semantics, nulls, row order, errors, dependencies and version. Distinguish official specification from unverified assumptions.
6. Design a production-capable workflow editor, typed engine, plugin manifest, package/import/export layer, SDK and results inspector as set out in `docs/PRODUCT_REQUIREMENTS.md` and `docs/ARCHITECTURE.md`. Avoid fabricated outputs, hard-coded demos, disconnected UI controls, hidden mocks and pretend parity.
7. Build the engine and implement catalogue tools in dependency order while retaining full scope. Each must run real inputs and be independently testable. External API/data-dependent tools must use legitimate credentials or clearly declare blocked, never pretend to work.
8. Keep every conformance result versioned, include source, exact configuration, inputs and output hashes. `verified` status requires passing tests; do not update states optimistically.
9. Report precise counts: catalogued, specified, implemented, and verified separately; report blocked separately, and record every gap. If deadlines are encountered, ship only honestly complete increments without claiming parity.
10. Before each code modification, check repository guidance and existing tests; before a release, run unit/integration/security, export compatibility and packaging checks. Do not paste vendor source code/assets/manual text wholesale into this repo.

Immediate milestone: perform source inventory enrichment, assess permission/documentation completeness, produce the canonical tool-contract schema, implement project scaffolding and tests, then demonstrate a real end-to-end typed CSV -> Filter -> Join -> Summarize -> CSV workflow with tests. This is the first dependency milestone, **not a scope reduction** or permission to abandon any catalogue entry.
