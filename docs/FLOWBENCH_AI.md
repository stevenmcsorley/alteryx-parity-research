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
2. locates data with **list_files** and **profile_file** (Flowbench reads the file and returns fields, suggested types, null counts and ranges); for Excel and OpenDocument files, **inspect_spreadsheet** first lists every sheet with its first rows, so the AI can find where the table starts below the notes and multi-row headers that published statistics carry. It also fetches public data with **download_file**, which saves the file under `data/` with a `<file>.source.json` provenance record (URL, time, size, SHA-256);
3. builds the workflow on the canvas, validates it and runs it, exactly as you would;
4. inspects outputs with **query_results**, which computes group-bys and aggregates over the full run output inside Flowbench;
5. reports with **run_evidence**: workflow digest, tool versions, row counts, output digests and input file hashes.

Each tool specification from **describe_tool** includes tested examples: a configuration, its input and the output the kernel produces. They are harvested from the test suite by `scripts/build_tool_examples.py` into `data/tool-examples.json` (110 tools so far), and `tests/test_tool_examples.py` re-runs every one against its kernel. The specification also lists related tools from the same catalogue category.

Beyond workflow tools, the AI can work with the computer:

| Operation | Does | Permission |
|---|---|---|
| read_text_file | read notes, metadata, provenance files, SQL or code | — |
| write_text_file, make_folder | write reports and files inside the workspace (never outside it, never silently over an existing file) | file writes |
| extract_archive | unpack .zip/.tar(.gz) downloads inside the workspace (entries that would escape it are refused) | file writes |
| run_command | run an installed program (argv, no shell) and see its output and the files it wrote | running programs |
| run_python | run a Python script with Flowbench's Python (pandas, numpy, pyarrow, scipy, scikit-learn, statsmodels, openpyxl); the script is kept under `.flowbench/scripts/` | running programs |
| list_connections, list_tables, describe_table, query_database | discover saved database connections and query them read-only; credentials stay in the system vault | — |

More operations on the computer:

| Operation | Does | Permission |
|---|---|---|
| inspect_environment | the operating system, Flowbench's Python and libraries, managed Python environments, R, Docker and installed programs | — |
| install_python_packages | pip-install into a managed environment that also sees Flowbench's bundled libraries; run_python then takes env | running programs, internet |
| run_r | run an R script with the installed Rscript | running programs |
| start_process, process_status, stop_process | long-running programs in the background, with output so far, exit code and stop | running programs |
| docker | `docker <args>`, including `compose` | running programs |

### Finding data on the web

| Operation | Does |
|---|---|
| search_datasets | search official catalogues: GOV.UK statistics and data sets, data.gov.uk, or any CKAN portal |
| web_search | general web search; see below |
| fetch_page | a page's title, readable text, links and the data files it offers. GOV.UK pages list their published files with direct download URLs |
| find_data_files | follow a site's links from a page and list every data file found |
| inspect_api | summarise an OpenAPI/Swagger document, or a GraphQL endpoint by introspection |
| fetch_api | GET a JSON API with page, offset, cursor or next-link pagination, saving all items to `.json` or `.csv` with provenance |

All need internet permission. Downloads follow redirects and record their final URL, time, size and SHA-256.

**Web search** uses your own [SearXNG](https://docs.searxng.org/) instance when one is set in **AI settings → Web search**. It falls back to DuckDuckGo when SearXNG returns nothing, or Brave Search with a saved connection and key. SearXNG must allow JSON output. A minimal setup:

```yaml
# docker-compose.yml
services:
  searxng:
    image: searxng/searxng:latest
    ports: ["127.0.0.1:8888:8080"]
    volumes: ["./settings.yml:/etc/searxng/settings.yml:ro"]
    restart: unless-stopped
```

```yaml
# settings.yml
use_default_settings: true
server:
  secret_key: "change-me"
  limiter: false
search:
  formats: [html, json]
```

Run `docker compose up -d`, then set the address to `http://localhost:8888`. The search results show which SearXNG engines did not respond. Engines such as Google and DuckDuckGo often block self-hosted instances; Bing and others usually work. The MCP server takes `--searxng URL`, and the `FLOWBENCH_SEARXNG_URL` environment variable works everywhere.

### Tool contracts

**describe_tool** returns a contract for every installed tool:
- the settings with their defaults and constraints, and the anchors;
- the error conditions the kernel raises, read from its code (185 tools);
- side effects and streaming;
- first-use downloads;
- recovery behaviour;
- limitations and verification status from the contract ledger;
- tested examples.

Formula, Summarize, Filter, Join, Union, Unique, Sort, Multi-Row Formula and Input Data also carry curated semantics: null handling, ordering, output types and names. Every curated rule is checked against the engine by `tests/test_tool_specs.py`. **formula_reference** gives the expression language: operators, keywords, conditionals, null rules, and the 63 functions that exist, with their argument counts.

Python and commands are fallbacks for steps no native tool covers, and they are recorded as such in the task, not disguised as workflow nodes.

If a run fails, the AI receives the engine's error code, message and failing node, so it can repair the workflow and run it again. When no native tool fits, it uses the Python or Run Command tools inside the workflow, so that step is recorded too.

These operations come from one registry: the same tool manifests drive the editor's configuration panels, the engine's validation and the AI's discovery. Nothing is reimplemented for the AI.

Results reach the AI in full. Image and chart cells are described by their size, because the raw bytes are of no use to a language model. Results older than the last six are shortened when the conversation is resent; the AI can call the tool again if it needs one.

### Tasks and verification

Every request is a task, saved under the app's data folder in `ai-tasks/`. A task records:
- the objective;
- the phases visited (Discover, Plan, Acquire, Build, Validate, Execute, Inspect, Repair, Complete);
- every action and its outcome;
- the runs, downloaded files and workflow revisions;
- errors and the evidence gathered.

The **Task** tab shows the current task live, with earlier tasks below it. While the AI works, a strip above the message box shows the phase, with **Pause** (the AI waits between actions so you can edit the workflow) and **Take over** (stop the AI and turn Full Control off).

A request that asks for analysis, calculations, a workflow, a chart or data needs execution evidence before it can complete: a successful run, or a result query on one. If the model answers in text alone, Flowbench sends it back, at most twice, to build and run the workflow. An answer that is still not backed by a run is labelled **Not verified**. Ordinary questions are answered normally. The model can declare a request a plain question with `task_classify`, and its reason is shown beside the answer. These checks are made by Flowbench from tool observations, not by the model.

### Deliverables

For multi-part requests, the AI lists what was asked for with `task_plan`: the dataset and its source, the workflow, statistics, each chart, saved files. It links each one to its artifact with `task_deliver`. Flowbench checks every artifact itself:
- files exist and are not empty;
- charts are image, SVG, HTML or PDF files;
- workflows parse as Flowbench workflows with nodes;
- sources have recorded provenance;
- tables and statistics name a run that succeeded in the task.

Counts the request states, such as "two charts", are read from the request itself, so a plan cannot ask for less. A task whose deliverables are missing ends as **Incomplete**, and the Task tab lists what is missing.

### Permissions

Downloads and file writes need permission. In **Full Control** they are granted for the workspace. In **Review mode** the panel shows each request (for example, "Download https://… into data/") with **Allow** and **Decline**. The engine itself refuses these operations without the grant, so this does not depend on the model following instructions.

### Using Flowbench from other AI clients (MCP)

The same operations are available to any Model Context Protocol client over stdio:

```
python -m flowengine mcp --workspace path/to/project --allow network,file_writes
```

Besides the operations above, the server offers **workflow_format** (the `.flow.json` document format with an example), **validate_workflow** and **run_workflow** for workflow files in the workspace. Permissions are granted for the session with `--allow` (`network`, `file_writes`, `processes`, `database_writes`, `remote_writes`); anything not granted returns a structured `permission-required` error.

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

## Acceptance tests

`tests/test_acceptance.py` drives Flowbench through the MCP server the way an orchestrator does, and recomputes every number independently with pandas:

- **A:** download a CSV, build and run a workflow, query the summary, and trace it to the downloaded bytes with run_evidence.
- **B:** clean a sales CSV, total and average by category, sort, chart to HTML, write a CSV, and reopen and rerun the workflow with identical output digests.
- **D:** a wrong column name fails the run; the error is read, the workflow repaired and rerun, and the download is not repeated.
- **E:** download JSON, flatten it with run_python, and analyse the result in a workflow.

Scenarios C (manual and AI editing on one canvas, with undo and redo) and F (no false completion) need the editor and are in `desktop/src/ai/AiSidebar.test.tsx`.

To give the same tasks to a real model:

```
FLOWBENCH_ACCEPTANCE_MODEL=ollama:qwen2.5:7b python -m unittest discover -s tests -p test_acceptance.py -k live
```

