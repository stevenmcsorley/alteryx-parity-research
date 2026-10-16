# Tree

> **Research record; NOT a working tool implementation or verified specification.** Flowbench provides the separate native leaf-path question described below; that does not establish Alteryx Tree interoperability.

## Identity

- Catalogue ID: `interface:tree`
- Category: Interface
- Published XML identifier: `AlteryxGuiToolkit.Questions.Tree.Tree` (manual transcription; verify against installed reference)
- Published user role: Full
- Reference build: Designer 2026.1.4.126 (installed-build comparison outstanding)
- Official catalogue: https://help.alteryx.com/current/en/designer/tools/designer-tools-list.html
- Specific reference: **NOT YET VERIFIED** — run public catalogue sync
- Implementation: **in-progress** — native hierarchical leaf-path question, run validation and Action binding are implemented; imported Alteryx Tree variants remain unsupported.
- Machine-readable contract: `tree.contract.json`
- Conformance verification: **not-tested**

## Flowbench-native behavior

- Configure hierarchical leaf choices as canonical `Parent > Child` paths.
- Select one declared leaf in the analytic-app prompt; its full path string can update a connected Action target.
- Reject duplicate paths, selectable parent paths, invalid defaults and undeclared answers.
- Native behavior is described in [INTERFACE_TREE.md](../../docs/INTERFACE_TREE.md).

## Sources and observations

The native engine adapter has a focused binding/validation regression and the desktop E2E runs the question form and workflow. These tests establish Flowbench behavior only. Alteryx Designer Tree source configuration and XML mapping are not verified.

## Conformance evidence

No Alteryx differential cases exist. Do not claim compatibility for geography allocation, variable allocation, file-system browsing or custom XML/database tree sources.
