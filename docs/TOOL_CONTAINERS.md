# Flowbench Tool Containers

Flowbench's native Tool Container is an editor-level grouping, not a row-processing kernel. Select one or more top-level canvas nodes, choose **Documentation → Tool Container**, then use **Group selected nodes**. The group can be renamed, collapsed or expanded, disabled or enabled, and ungrouped in the Inspector. Container metadata is serialized under `metadata.ui.containers`; disabling a container marks its child tools disabled for workflow execution. Grouping and ungrouping preserve child positions and are undoable.

Flowbench AI can include `group`, `configure_container` and `ungroup_container` operations in a validated graph patch. Grouping accepts top-level node references from the active workflow snapshot or nodes added earlier in the same patch. Nested child references are rejected rather than silently reparented. A disable-state change is surfaced as an execution consequence in the proposal.

This native editor functionality does not establish compatibility with Alteryx Tool Container XML. The observed vendor configuration includes `Caption`, `Disabled`, `Folded` and `Style`; Flowbench currently does not map imported `Folded`/`Style` variants or claim reference parity. Existing supported native metadata survives Flowbench save/reopen.

Verification is in `desktop/e2e/app.spec.ts` (palette grouping, Inspector controls and collapse visibility), `desktop/src/ai/workflowPatch.test.ts` (staged AI grouping/configuration/ungrouping), and `desktop/src/state/store.test.ts` (nested ungroup coordinate preservation). These are Flowbench implementation checks, not Alteryx differential observations.
