# Flowbench 1.0.0-rc.4

This release is built on the integrated 1.0.0-rc.3 application and preserves its workflow editor, AI providers, reporting, input formats, managed runtimes, and existing tools.

## Highlights

- **Flowbench AI resumes interrupted research consistently.** If the model spends repeated response cycles on Flowbench-internal orchestration without taking an application action, Flowbench pauses the turn while preserving the native conversation and completed tool results. Retry continues the same request and explicitly retains the selected question, sources, and acquired files instead of silently restarting research.
- **Clearer recovery state.** The pause message explains that progress was preserved and offers a same-task continuation.

## Included from 1.0.0-rc.3

- HTML/PDF result reports, workflow auto layout, clearer field/type previews, additional input formats, and the integrated Flowbench AI workspace.
- Existing ChatGPT and DeepSeek provider configuration, workflow editing, validation, execution, and results inspection from the integrated main branch.

## Limits

- Retry continuity depends on prior tool results remaining within the provider's usable conversation context. The assistant must inspect current application state and verify any work before reporting completion.
- This release does not claim that an analysis is novel or that an external discovery is independently verified.
- Provider availability, external data access, permissions, and remote data routing remain subject to user configuration.
