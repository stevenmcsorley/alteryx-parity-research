# Milestone 2.1 — owner-run Windows desktop acceptance

This is the remaining **manual** acceptance gate. The CI installer smoke test, Python/DB suites, and browser
tests are automated evidence; they do not count as operating this database workflow in the installed Tauri app.
The repository owner should perform the procedure below on Windows and send the evidence bundle for review.
Milestone 2.1 stays incomplete until those results have been checked and recorded in the milestone report.

## Prerequisites

- Windows with Docker Desktop running and enough memory for PostgreSQL 16 and SQL Server 2022 Developer.
- This repository checked out at `milestone-2-desktop-and-engine` (or the reviewed PR #2 head).
- Python 3.12 with the repository requirements installed, Docker CLI, and OpenSSL. Git for Windows supplies
  OpenSSL; the fixture setup script adds its usual Git installation path if needed.
- The Flowbench Windows NSIS installer from the successful [clean-runner Actions run 38460982270](https://github.com/stevenmcsorley/alteryx-parity-research/actions/runs/38460982270).
  Install it normally. The installed application includes managed Python; system Python is not needed by the app.
- Do this only with the synthetic fixture containers named `flowbench-test-pg` and `flowbench-test-mssql`.
  `Up` replaces those named containers only after an explicit typed confirmation. Do not point the procedure at
  any non-test database.

Open PowerShell at the repository root. If the current execution policy blocks the script, use a process-only
policy override:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\tests\integration\desktop_acceptance.ps1 -Action Up
```

The setup invokes the existing `tests/integration/db_env.py up`, then connects using the least-privilege
`fb_reader` logins to verify readiness and exact fixture counts. PostgreSQL is checked with TLS certificate
and hostname verification. SQL Server is checked over the Docker-published loopback port using the fixture's
existing no-TLS configuration. The script displays host, random port, database, schema/table, login, TLS mode,
CA path, and expected timeouts; it never prints a password. It reads `tests/integration/.db_env.json`, which is
ignored by Git and must not be committed or attached to evidence.

Copy one reader password to the Windows clipboard only when entering it in the app. The value is not written to
the terminal:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\tests\integration\desktop_acceptance.ps1 -Action CopyReaderPassword -Database postgresql
# Paste into the PostgreSQL password field in Flowbench.
Set-Clipboard -Value ''

pwsh -NoProfile -ExecutionPolicy Bypass -File .\tests\integration\desktop_acceptance.ps1 -Action CopyReaderPassword -Database sqlserver
# Paste into the SQL Server password field in Flowbench.
Set-Clipboard -Value ''
```

Do not use `postgres`, `sa`, or either writer login in Flowbench. The elevated SQL Server fixture credential is
used only inside the guarded `Mutate`/`Restore` helper and is never displayed or saved in a workflow.

## Create and test the read-only connections

Start the **installed** Flowbench application and open **Databases** in the toolbar. Create these two
connections; leave **Allow database writes** unchecked on both.

| UI field | PostgreSQL | SQL Server |
|---|---|---|
| Name | `M21 PostgreSQL Reader` | `M21 SQL Server Reader` |
| Database type | PostgreSQL | Microsoft SQL Server |
| Host | `localhost` | `127.0.0.1` |
| Port | Printed by `-Action Status` | Printed by `-Action Status` |
| Database | `flowbench_test` | `flowbench_test` |
| User | `fb_reader` | `fb_reader` |
| Password | Paste using the clipboard steps above | Paste using the clipboard steps above |
| TLS | `Verify certificate and host` (`verify-full`) | `Disable` (fixture is local loopback only) |
| CA certificate path | Path printed by `-Action Status` | Leave blank |
| Connection timeout | `10` seconds | `10` seconds |
| Query timeout | `120` seconds | `120` seconds |
| Allow database writes | Off | Off |

Save each connection and click **Test**. Both must report a successful connection. The password input should be
blank when you reopen an existing saved connection; the OS credential vault supplies the saved credential.
Clear the clipboard immediately after each paste.

## Build the workflow in the canvas

Create a new workflow named exactly `M21 Customer Order Manual Acceptance`. Save it as
`M21_Customer_Order_Manual_Acceptance.flow.json` in a new evidence folder outside the repository, for example
`$env:USERPROFILE\Desktop\FlowbenchAcceptanceEvidence`. Keeping it outside the checkout prevents accidental
commits of local run evidence. Workflow files contain connection references only, but still inspect the file
before sharing it.

Drag the two **Database Input** extension tiles from the palette's **Database extensions** section, then drag
the catalogue tools **Join**, **Filter**, **Formula**, **Summarize**, and **Browse** onto the canvas. Configure
them as follows. In the Inspector, set the visible node labels to `customers`, `orders`, `join`, `filter`,
`formula`, `summarize`, and `browse` respectively. The settings shown are the exact stored values; omit
unlisted optional properties.

### `customers` — Database Input

- Connection: `M21 PostgreSQL Reader`
- Source: `Table or view`
- Schema: `sales`
- Table: `customers`
- Click **Preview live data**. Expect 41 rows and fields `customer_id`, `name`, `region`, `credit`,
  `signup_date`, `active`; the fixture includes the Unicode name `Zoë Ünïcode` and null values.

### `orders` — Database Input

- Connection: `M21 SQL Server Reader`
- Source: `Table or view`
- Schema: `ops`
- Table: `orders`
- Click **Preview live data**. The preview is capped at 100 rows (the table contains 120) and has fields
  `order_id`, `customer_id`, `order_date`, `qty`, `unit_price`, `channel`. The workflow run must read all 120.

### `join` — Join

```json
{
  "by": "fields",
  "keys": [{"left": "customer_id", "right": "customer_id"}],
  "case_insensitive": false,
  "right_prefix": "Right_",
  "drop_right_keys": true,
  "left_fields": ["customer_id", "name", "region"],
  "right_fields": ["order_id", "customer_id", "qty", "unit_price"]
}
```

The Join output has six fields, in order: `customer_id` (int32), `name` (string), `region` (nullable string),
`order_id` (int32), `qty` (int16), `unit_price` (fixed decimal with scale 2). `drop_right_keys` removes the
duplicate right-side `customer_id`.

### `filter` — Filter

Choose the basic comparison mode:

```json
{"field": "unit_price", "operator": ">=", "value": 20}
```

Use the **true** output for the next connection. Leave the **false** output unconnected.

### `formula` — Formula

Add one formula:

```json
{
  "formulas": [{
    "field": "Revenue",
    "expression": "[qty] * [unit_price]",
    "type": "fixed_decimal",
    "size": 18,
    "scale": 2
  }]
}
```

### `summarize` — Summarize

Add these actions in this order and leave `sort_groups` enabled (true):

```json
{
  "actions": [
    {"field": "region", "op": "group_by", "output": "Region"},
    {"field": "Revenue", "op": "sum", "output": "GrossRevenue"},
    {"field": "order_id", "op": "count", "output": "OrderCount"}
  ],
  "sort_groups": true
}
```

The final schema is `Region` (nullable string), `GrossRevenue` (fixed_decimal(38,2)), and `OrderCount` (int64).
The null `Region` group is expected and sorts first.

### `browse` — Browse

Leave its configuration empty. Connect the Join's **joined** anchor (not either unjoined anchor) to Filter;
then connect Filter **true** → Formula → Summarize → Browse.

The full edge list is:

| From | To |
|---|---|
| customers.output | join.left |
| orders.output | join.right |
| join.joined | filter.input |
| filter.true | formula.input |
| formula.output | summarize.input |
| summarize.output | browse.input |

Save again after configuration and confirm the toolbar reports no validation problems.

## Run and compare the baseline

Run with **Run** or F5. It must succeed and fetch the source rows afresh. Select the Browse capture in the Results
output picker; it must contain five rows and three columns. The row counts at each step are:

| Step | Expected rows |
|---|---:|
| PostgreSQL customers input | 41 |
| SQL Server orders input | 120 |
| Joined records (Join `joined`) | 110 |
| Filter `true` (`unit_price >= 20`) | 68 |
| Formula | 68 |
| Summarize | 5 |
| Browse capture | 5 |

Expected aggregate values, independently calculated from the fixture generator's deterministic Customers and
Orders records:

| Region | GrossRevenue | OrderCount |
|---|---:|---:|
| *(Null)* | 887.96 | 5 |
| East | 2473.54 | 16 |
| North | 2608.95 | 18 |
| South | 1154.13 | 14 |
| West | 2672.36 | 15 |
| **Total** | **9796.94** | **68** |

In **Messages**, confirm both live database reads and the cross-database Join's local execution/fallback
diagnostic. Filter, Formula, and Summarize should execute locally after the cross-database Join; this workflow
should not claim those operations were pushed into either database. In **Performance**, capture node durations
and output row counts. The manifest for these runs should have zero pushed `database_queries` and should show
the database inputs, local Join/Filter/Formula/Summarize, and Browse row counts. If the actual diagnostics differ,
save the run and report the difference rather than assuming it is correct.

### Export both formats

Keep the Browse result selected. Click **Export CSV…**, save as `m21-baseline.csv`, then click **Export
Parquet…** and save as `m21-baseline.parquet` in the evidence folder. The native file dialogs should select the
destination. Both files must represent all five rows, not only the visible preview. Verify the CSV fields and
values against the table above. Check that the Parquet file opens and has the same three typed fields, five rows,
decimal scale 2, and a null value in `Region` for the first group. The results grid should show
`5 rows · 3 fields` (or the same counts with locale formatting).

## Prove refresh after a source change, then restore

With the successful baseline run captured, update only fixture order `1007` by running:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\tests\integration\desktop_acceptance.ps1 -Action Mutate
```

The helper uses a guarded update: it changes `unit_price` from exactly `21.29` to `22.29`, or reports that the
mutation is already applied. It refuses any unexpected value. Order 1007 has customer 5 (South), quantity 1,
and passes the filter, so the next application run must still have 110 joined rows, 68 filtered rows, and five
groups; `South.GrossRevenue` must increase by exactly 1.00 to **1155.13**, and total GrossRevenue becomes
**9797.94**. The app must re-read the source on rerun. Export the changed Browse result as
`m21-mutated.csv` and `m21-mutated.parquet`.

Capture the changed result and manifest before restoring the fixture. Then restore the one changed value:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\tests\integration\desktop_acceptance.ps1 -Action Restore
pwsh -NoProfile -ExecutionPolicy Bypass -File .\tests\integration\desktop_acceptance.ps1 -Action Status
```

Restore changes only order 1007 from exactly `22.29` back to `21.29`. Rerun once more if desired; the original
baseline aggregates must return. Leave the test containers available for review, or remove only these named
synthetic fixtures when finished:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\tests\integration\desktop_acceptance.ps1 -Action Down
```

`Down` requires typing `REMOVE FIXTURES` and deletes only the named test containers, ignored `.db_env.json`, and
the generated PostgreSQL test certificate/private key directory under `%TEMP%`.

## Evidence bundle and privacy

Create the evidence folder outside the repository. Capture these items:

1. Screenshot of both tested connections with host/port/database/user/TLS visible and all password fields blank.
   Redact account names embedded in the temporary CA file path if visible.
2. Screenshot of the saved canvas with all seven configured nodes and six labeled edges visible; separate
   inspector screenshots for the Join, Filter, Formula, and Summarize settings.
3. Baseline Results screenshot showing all five aggregate rows, schema, and successful status.
4. Messages and Performance screenshots showing the two database reads, cross-database local fallback, node
   row counts/timings, and successful run ID. Do not include connection passwords or secrets.
5. The matching baseline and mutated CSV/Parquet files, plus the saved `.flow.json` after checking it contains
   only connection IDs and no password, token, connection string, or CA private key.
6. A filtered run-manifest summary. After closing Flowbench, use the installed bundled Python executable
   (`runtime\python.exe` beside the installed `engine` directory) to run the standard-library-only exporter:

   ```powershell
   $InstallDir = 'C:\Path\To\Flowbench'
   $Evidence = Join-Path $env:USERPROFILE 'Desktop\FlowbenchAcceptanceEvidence'
   & (Join-Path $InstallDir 'runtime\python.exe') .\tests\integration\export_acceptance_manifests.py `
     --history (Join-Path $env:APPDATA 'org.parityresearch.flowbench\history.sqlite') `
     --workflow-name 'M21 Customer Order Manual Acceptance' `
     --limit 10 `
     --output (Join-Path $Evidence 'acceptance-manifests.json')
   ```

   The exporter includes run IDs, workflow digests, node tools, output rows/field types/digests, timing, query
   execution summaries and event counts. It deliberately omits saved workflow contents, SQL text, parameter
   values, connection settings, and event/error messages. Attach this filtered JSON, **not** the complete
   `history.sqlite` or `.db_env.json`.

Before sharing, inspect screenshots and files for credentials, home-directory names, unrelated workflow data,
or other private material. Never share `.db_env.json`, admin/writer credentials, OS-vault contents, or the
PostgreSQL private key. Send the evidence bundle to the repository owner for review. Human verification should
be recorded in `docs/MILESTONE_2_1_REPORT.md` only after the expected results and exports match.
