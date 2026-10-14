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
runner differential result. Native Designer app-runner comparison and desktop
palette/configuration for an Action node remain incomplete. See the generated
catalogue contract and roadmap for the exact supported/unsupported variants.
