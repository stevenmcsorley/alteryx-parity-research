# Flowbench Runner

`core.runner` (catalogue entry `developer:runner`) runs the workflow file named by each incoming record,
one at a time and in record order. `path_field` must be a text field. Paths may name Flowbench
`.flow.json` documents or importable Alteryx workflows (the macro loader's rules apply). Relative paths
resolve against the workflow directory, and each child runs with its own directory as base.

- **Permissions.** Runner is a process-class tool. It runs only when the run grants `allow_processes`
  (CLI `--allow-processes`; the desktop asks), and the permission preflight lists it with the other
  process tools. Child runs receive exactly the parent run's permission flags, so a child never gains
  a permission the user did not grant. A child effect that was not granted fails safely and is reported.
- **Outputs.** Success and Failure each carry an event log: `Workflow`, `Status`, `Sequence`, `Event`,
  `NodeID`, `Message`. Events are node completions, skips and failures, warnings, info messages and
  run completion. Status is `succeeded`, `failed`, `invalid` (unloadable, failed validation, recursive
  or too deep) or `not-run`.
- A failed child does not fail the parent. With `on_failure: stop`, later records are logged as
  `not-run` on Failure and are not executed. Cancelling the parent cancels the active child.
- A workflow already in the current call stack is reported as invalid instead of being re-entered.
  `max_depth` (default 8) bounds nested Runner chains.

Child outputs are not retained in memory. Their files and other effects are the result. These are
Flowbench semantics informed by the public description of the CReW Runner macro. Its additional
options and Alteryx XML import are not implemented or claimed. Tests: `tests/test_runner.py`.
