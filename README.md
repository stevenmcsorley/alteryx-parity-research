# Flowbench

**Build visual data workflows, run them locally, and work with an AI assistant inside the same desktop app.**

Flowbench is a cross-platform desktop application for importing data, connecting processing tools, running workflows, inspecting results and exporting them. Its canvas, configuration inspector, results grid and Flowbench AI workspace are integrated with one execution engine.

![Flowbench workflow canvas with the tool palette, configuration inspector, results grid and AI dock](docs/images/flowbench-example.png)

*A real Flowbench session showing a connected customer and orders workflow, its selected Join configuration, execution results and the AI dock.*

## What you can do

- **Create visual workflows:** search the categorized tool catalogue, add nodes to the canvas, connect typed anchors and configure tools in the inspector.
- **Work with real data:** read and write common file formats, connect to PostgreSQL and SQL Server, and inspect schemas and bounded previews.
- **Run and troubleshoot:** validate a workflow, execute or cancel it, inspect node results and diagnostics, review performance and reopen prior runs.
- **Inspect and export results:** view rows and field types in the results grid, then export complete outputs as CSV or typed Parquet.
- **Use Flowbench AI:** connect ChatGPT or a supported local model. The assistant can discover installed tools, inspect workflow context, propose or apply workflow changes according to the selected permission mode, execute workflows, inspect actual results, and help recover from errors.
- **Reuse existing work:** save native `.flow.json` workflows and import supported `.yxmd`, `.yxmc`, `.yxwz` and `.yxzp` documents. Import reports identify unsupported settings instead of silently treating them as equivalent.
- **Extend analysis:** use the available macro runtimes, reporting, spatial, statistical, predictive, machine-learning and connector tools where their dependencies and configurations are supported.

Flowbench includes a searchable catalogue of **323 entries across 29 categories**. Catalogue presence does not by itself mean a tool is executable: the palette indicates tool availability, and [the coverage report](docs/COVERAGE.md) distinguishes implementations from reference evidence. Flowbench is actively developed and does not claim complete Alteryx parity.

## Get Flowbench

Download the latest Windows, macOS or Linux package from [GitHub Releases](https://github.com/stevenmcsorley/flowbench/releases). The Windows installer includes its managed Python runtime; users do not need to install Python, Node.js or Rust separately. macOS and Linux builds include their own managed runtime as well.

Installers are currently unsigned. Windows SmartScreen may show **More info → Run anyway**. On macOS, follow the release notes if Gatekeeper blocks the app. R is an optional external runtime for workflows that use R tools.

## Try an example

Open one of these example workflows from the repository, or use it as a starting point for your own data:

| Example | What it demonstrates |
|---|---|
| [Monthly sales](examples/sales/monthly_sales.flow.json) | Read customer and order data, join, summarize and inspect sales results |
| [Customer reviews](examples/text_mining/customer_reviews.flow.json) | Text-processing workflow |
| [Fuel forecast](examples/predictive/fuel_models.flow.json) | Predictive model workflow |
| [Fuel investigation](examples/investigation/fuel_investigation.flow.json) | Data profiling and investigation |
| [UK spatial analysis](examples/spatial/uk_spatial.flow.json) | Spatial processing with local sample data |
| [Fuel report](examples/reporting/fuel_report.flow.json) | Reporting workflow |

Workflow paths are resolved relative to the project. Keep each example's companion `data/` directory beside its workflow file.

## Flowbench AI

Open **Flowbench AI** from the right-side dock. Choose ChatGPT, a local model server such as Ollama or LM Studio, or the hosted DeepSeek API. ChatGPT sign-in uses the official account flow; Flowbench does not ask for your ChatGPT password. Local models keep prompts on your machine when the server runs locally. DeepSeek requires its own API key and bills separately from a ChatGPT subscription; Flowbench asks before routing data to it.

The AI uses Flowbench’s installed tool descriptions and the same workflow editor and engine as manual work. Depending on the selected permission mode, it can preview edits for review or make routine workspace changes directly. Workflow runs that invoke code, and operations with external effects, remain subject to their own permissions. Data sent to a remote provider is governed by the AI data-sharing setting; inspect that setting before including private records.

See [Flowbench AI usage and permissions](docs/FLOWBENCH_AI.md) for provider setup, workspace behavior, supported operations and privacy details.

## Build from source

Development prerequisites: Python 3.12, Node.js 22, Rust stable and WebView2 on Windows. Platform-specific native libraries for Linux/macOS are listed in [the desktop build guide](docs/DESKTOP.md) and [cross-platform packaging notes](docs/CROSS_PLATFORM_UPDATE.md).

```powershell
python -m pip install -r requirements-dev.txt
python -m pip install -r requirements.txt
python -m flowengine validate examples/sales/monthly_sales.flow.json
python -m flowengine run examples/sales/monthly_sales.flow.json

cd desktop
npm ci
npx tauri dev
```

For installer creation, follow the platform-specific staging steps in [docs/DESKTOP.md](docs/DESKTOP.md). To run the existing application checks, see [docs/FULL_ACCEPTANCE.md](docs/FULL_ACCEPTANCE.md); external services and native installer checks are reported separately when unavailable.

## Project and compatibility

Flowbench maintains a **323-entry catalogue** and separate implementation and compatibility evidence. The current ledger records **307 implemented, 6 in progress and 10 not started**; those counts are generated from repository evidence and can change as development continues. Reference comparisons currently have one known failing Sort collation case; strict full-tool verification remains incomplete. See [coverage](docs/COVERAGE.md), [compatibility profiles](docs/COMPATIBILITY_PROFILES.md) and [known engine differences](docs/ENGINE_SEMANTICS.md) for current details.

The repository contains Flowbench’s independently written application and engine. It does not include Alteryx software, vendor source code, protected artwork, customer data or credentials. Reference investigation follows the scope recorded in [docs/AUTHORISATION.md](docs/AUTHORISATION.md).

## Useful links

- [Desktop guide](docs/DESKTOP.md)
- [Flowbench AI](docs/FLOWBENCH_AI.md)
- [Coverage and tool availability](docs/COVERAGE.md)
- [Example workflows](examples/)
- [Releases](https://github.com/stevenmcsorley/flowbench/releases)
- [Issues and product backlog](https://github.com/stevenmcsorley/flowbench/issues)
