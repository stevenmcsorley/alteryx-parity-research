# Flowbench analytic application inputs

Flowbench's native workflow format can declare a per-run form under
`metadata.application`. The desktop presents that form before execution. Answers
are type-checked by the engine and applied to a private copy of the document;
the saved workflow is not modified.

Supported question types are `text`, `boolean`, `number`, and `choice`. Each
question has a unique `id`, optional `label`, `description`, `required`, and
`default`; `choice` also requires a non-empty `options` array. Mark a text
question `secret: true` to show a password field. Secret questions cannot have
workflow defaults, and submitted values are excluded from run history.

An action has `question`, `node_id`, and `config_path`. The path is an array of
object property names and/or decimal array indexes. It must resolve to a
configuration value that already exists. The engine updates only that value,
then performs the usual workflow configuration and schema validation. Unknown
inputs, invalid types, missing required values, unsafe paths, and invalid
targets stop the run with an explicit error.

Example:

```json
{
  "metadata": {
    "application": {
      "questions": [
        {"id": "limit", "label": "Minimum amount", "type": "number", "default": 10}
      ],
      "actions": [
        {"question": "limit", "node_id": "filter_1", "config_path": ["expression"]}
      ]
    }
  }
}
```

The native workflow-format runtime remains separate from imported Alteryx app
XML. The importer recognizes Alteryx question/action connections and extracts
one executable Action form: a single connected question drives an unconditional
`UpdateValue` in `Simple` mode targeting a mapped Filter `Expression`. The
question answer is applied to a private workflow copy before normal validation
and execution. Other Action modes, targets, conditions and multi-question
expressions produce import warnings and are not executed.

Imported Interface nodes, their wiring and `RuntimeProperties` are preserved on
export with ToolIDs remapped. Password-question defaults are redacted from the
imported metadata and export. Reference XML structure and the authorized
black-box CLI observation are recorded in `data/action-reference-evidence.json`.
The CLI did not apply RuntimeProperties actions, so this is not a Designer app
runner differential result.

The desktop palette also offers a **Flowbench Action** editor node. Its form
question is configured in the inspector, and its `Action` output connects to a
tool's separate `Action` target. That edge is saved under editor metadata and is
never sent to the row-processing DAG. Native question controls include text,
multiline text, number, boolean, choice, ISO date and hierarchical leaf-path answers; actions
select an existing target configuration value and run through the same
private-copy, type-check and validation path described above. This is native
Flowbench functionality, not a claim that every Alteryx
Interface connection or Action XML variant is interchangeable. Imported XML
retention and the limited imported UpdateValue adapter remain tracked
independently in the generated contract and roadmap.

Choice and List Box answers have distinct types: a single choice is a string,
while a List Box submits a list even when only one value is selected. Options
must be unique strings; required multi-select questions cannot submit an empty
list. Check Box answers remain booleans and Numeric Up Down values remain
finite numbers subject to configured bounds.
File Browse (`file`) and Folder Browse (`folder`) answers are path strings bound
through the same actions. A File Browse question may set `mode` to `open`
(default) or `save`, and `extensions` to unique lowercase names without dots,
such as `["csv", "tsv"]`. The desktop picker applies those filters and opens a
save dialog in save mode. At run time the engine resolves relative answers
against the workflow directory: an open-mode file must exist, a save-mode file
needs an existing parent folder and must not name a folder, and a folder answer
must be an existing directory. A non-matching extension, missing path or control
character stops the run with `application-input-invalid`. An empty optional
answer is bound unchanged. These are native rules; Alteryx File/Folder Browse
XML settings and Gallery upload behaviour are not mapped.

# Native Batch Macro Control Parameter

Flowbench-native analytic-app metadata supports a `control-parameter` question
inside a Batch Macro child definition. Its `control_field` names a column on the
parent macro's `control` input. Each ordered control row supplies that value to
the child workflow's connected configuration Action before the child executes.
The parameter is omitted from the standalone application form and cannot be
bound as an ordinary application answer. This adapter does not establish
Alteryx Control Parameter XML compatibility; imported vendor configurations
remain subject to their existing preservation and diagnostic behavior.

# Map, saved-connection and sourced questions

- **Map** (`type: "map"`): the answer is a GeoJSON geometry, or `longitude, latitude` typed in the run dialog, which becomes a Point. Flowbench checks it with shapely and binds it as compact GeoJSON text, ready for the spatial tools. Drawing on an interactive base map is not available because no map tile provider is bundled.
- **Saved connection** (`type: "connection"`, optional `kinds` from `database`, `web`, `github`, `smtp`): the run dialog lists the connections saved in Settings, and the answer is the connection's stable id. Secrets never pass through the application.
- **Option sources** for Drop Down, Radio Button, List Box and Tree: `source: {kind: "file", path, field}` reads the distinct values of a field from any file Input Data reads. A Tree uses `fields` (one per level) instead of `field`, or `{kind: "folder", path, depth, extensions}` to browse files. Sources are read each time the application is described or run, so options follow the data. An answer must be one of the current options.

# Formula and Input Data actions

An action's `kind` is one of:
- `value` (the default): the answer itself goes into the configuration path.
- `formula`: `expression` is evaluated over every answer, each one a field `[question id]`, and the result is written to the path.
- `input`: points a `core.input_data` tool's `path` at the chosen file. It is rejected for any other tool.

Binding never changes the saved document.

# Explorer Box

A documentation box on the canvas. It shows one of:
- a web page, in a sandboxed frame; sites that forbid embedding stay blank;
- a folder listing;
- a preview of a text file.

Relative paths resolve against the workflow folder. Alteryx HtmlBox nodes import with their `URL` and `HideAddressBar` settings. The box is never part of a run.
