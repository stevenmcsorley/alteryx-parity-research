# Flowbench 1.0.0-rc.3

This release candidate focuses on making everyday workflows easier to arrange, inspect, report and import.

## Highlights

- **HTML and PDF result reports:** export the selected output from a completed run as a styled report containing its workflow/source details, schema and a bounded result sample. CSV and typed Parquet continue to export the complete result.
- **Auto layout:** arrange connected workflow tools left to right by dependency depth. Branches remain in separate rows; comments and documentation boxes keep their position. Undo restores the previous layout.
- **Clearer troubleshooting:** use node-linked execution messages to focus a failing tool, and inspect effective fields and types in the Fields panel and Results grid.
- **More input formats:** read legacy Excel `.xls` workbooks and GeoJSON Features/FeatureCollections. Spreadsheet cell values and GeoJSON property types are preserved where available; GeoJSON geometry is exposed for Spatial tools.
- **Flowbench AI workflow assistance:** ask the configured provider to inspect, build, validate, run and explain workflows using the actual installed tools and execution engine. Availability depends on the selected model, permission mode and data-routing settings.
- **Documentation and screenshot refresh:** the README now leads with the Flowbench product, and the workflow guide includes import, layout, debugging, report-export and AI examples.
- **Windows reliability:** barcode generation uses a short-lived local worker so a native imaging library failure cannot terminate the long-lived engine process.

## Report and import details

HTML/PDF reports show up to 500 rows and 12 fields; cells over 500 characters are shortened and the schema listing is capped at 200 fields. The report states the full record and field counts. Use CSV or Parquet when complete data is required.

The GeoJSON importer supports `Feature` and `FeatureCollection`. Properties become columns, a feature-level `id` becomes `_feature_id`, and geometry remains GeoJSON text in the `geometry` field. It does not reproject coordinates.

Legacy `.xls` reading uses the pinned `xlrd` 2.0.2 dependency in the managed runtime.

## Compatibility and limits

- Flowbench does not claim full Alteryx behavioral parity. The catalogue availability and compatibility evidence remain in [coverage](COVERAGE.md); the known Sort collation difference remains recorded.
- AI results depend on the connected provider/model and its tool-calling support. Remote providers receive only data allowed by the configured routing consent.
- Report exports are bounded views, not replacements for full CSV/Parquet exports or custom Reporting workflows.
- Windows and other installers remain unsigned.

