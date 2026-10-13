# User-code runtimes (Python, R, external programs)

## Security model

Python, R and Run Command execute outside the engine process. They run only when the run grants
`allow_processes` (CLI `--allow-processes`; the desktop app asks the user each run, naming the tools). Without
the grant they fail with `permission-denied`. Child processes get a scrubbed environment (no inherited tokens or
credentials; see `runtimes.clean_env`), a private temporary working directory, a time limit and cancellation. They are not
OS sandboxes: filesystem and network access follow the user's permissions. Do not run untrusted workflows.

## Python (baseline 3.12)

* `environment: "default"` uses the engine's interpreter (Python 3.12 in this project).
* Named environments live in `~/.flowbench/envs/<name>` (override with `FLOWENGINE_HOME`):

  ```powershell
  python -m flowengine env create analytics -r requirements-analytics.txt   # pinned list
  python -m flowengine env list
  ```

  Creation records `flowbench-env.json` with the Python version, requested requirements and the resolved
  `pip freeze` lock, so upgrades are explicit and reproducible. Any interpreter can create an environment
  (`--python path\to\python.exe`) for future runtime upgrades.
* Code reads and writes tables with `from flowbench_io import read_input, write_output, input_count`
  (`read_input(n, as_frame=True)` returns a pandas DataFrame if pandas is installed in that environment). The
  exchange format is typed JSON lines, so environments need no third-party packages. Outputs 1–5; outputs not
  written are empty.

## R

* `Rscript` is discovered from `FLOWENGINE_RSCRIPT`, `PATH` or standard Windows install folders
  (`python -m flowengine env rscript`).
* Tools declare `packages`; each is checked with `requireNamespace` and its version is printed to the run log
  before user code runs.
* Exchange is CSV (`read_input(n)` / `write_output(n, df)`); output fields are text.
* **Status:** no R is installed on the development machine, so the R tool has no passing evidence and stays
  `in-progress`. Its test runs automatically when R is present.

## Statistical tools (Predictive, Time Series, Machine Learning, Text Mining)

These are not implemented yet. The design is that each is a kernel bound to a **named, versioned runtime**
(a Python environment with pinned scientific packages, or an R library snapshot), with the runtime identity
and package versions recorded in the run manifest so numerical results are reproducible. The reference
product implements many predictive tools as R macros; conformance will need numeric tolerances that are
justified per algorithm (docs/REFERENCE_TESTING.md).
