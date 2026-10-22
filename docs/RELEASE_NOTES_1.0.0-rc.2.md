# Flowbench 1.0.0-rc.2

This release candidate adds DeepSeek as a hosted AI provider and refreshes the product-facing documentation.

## Highlights

- **DeepSeek API provider:** connect with a DeepSeek API key, discover models from the provider, stream assistant responses and use Flowbench's function tools. API use is billed by DeepSeek separately from a ChatGPT plan. The key is held in native memory for the current app session and must be entered again after restart.
- **Hosted model context handling:** hosted OpenAI-compatible providers no longer inherit Ollama's configured context budget or its local-model warning. Ollama retains its configured request budget.
- **Product-focused README:** updated installation guidance, screenshots, user-facing capabilities, example workflows and compatibility status.

DeepSeek's documented Chat Completions tool-call mode is used with thinking disabled because Flowbench does not persist DeepSeek's private `reasoning_content` between stateless tool continuations. Models are discovered from the connected account; names and availability can change.

## Known limitations

- A real DeepSeek key and account are required to verify live inference; this release does not include credentials or subsidise API use.
- Full Alteryx behavioural parity is not claimed. Current catalogue implementation and reference evidence are tracked in [coverage](COVERAGE.md); the known Sort collation difference remains recorded.
- Installers are unsigned.
