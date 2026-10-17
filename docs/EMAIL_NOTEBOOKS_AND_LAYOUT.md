# Email, API Output, Jupyter Flow, Map Input, Transpose In-DB and Visual Layout

## Email (`core.email`)

Sends one message per record over SMTP. From, To, Cc, Bcc, Subject and Body each take either a field or fixed
text. Fixed text can include `[Field]` placeholders; a placeholder that names no field stays as written. Lists of
addresses can be separated by commas or semicolons. The body can be plain text or HTML: an HTML body also gets a
plain-text alternative with the tags stripped. Attachments come from fixed paths or from a field, which can hold
several paths separated by semicolons. Each attachment can be at most 25 MB.

**Checked before anything is sent.** The tool builds every message first and rejects:

- invalid addresses
- line breaks in headers, which blocks header injection
- missing attachments or ones that are too large

If any record fails, no email is sent at all. When every record passes, the messages go out in one SMTP session.

**Permissions.** Messages carry record data to an outside server. The run therefore needs both *network requests*
and *remote writes* (`--allow-network --allow-remote-writes`); the desktop app asks for both. A disabled tool
(`enabled: false`) sends nothing and marks every record `disabled`.

**Servers and credentials.** A server without a login can be set in the tool: host, port and encryption (None,
SSL/TLS or STARTTLS, with default ports 25, 465 and 587). A server that needs a login uses a saved **Email
(SMTP)** connection (toolbar → Web & API):

- `smtps://host:465` uses TLS from the start.
- `smtp://host:587` uses STARTTLS. STARTTLS is required except for localhost.

The password is kept in the operating system vault and is only sent over an encrypted connection. Microsoft 365
OAuth and Purview sensitivity labels are not supported.

## API Output (`core.api_output`)

Writes the stream to the run messages as comma-delimited, quote-qualified text:

- the first message holds the field names, and each record follows as one message
- each message is cut to 255 characters
- embedded quotes are doubled
- blobs show their size, for example `"14 Bytes"`

## Jupyter Flow (`core.jupyter_flow`)

Runs a `.ipynb` notebook as part of the workflow, in an isolated Python process. This needs the *processes*
permission. Notebooks use the same markers as the vendor tool:

```python
#ayx_input            # connection #1 replaces this assignment's value (a pandas DataFrame)
sales = pd.read_csv("sample.csv")

#ayx_input=#2         # connection #2
prices = pd.DataFrame(...)

#ayx_output           # output 1 receives this variable's value at the end of the cell
result = sales.merge(prices)

#ayx_output=3         # output 3 (1–5); a Series becomes a one-column DataFrame
```

How the notebook runs:

- Code cells run in order, with the notebook's folder as the working directory.
- Markdown cells are skipped.
- IPython magics (`%…`, `!…`) are skipped with a warning.
- `packages_path` adds a site-packages folder or a `.pyz`/`.zip` of packages.

The vendor's zip-app building and data-cache options are not reproduced.

## Map Input (`core.map_input`)

Turns spatial objects into a workflow input, with one record per object holding `Label`, `Type` and `SpatialObj`.
It has two modes:

- **Draw:** enter points, lines and polygons as WKT or GeoJSON, each with a label.
- **Select:** connect reference records and pick objects by label, by a bounding box, or both.

Geometries are checked against the CRS; for EPSG:4326 that means longitude and latitude ranges. Drawing objects on
a map in the editor is not available yet.

## Transpose In-DB (`core.transpose_indb`)

Pivots data fields into rows inside the database. The SQL is a `UNION ALL` of one `SELECT` per data field. Each
output row holds the key fields, then `Name` (the field name, bound as a parameter) and `Value` (the field
converted to text by the database). Number and date text therefore follows the database's own formatting, as the
vendor documents. Blob fields are rejected. The tool is tested against real PostgreSQL 16 and SQL Server 2022.

## Visual Layout (`core.visual_layout`)

Places report elements on a page in rows:

- Each row lists elements by connection number and record number, for example `{"input": 2, "record": 1}`.
- One element fills its row; several elements share the row side by side. Rows stack from top to bottom.
- Elements that are left out are reported.
- With no rows configured, every element is stacked in connection order.

Connect the output to **Render**, which sets the page size, orientation and margins. The vendor's drag-and-drop
layout window is not reproduced.
