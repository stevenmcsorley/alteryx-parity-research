# Flowbench In-Database tools

In-DB tools build a query step by step and run it inside PostgreSQL or SQL Server
(`flowengine/tools/indb.py`). An **In-DB stream** is a one-row table holding the connection id, the SQL
text with `%s` markers, the bound parameters, the result fields and the steps that built it. Each tool
wraps the upstream query in a new SELECT. Nothing executes until **Data Stream Out**, **Browse In-DB**,
**Write Data In-DB** or **Dynamic Input In-DB** (to read field names) contacts the server.

**Same semantics as the local engine.** Filter and Formula expressions are translated by the pushdown
translator:
- two-valued logic, so a comparison with null is false and the record goes to False;
- null-safe `=`, and case-insensitive text comparison;
- division by zero gives null;
- text grouping and join keys use a binary (code-point) collation, as locally.

An expression without an exact SQL equivalent is rejected with the reason, never approximated.
Join In-DB matches null keys to null keys and compares keys case-sensitively, like the local Join.
Values from data or configuration are always bound parameters, and identifiers are quoted by the dialect.

| Tool | Kernel | Behaviour |
|---|---|---|
| Connect In-DB | `core.connect_indb` | Table or view, or a read-only parameterised SELECT, from a saved connection |
| Data Stream Out | `core.data_stream_out` | Runs the query and returns its records (optionally the first `max_rows`) |
| Data Stream In | `core.data_stream_in` | Local records as a stream. `inline` uses a bound VALUES list without write access, within driver parameter limits (2,000 on SQL Server). `table` creates a table and needs write access |
| Browse In-DB | `core.browse_indb` | Fetches only the first `preview_rows` for the results panel, and passes the stream on |
| Select In-DB | `core.select_indb` | Keep, order and rename columns |
| Filter In-DB | `core.filter_indb` | True and False streams |
| Formula In-DB | `core.formula_indb` | Add or replace columns: numbers become doubles, text stays text, conditions become booleans, and field copies keep their type |
| Summarize In-DB | `core.summarize_indb` | group_by, count, count_non_null, count_distinct, sum, avg, min and max |
| Join In-DB | `core.join_indb` | Inner, left, right or full outer join of two streams on the same connection |
| Union In-DB | `core.union_indb` | UNION ALL by field name. Missing fields become typed nulls |
| Sample In-DB | `core.sample_indb` | First N by `order_by` (required), random N, or random percent (not repeatable) |
| Write Data In-DB | `core.write_indb` | Create, replace or append a table from the stream inside the database, in one transaction |
| Dynamic Input In-DB | `core.dynamic_input_indb` | One read-only query or table name per record, stacked. Every query is checked |
| Dynamic Output In-DB | `core.dynamic_output_indb` | The stream's SQL, parameters and steps as a record. Nothing executes |
| Macro Input / Output In-DB | `core.macro_input_indb`, `core.macro_output_indb` | Pass In-DB streams into and out of macros |

**Permissions.** Reads use read-only sessions and the read-only SQL guard. Write Data In-DB and Data
Stream In table mode need a connection with writes enabled **and** the per-run database-write grant
(CLI `--allow-database-writes`, or the desktop prompt). They are listed by the permission preflight.

Tests: `tests/test_indb.py` checks SQL composition, parameter order, permissions and limits against a
recording database. `tests/integration/test_indb_integration.py` runs every tool on real PostgreSQL 16
and SQL Server 2022 in the database CI job, and requires the same results as the local Filter,
Formula, Summarize and Join tools.

Not implemented: other database products, the vendor's In-DB XML import, temporary-table streaming,
and spatial In-DB functions.
