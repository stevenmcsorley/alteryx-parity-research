# Alteryx Action runner acceptance (owner-operated)

## Evidence status

The fixture below is authored by this project from structural observations of the installed Designer 2026.2.0.8 workflow format. The Flowbench engine has an independent test for its supported `UpdateValue / Simple` Filter-expression adapter. Neither that engine test nor the prior `AlteryxEngineCmd.exe` experiment establishes Designer analytic-app runner behavior. The command-line experiment did not apply `RuntimeProperties/Actions`.

Native Designer GUI control is unavailable in the current development session. No vendor-run output or error result is claimed. The repository owner must run the following procedure in Designer and return the requested evidence before this is described as reference-confirmed.

## Prepare and run the fixture

1. On Windows with Alteryx Designer **2026.2.0.8**, open PowerShell at the repository root.
2. Run `powershell -NoProfile -ExecutionPolicy Bypass -File tests/fixtures/action/prepare_designer_fixture.ps1`. The script writes a synthetic `.yxwz` under `%TEMP%\Flowbench-Action-Acceptance`; it includes no credentials or external data.
3. In Designer, use **File → Open Workflow** and open the printed `.yxwz` path. Confirm the canvas has Text Box, Action, Text Input, Filter and Browse tools. Confirm the Filter's True output feeds Browse. Do not edit the Action wiring or configuration.
4. Click **Run**. In the analytic-app runner, leave the default answer `[region] = "West"` unchanged and submit/run.
5. Record whether Designer accepts the app and whether the Browse result contains exactly one record: `region` (V_String), value `West`. The source has three records in order East, West, North. The filter starts as `[region] = "East"`; the Action destination is `4/Expression`. This expected result is derived from the authored fixture configuration, not yet an observed Designer result.
6. Run a second time with `[region] = "North"`. If the runner allows it, the Browse result should contain exactly one `North` record. This checks that the submitted value is applied for each run rather than persisted as a mutation to the saved workflow.
7. Optional negative check: enter malformed filter text `[` and submit. Record the exact runner/tool error, including whether failure occurs before or during workflow execution. Do not infer a general invalid-expression rule from this one observation.
8. Close Designer without saving. Reopen the fixture and confirm its configured Filter expression is still `[region] = "East"`.

## Compare with Flowbench

In the installed Flowbench desktop, use **Open workflow** to import the same generated `.yxwz`. The importer should report one supported Action adapter and expose the `Region to include` text input. Submit `[region] = "West"` and run. The Filter true output/Results should contain `West`; the false output should contain `East` and `North`. Reopen or inspect the source fixture and confirm it remains unchanged. Current Flowbench support is deliberately limited to one connected question, unconditional `UpdateValue`, `Simple` mode, a mapped Filter `Expression` target. Other Action variants are preserved and reported as unsupported.

## Capture and return evidence

Please provide:

- Designer build/version and Windows version.
- Screenshot of the opened canvas showing the Question → Action target relation and Filter/Browse data flow.
- Screenshot of the app prompt and submitted answer (the fixture has no secrets).
- Screenshot of Browse output for each accepted value, including field name/type and row count if shown.
- The result and exact error text for the optional malformed-input check.
- Screenshot showing the unchanged initial Filter expression after reopening without saving.
- Flowbench import diagnostics, prompt and result screenshots for the same values.
- If practical, export the synthetic Designer workflow after opening and return the XML **only after checking it contains no personal paths or secrets**. This helps establish whether the runner rewrites the expression/configuration.

Keep native Designer observations separate from Flowbench tests. Redact user names, machine names, local paths and any unrelated workflow content. Do not send Alteryx credentials or session data. Once received, record the observed result, date, build and evidence file references in `docs/MILESTONE_3_ACTION_EVIDENCE.md`; do not change the `reference-tested` ledger status unless the repository's evidence rules are met.

## Next reference-backed cases

After this simple-update fixture has been observed, add one authored fixture per variant before implementing it: formula-based update, field-selection update and each target-specific configuration update. For each, capture the initial target configuration, runner answer, post-run configuration, output rows and invalid-input behavior. A structurally observed XML path alone is not proof that the Designer app runner applies the variant.
