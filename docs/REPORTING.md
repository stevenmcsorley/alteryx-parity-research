# Flowbench reporting

Reporting tools exchange **report elements** in a `Report` text field (`flowbench.report.v1` JSON).
Elements carry computed content, not rendered output, so Render can draw the same chart as SVG in HTML
and as vector graphics in PDF (`flowengine/reports.py`, `flowengine/charts.py`).

| Tool | Kernel | Behaviour |
|---|---|---|
| Table | `core.report_table` | Selected columns with labels and formats (`auto`, `number`, `integer`, `percent`, `currency`, `text`, plus decimals). Numbers get thousands separators and are right-aligned. Optional `group_field` makes one table per group, and `total` columns add a summed total row. `max_rows` limits each table and warns when rows are cut. |
| Interactive Chart | `core.interactive_chart` | Bar, line, area, scatter, histogram or time-series from engine data. Aggregation by x is `sum`, `mean`, `count`, `min`, `max` or `none`, and `none` rejects repeated x values. `series_field` splits one y field. Also sorting, category limits, labels and size. Outputs the element and a rendered `Chart` preview. Values appear as hover tooltips in HTML and the results panel. |
| Report Text | `core.report_text` | Title, heading, body or caption text, left, centre or right aligned. `{{Field}}` placeholders are filled with plain field values (never evaluated) in `first_record` or `per_record` mode, and are escaped when rendered. |
| Image | `core.report_image` | Image from a file or from a blob or path field per record. It is decoded and validated with Pillow (PNG, JPEG, GIF, BMP, WebP, TIFF) with size and decompression-bomb limits. Non-JPEG images are normalised to PNG. Optional display width. |
| Layout | `core.report_layout` | Arranges elements from all connections (connection order, then record order) vertically or horizontally, with an optional heading. In horizontal rows, images keep their display width. Layouts nest. |
| Render | `core.render` | Writes every input element in order to `.html` or `.pdf`, with page size (A4, A3, Letter, Legal), orientation, margins and an optional page break per record. PDF tables split across pages with repeated headers. Output: path, format, page count and bytes. |

**HTML** is a single self-contained file: print CSS sets the page size, charts are inline SVG and
images are data URIs. All text is HTML-escaped.

**PDF** uses reportlab. Text uses a Unicode TrueType system font when available: Arial or Segoe UI on
Windows, Arial on macOS, DejaVu Sans or Liberation Sans on Linux, or an explicit `font_path`.
Otherwise it uses built-in Helvetica, and characters outside Latin-1 are replaced with a warning.
Layout errors (for example a single cell taller than a page) fail the node with a message.

Example: `examples/reporting/fuel_report.flow.json`. It runs Input Data → Summarize → Report Text,
Image, Table and Interactive Chart → Layout → Render, writing `out/fuel_report.pdf` and
`out/fuel_report.html` from UK weekly fuel prices. Tests: `tests/test_reporting.py`. The tests check
real HTML content, and PDF page counts, text operators, embedded images and vector chart bars.

Not implemented: the vendor report snippet format, Alteryx Reporting XML import (instances import as
unsupported nodes with their XML kept), headers and footers, map elements, and DOCX, XLSX or image
export formats.
