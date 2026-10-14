# Full acceptance runner

Run `python scripts/full_acceptance.py` from the repository root. The runner writes `full_acceptance.json` and `full_acceptance.md` under `artifacts/full-acceptance/`, including the generated 323-entry catalogue matrix, suite commands, durations, test counts, skipped counts, reference build records and output tails. It exits 1 on a runnable failure and 2 if any suite is blocked or intentionally not run; missing external tooling is never counted as a pass.

The runner currently executes repository validation, generated-ledger checks, all Python tests, desktop TypeScript/unit and Playwright tests, real PostgreSQL/SQL Server fixtures when Docker and OpenSSL are available, real R tests when R is installed, Rust tests when Cargo is installed, and E1/E2 differential plus imported-sample holdouts when the reference installation is available.

Set `FLOWBENCH_REFERENCE_ENGINE` to the authorised `AlteryxEngineCmd.exe` path when it is not at `%LOCALAPPDATA%\Alteryx\bin\AlteryxEngineCmd.exe`. The E1 and E2 harnesses are invoked separately. Holdout cases use `conformance/reference/holdout_samples.py`; those runs need the authorized installation's sample and data directories. Reference output is actual EngineCmd evidence and does not stand in for analytic-app GUI execution.

The 200,000-row benchmark is opt-in with `--include-performance`. Windows installer packaging and the install/upgrade/uninstall smoke test are opt-in with `--include-installer` and must run on an isolated Windows runner because they change the installation state. Database fixtures use random credentials, create isolated local containers, and are torn down in a `finally` path. If provisioning fails, teardown is still attempted.

Suite results use distinct `PASS`, `FAIL`, `SKIP`, `BLOCKED`, and `NOT RUN` states. Python's unittest aggregate can pass while reporting one or more skipped tests; those skip counts appear in their own report column, and the corresponding external-runtime suite remains separately blocked when unavailable. The generated product status fields are copied from `data/tool-status.json`; the runner does not infer tool implementation or parity from suite success.
