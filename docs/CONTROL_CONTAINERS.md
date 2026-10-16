# Flowbench Control Containers

`core.control_container` (catalogue entry `developer:control-container`) is a native execution gate for a
group of nodes. Unlike the editor-only Tool Container, the engine schedules it. Its configuration lists
the contained node ids in `children` and an optional `caption`. It has an optional **Control** input and
a **Log** output.

Execution rules:

- Contained nodes wait until every node feeding Control has finished. The container is active when
  Control is unconnected or receives at least one record.
- When the container is inactive, its contained nodes, nodes downstream of them and nested containers
  are skipped. Each skip is reported with its reason.
- The container node finishes after all of its contents. Log has one row per contained node event
  (`completed`, `skipped`, `warning`, `info`): `Container` (node id), `NodeID`, `Tool`, `Event`,
  `Message` and `Records` (output rows for `completed`). An inactive container emits an empty Log, so
  connecting one container's Log to the next one's Control runs them in sequence and stops the chain
  when an earlier step does not run.
- A disabled container skips everything it contains. Containers may be nested, but a node belongs to at
  most one container. Unknown or duplicate children, containment cycles and a Control input fed from
  inside the container are validation errors.
- Contained nodes execute through the ordinary batch path. They are excluded from streaming segments
  and database pushdown so that the gate is honoured. A failure inside a container fails the run, as
  elsewhere in Flowbench.

In the desktop app, add Control Container from the Developer palette and use **Contained nodes** in
the Inspector. Deleting a node, in the editor or through an AI patch, removes it from any container's
children. The AI agent configures containers through the ordinary tool `add`/`configure` operations.

These are Flowbench semantics derived from the public functional description of the tool. Alteryx
Control Container XML import (`Caption`, `Disabled`, `Folded`, `Style`, `SuppressErrorsDesignTime`,
`SuppressErrorsRunTime`), error-suppression options and the vendor log layout are not implemented or
claimed. Tests: `tests/test_control_container.py`, `desktop/src/state/store.test.ts`,
`desktop/src/ai/workflowPatch.test.ts`.
