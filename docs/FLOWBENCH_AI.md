# Flowbench AI

Flowbench AI is the assistant panel. It can inspect the open workflow, find tools in the catalogue, propose edits, run the workflow and read the results. Every edit is validated by the engine before it reaches the canvas.

## Choosing a model

Open the panel's settings (⚙) and pick a provider under **Model**.

**ChatGPT** uses your ChatGPT plan through Sign in with ChatGPT. Prompts, and any workflow data the AI asks to read, are sent to OpenAI once you allow data sharing. Reasoning effort is chosen beside the model; *Low* is the default and uses the least of your plan.

**Local model** uses a model server you run:

| Server | Default address | Notes |
|---|---|---|
| Ollama | `http://localhost:11434` | Flowbench uses Ollama's own chat API so it can set the context length (default 32k tokens). |
| LM Studio | `http://localhost:1234/v1` | Start the server in LM Studio's Developer tab. Set the context length when you load the model. |
| Other OpenAI-compatible | `http://localhost:8000/v1` | llama.cpp server, vLLM, Jan, LocalAI, or anything else with `/v1/chat/completions` and tool calling. An API key is optional; it is kept in memory until Flowbench closes. |

**Connect** checks the server by listing its models before anything is saved. Switching provider starts a new conversation.

When the server is on `localhost`, nothing leaves the computer and no data-sharing consent is asked for. A server elsewhere on the network needs the same consent as ChatGPT.

### Which local model?

The model must support **tool calling**. Good starting points are Qwen 2.5 or Qwen 3 (7B or larger), Llama 3.1 8B or newer, and Mistral. With Ollama, for example:

```
ollama pull qwen2.5:7b
```

Small models (3B and under) can call tools, but they plan multi-step workflows poorly. Flowbench's tool descriptions take about 6k tokens before the conversation starts, so a context length of 32k or more works best. Without a GPU, expect a minute or more per step.

## Approvals

Under **Permissions**:

- **Review mode** (the default): the AI asks before every workflow edit, run and save. Each edit is shown as a preview first.
- **Full Control**: the AI works without asking, until Flowbench closes. Every edit can still be undone. Running code and writing to databases are still confirmed separately.

## How the AI works: Flowbench does the computing

The AI is the orchestrator; Flowbench is the worker. For anything beyond an ordinary question, the AI:

1. finds tools with **find_tools** (search by task, such as "average by group" or "download csv") and reads their full specification with **describe_tool**;
2. locates data with **list_files** and **profile_file** (Flowbench reads the file and returns fields, suggested types, null counts and ranges), or fetches public data with **download_file**, which saves the file under `data/` with a `<file>.source.json` provenance record (URL, time, size, SHA-256);
3. builds the workflow on the canvas, validates it and runs it, exactly as you would;
4. inspects outputs with **query_results**, which computes group-bys and aggregates over the full run output inside Flowbench;
5. reports with **run_evidence**: workflow digest, tool versions, row counts, output digests and input file hashes.

If a run fails, the AI receives the engine's error code, message and failing node, so it can repair the workflow and run it again. When no native tool fits, it uses the Python or Run Command tools inside the workflow, so that step is recorded too.

These operations come from one registry: the same tool manifests drive the editor's configuration panels, the engine's validation and the AI's discovery. Nothing is reimplemented for the AI.

Results reach the AI in full. Image and chart cells are described by their size, because the raw bytes are of no use to a language model. Results older than the last six are shortened when the conversation is resent; the AI can call the tool again if it needs one.

### Permissions

Downloads and file writes need permission. In **Full Control** they are granted for the workspace. In **Review mode** the panel shows each request (for example, "Download https://… into data/") with **Allow** and **Decline**. The engine itself refuses these operations without the grant, so this does not depend on the model following instructions.

### Using Flowbench from other AI clients (MCP)

The same operations are available to any Model Context Protocol client over stdio:

```
python -m flowengine mcp --workspace path/to/project --allow network,file_writes
```

Besides the operations above, the server offers **validate_workflow** and **run_workflow** for `.flow.json` files in the workspace. Permissions are granted for the session with `--allow` (`network`, `file_writes`, `processes`, `database_writes`, `remote_writes`); anything not granted returns a structured `permission-required` error.

## Usage

Every step of a turn resends the conversation, so keeping data in Flowbench (rather than reading rows into the conversation) is what keeps usage low. The token count for the current turn shows beside the message box, and turns over 200k tokens are highlighted. **Usage** in settings shows the conversation's totals. With a local model, the count warns when the conversation nears the model's context length.

For long tasks:
- use a new conversation for each task;
- keep reasoning effort low unless the task needs more;
- consider a local model for exploratory work.

## Troubleshooting

**Show safe response diagnostics** lists recent responses in plain language, such as "Stopped: the plan's usage limit was reached". **Show technical details** gives the metadata-only record for bug reports; it never includes prompts, responses or data.

The protocol tests against a real local model are opt-in:

```
cd desktop/src-tauri
FLOWBENCH_TEST_OLLAMA_MODEL=qwen2.5:3b cargo test -- --ignored live_
```
