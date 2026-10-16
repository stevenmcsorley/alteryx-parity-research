# Flowbench Dynamic Input

`core.dynamic_input` (catalogue entry `developer:dynamic-input`) reads the file named by each incoming
record and stacks the results in record order. A typical workflow is Directory → Filter → Dynamic Input
with `path_field: FullPath`.

- `path_field` must be a text field. Relative paths resolve against the workflow directory. An empty or
  null path, or a missing file, fails the run and names the record number.
- All Input Data read options apply to every file: format detection, delimiter, header, encoding,
  `skip_rows`, type inference, declared `fields`, conversion-error handling, ragged rows,
  `empty_as_null` and `add_file_name_field`. `max_records` limits each file, not the total.
- With `schema_mode: identical` (the default), every file must have the first file's field names, order
  and types. `union` stacks by field name, widens types like Union and fills missing fields with null.
- Each record is read separately, so a path that appears twice is read twice.
- Without declared `fields`, the output schema depends on the files and is known only at run time. With
  `fields`, the schema is static, and an empty input produces an empty table. Without them, an empty
  input fails, because there is no schema to output.

These are Flowbench semantics derived from the public functional description of the "read a list of
data sources" mode. The vendor's SQL query modification mode (`Modifications`), its template-based
`ReadList_Type` path replacement variants, and Alteryx XML import are not implemented or claimed.
Tests: `tests/test_dynamic_input.py`.
