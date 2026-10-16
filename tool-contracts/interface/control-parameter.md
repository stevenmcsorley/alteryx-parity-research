# Control Parameter

> Flowbench-native partial implementation; **NOT a verified specification or Alteryx parity claim**.

## Flowbench-native support

In a Batch Macro child workflow, a `control-parameter` question names the parent
control-table column through `control_field`. Each row in the parent's control
input runs the child once, in input order. The corresponding `metadata.application.actions`
entry applies that value to an existing child tool configuration property before
the child workflow is validated and executed. Array indexes in action paths are
supported.

The desktop exposes the native Control Parameter in the Interface palette and
inspector. It is serialized as application metadata and is not shown in the
standalone analytic-app form. Running a child document directly without a Batch
Macro control context fails explicitly.

## Limits and compatibility

- This implementation reuses the existing Flowbench Batch Macro runtime; it is
  not an Alteryx Control Parameter XML adapter.
- Conditional control-parameter Actions, application Error Message nodes, and
  ordinary analytic-app Actions inside the same batch child are rejected rather
  than silently ignored.
- A control parameter must map to at least one Action target.
- Alteryx-specific XML configuration, source anchors and reference output
  semantics remain unverified. Import retains unsupported vendor configuration
  with diagnostics.

## Evidence

The executable Flowbench-native path is covered by
`test_macros.BatchMacroTests.test_native_control_parameter_uses_each_parent_row_and_updates_child_configuration`.
The generated contract and roadmap provide the current catalogue and reference
status. No Alteryx differential claim is made.
