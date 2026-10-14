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

This native workflow-format runtime is an initial Interface milestone. It does
not claim that imported Alteryx Check Box, Drop Down, Action, Condition, or
other Interface tool XML is fully mapped or reference-verified. Their actual
2026.2 serialization, dependent-control behaviour, and Action update semantics
remain in the catalogue roadmap pending authorised reference evidence.
