# JSON Schema

Independent Flowbench implementation: `core.json_schema` (`developer:json-schema`). Configure a title, optional description, output column and one or more properties with string, integer, number, boolean, date, datetime or enum types. Property configuration supports descriptions, defaults, required flags and applicable constraints. It emits a JSON Schema 2020-12 document; an optional incoming table receives the same schema string on each row.

The schema builder behavior is described by the [official JSON Schema tool documentation](https://help.alteryx.com/current/en/designer/tools/developer/json-schema-tool.html). The kernel is exercised through the Flowbench workflow engine in `tests/test_json_schema_tool.py` and the generated output is checked by the Python `jsonschema` implementation.

**This tool is not reference verified.** Evidence limits: Alteryx XML import/export translation and differential output compatibility remain unverified. Flowbench's `x-flowbench-min-date` / `x-flowbench-max-date` keywords preserve configured date bounds as annotations because JSON Schema Draft 2020-12 has no standard string-date minimum/maximum assertion keywords.
