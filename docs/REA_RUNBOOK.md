# REA and agent setup runbook

REA source: https://github.com/morluto/rea. Documentation: https://rea.tools/guides/ and https://github.com/morluto/rea/tree/main/docs.

## 1. Readiness on Windows 11

- Node.js 22.x >=22.19, 24.x >=24.11, or 26+ and npm; Python 3.11+ for repository scripts.
- Codex or Claude Code if agent-driven investigation is desired.
- Optional Ghidra **12.1.x** and JDK 21+ for Windows native PE x64 analysis, according to REA's **experimental Windows Ghidra** guide. Static .NET assembly inspection uses REA's managed-code tools and does not require Ghidra.
- Before attempting anything beyond publicly available docs, complete the rights check in `RIGHTS_AND_BOUNDARIES.md`.

## 2. Install

```powershell
node --version
npx rea-agents@latest setup
# Choose Codex / Claude Code explicitly and approve config changes.
# Restart the configured agent.
npx rea-agents@latest doctor
```

CLI alternative:

```powershell
npm install --global rea-agents
rea doctor --json
rea providers --json
```

For Windows native inspection with a supported installed Ghidra provider, consult https://github.com/morluto/rea/blob/main/docs/windows-ghidra-p0.md. Do not assume all of REA's macOS and Linux analysis features are available under Windows.

## 3. Recommended procedure: PUBLIC DOCUMENTATION FIRST

1. Run `python scripts/sync_official_catalog.py` and review `data/catalog.json`.
2. Record reference app version and category name, XML tool identifier, role, relevant help URL, and external dependencies.
3. Write a tool contract from published semantics: configuration, input and output schemas, defaults, conversions, edges/errors, side effects, determinism.
4. Write independent fixtures and expected results. Use public mathematical specs/library tests where suitable.
5. Implement and validate original code. Keep traceability to sources, not source-code transcription.

## 4. REA for authorised targets only

REA methods you can select when rights allow:

- JS/Electron: `rea analyze-javascript-application C:\\path\\to\\allowed-app --json` inventories application imports and boundaries.
- Managed .NET: `rea inspect-managed-artifact C:\\path\\to\\AllowedAssembly.dll --json` followed by `rea inspect-managed-members`.
- Native PE (provider required): `rea inspect C:\\path\\to\\allowed-native.exe --provider ghidra --format json` on an admitted target.
- Websites: REA browser investigations require target access and permission for the requested instrumentation. Static public documentation is enough for many tool contracts.

REA guided prompts include `investigate_feature`, `compare_application_versions`, `verify_reconstruction`, `audit_residual_unknowns`; verify current MCP catalogue before tool calls. REA may return incomplete/heuristic interpretations; check evidence, never treat AI explanations alone as proof.

## 5. Per-tool investigation record

Store under `research/<category>/<tool-slug>.md` (future commits). Required headings:

```
Target release / exact build:
Official tool name and XML identifier:
User role, add-on, version and external dependencies:
Permission/authorisation scope:
Source URLs and accessed timestamps:
Input anchors, output anchors, data schema:
Configuration fields, validation, defaults:
Error/warning behaviour; row and field order:
Determinism / numeric tolerance:
Expected independent reference fixtures:
Observed/reference results (only if permitted):
Open questions / unknowns:
Implementation state and test report refs:
```

## 6. Replay and acceptance

Commit only original test cases and lawful metadata. A comparison report must identify reference build, engine (AMP/original), deterministic seed, locale/timezone, settings, output SHA256/tolerances, failures and known uncertainties. No binary dumps, unlicensed output samples or secrets in Git.
