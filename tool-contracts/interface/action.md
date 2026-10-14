# Action

> **Research record; NOT a verified specification.** An independent workflow application adapter exists for the limited supported variant below; this is not full Action parity.

## Identity

- Catalogue ID: `interface:action`
- Category: Interface
- Published XML identifier: `AlteryxGuiToolkit.Action.Action` (observed in installed 2026.2.0.8 workflows)
- Published user role: Full
- Reference build: Designer 2026.2.0.8 (structural workflow observations only)
- Official catalogue: https://help.alteryx.com/current/en/designer/tools/designer-tools-list.html
- Specific reference: [Action Tool](https://help.alteryx.com/current/en/designer/tools/interface-tools/action-tool.html)
- Implementation: **in-progress**
- Machine-readable contract: `action.contract.json`
- Conformance verification: **not-tested**

## Confirmed structure and executable subset

- Action nodes use `AlteryxGuiToolkit.Action.Action`, have empty node Configuration, and expose incoming `Question` and `Condition` connections plus outgoing `Action` configuration links.
- Rules and questions are serialized under `Properties/RuntimeProperties/Actions` and `Questions`.
- Executable subset: one connected question → unconditional `UpdateValue` / `Simple` → mapped Filter `Expression`.
- Interface nodes, connections and RuntimeProperties are retained during export, with ToolIDs remapped. Password question defaults are redacted.
- Independent executable/import/export tests: see `action.contract.json` and `tests/test_fe_interop.py`.
- Structural observations and the project-authored EngineCmd observation are recorded in `data/action-reference-evidence.json`.

## Sources and observations

The installed Designer 2026.2.0.8 examples establish XML structure and observed action type/destination names. A project-authored synthetic `.yxwz` run through AlteryxEngineCmd E1 did not apply `RuntimeProperties/Actions`; this invocation is not the Designer analytic-app runner and is not a behavioural compatibility pass. The native Designer runner was not available for this session.

Unsupported and explicitly warned: `UpdateValueFormula`, `Dynamic`, `UpdateInput`, `UpdateMap`, `UpdateSelect`, `UpdateRawXml`, condition-driven actions, multi-question expressions, and non-Filter targets. No Action E1/E2 differential case is claimed. Desktop Action palette/configuration and Designer app-runner acceptance remain open.

## Conformance evidence

Not tested. Do not mark verified without a report, input/config hashes and a reproducible comparison rule.
