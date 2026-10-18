# Tool Container

> **Native Flowbench editor adapter; NOT a working tool implementation for Alteryx and NOT a verified specification.**

## Identity

- Catalogue ID: `documentation:tool-container`
- Category: Documentation
- Published XML identifier: `AlteryxGuiToolkit.ToolContainer.ToolContainer` (manual transcription; verify against installed reference)
- Published user role: Basic, Full
- Reference build: Designer 2026.1.4.126 (installed-build comparison outstanding)
- Official catalogue: https://help.alteryx.com/current/en/designer/tools/designer-tools-list.html
- Specific reference: **NOT YET VERIFIED** — run public catalogue sync
- Implementation: **implemented** — native canvas grouping, collapse, disable and AI patch operations are implemented; no row kernel is applicable.
- Machine-readable contract: `tool-container.contract.json`
- Conformance verification: **not-tested**

## Flowbench-native behavior

- Select top-level canvas nodes and group them under a caption.
- Rename, collapse/expand, disable/enable, and ungroup from the Inspector.
- Persist native container structure in `metadata.ui.containers` when saving a Flowbench workflow.
- AI graph patches can group top-level nodes, configure the group, and ungroup it as part of the same validated undoable edit.
- Disabling a container disables its child tools at execution time.
- Nested container ungroup preserves child coordinates relative to the outer parent.

## Sources and observations

The native editor behavior is documented with implementation evidence in [TOOL_CONTAINERS.md](../../docs/TOOL_CONTAINERS.md). It is covered by a desktop Playwright acceptance and focused TypeScript state/AI-patch tests. This does not verify Alteryx runtime compatibility.

## Conformance evidence

Alteryx reference comparison has not been performed. XML `Folded` and `Style` mapping remain unsupported/unverified; do not claim parity.
