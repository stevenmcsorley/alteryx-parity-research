# Workflow, data and reporting guide

This guide covers the current desktop workflow for importing common data files, arranging and diagnosing a workflow, exporting results, and using Flowbench AI to author a workflow.

## Import data

1. Search **Input Data** in the left palette and add it to the canvas.
2. Select the node and set its **Path**. **Format: Auto** uses the file extension; choose a format explicitly if the extension is unusual.
3. For delimited files, choose the type inference mode and review the input schema. Flowbench inference scans the complete file, retains identifier-shaped values such as postcodes as text, and supports explicit field overrides. Alteryx compatibility mode reads delimited values as text.
4. For spreadsheet files, select the worksheet or provide a cell range and rows to skip when the data has title rows above its header.

| Format | Supported input | Type/schema behavior |
|---|---|---|
| CSV, TSV, custom-delimited text | `.csv`, `.tsv`, `.tab`, `.txt` | Header, encoding, delimiter, null and inference controls; explicit field overrides are available. |
| Excel | `.xls`, `.xlsx`, `.xlsm` | Reads worksheet cell values and preserves spreadsheet types where available. Legacy `.xls` uses the bundled `xlrd` reader. |
| OpenDocument spreadsheet | `.ods` | Reads worksheet values and types. |
| JSON / JSON Lines | `.json`, `.jsonl`, `.ndjson` | Reads arrays of records or one JSON object per line and forms a union of fields. |
| GeoJSON | `.geojson` | Reads a Feature or FeatureCollection; properties become columns, a top-level feature `id` is retained as `_feature_id`, and geometry is retained as GeoJSON text in the `geometry` field. |
| Parquet / Arrow IPC | `.parquet`, `.arrow`, `.feather`, `.ipc` | Uses the file's Arrow schema and typed values. |
| Alteryx data file | `.yxdb` | Requires the bundled optional reader; some original widths and decimal distinctions are unavailable from that reader. |

For GeoJSON, connect the Input Data output to a Spatial tool and select `geometry` as its geometry field. The coordinates are treated as GeoJSON coordinates (longitude/latitude under RFC 7946). Set a different CRS in a Spatial tool only when the source data actually uses that CRS.

## Arrange and troubleshoot a workflow

- Choose **Auto layout** in the toolbar or press **Ctrl+Shift+L**. Flowbench places connected tools in left-to-right dependency order and keeps sibling branches in separate rows. Comments, Explorer nodes, and container frames stay where they are. Use **Undo** to restore the prior positions.
- Select a node to inspect its configuration, anchors, and available schema. After validation or a run, use the **Fields** tab to see output field names, types, sizes, and scales.
- Open **Messages** and select **Show node** on a run error to focus the failing node. Click node-linked validation problems to inspect the configuration that needs attention.
- The Results grid displays field types with the values. CSV and Parquet exports include the complete output, independent of the number of preview rows shown in the grid.

## Export a result report

1. Run the workflow and select a Browse output or another retained result from the output picker.
2. Choose **Report HTML…** or **Report PDF…** in the Results toolbar and select a destination.
3. The report includes the workflow name, source node and anchor, total row and field counts, output schema, generation time, and a formatted table.

Reports include at most the first 500 rows and first 12 fields; cells longer than 500 characters are shortened, and the schema listing is capped at 200 fields. This keeps the document readable and bounded while the report states the total row and field counts. Export CSV or Parquet when the recipient needs the full dataset. PDF output uses a platform font when available; HTML keeps Unicode text and is suitable for printing from a browser.

For more control—charts, grouping, headers, footers, layouts, and multi-page composition—add the Reporting tools (Table, Interactive Chart, Text, Layout, Header/Footer, and Render) to the workflow. Those tools build report elements from actual workflow records and write HTML or PDF through the Render node. Choose an output path inside the authorised workspace and review the configured write permissions before running.

## Ask Flowbench AI to build a workflow

Open the **Flowbench AI** dock and select an available provider. With a supported model connected, ask for a concrete task, for example:

> Read `sales.csv`, convert `Sales` to FixedDecimal if needed, filter sales greater than 100, sort descending, summarize sales by category, and show the results.

Flowbench AI can inspect the installed tool catalogue and workflow context, propose or apply graph edits according to the current permission mode, validate through the engine, run the workflow, and inspect real Browse results. In Review mode, inspect and approve its proposed graph change. Full Control applies routine workspace changes directly when enabled for that workspace; it does not grant separate permissions for script execution, external writes, or sending private data to a hosted provider.

If the model provider is unavailable, no graph change or result should be treated as completed. Use the application Messages and AI activity/error details to see which step failed. Catalogue entries that are not executable remain unavailable to the assistant.

## Current boundaries

- Legacy Alteryx imports may retain original text-import settings; changing an existing workflow to Flowbench inference is an explicit configuration change.
- GeoJSON input currently targets Feature and FeatureCollection documents. It does not reproject coordinates during import; use Spatial tools with the correct CRS.
- Result reports are formatted views of an output sample, not a replacement for complete data exports or custom Reporting tool workflows.
- AI behavior depends on the selected provider/model, its available tool-calling capabilities, workspace permissions, and data-routing consent.
