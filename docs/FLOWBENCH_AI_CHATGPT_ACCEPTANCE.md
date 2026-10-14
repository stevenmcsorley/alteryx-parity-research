# Flowbench AI — ChatGPT account acceptance

This procedure verifies the live OpenAI integration in a Windows Tauri build. Automated tests use deterministic provider mocks and do not prove OAuth eligibility or live inference.

## Before starting

- Use a current Flowbench Windows development build or installer containing the Flowbench AI checkpoint.
- Sign in with a ChatGPT account that is eligible to grant ChatGPT plan usage to this locally hosted open-source client.
- Ensure the machine can open the system browser and reach `auth.openai.com` and `api.openai.com`.
- Do not capture or share browser URLs containing callback parameters, credential files, tokens, or screenshots showing private workflow content.

Flowbench uses the public Sign in with ChatGPT OAuth flow. First registration starts with `dynamic_agent_client`; the issued client ID is saved with the account and reused. It requests `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`, verifies the ID token signature and claims, and refuses inference unless the token response granted `chatgpt.tokens.use.direct`. Tokens are protected using Windows DPAPI for the current user. The public inference route is `POST https://api.openai.com/v1/responses`; each request sets `store: false` and `stream: true`.

## Acceptance steps

1. Launch Flowbench and open **Flowbench AI** in the right dock. Confirm the Inspector tab remains available.
2. Select **Continue with ChatGPT**. Complete the OpenAI flow in the system browser and grant the requested plan usage permission. Return to Flowbench after the loopback callback confirmation.
3. Confirm the workspace reports ChatGPT plan authorization and a model list appears. The choices must be the account's visible models, not a static built-in list.
4. Select an available model. Tick the consent checkbox describing prompt and bounded tool-result routing.
5. Ask: `Which implemented Flowbench tools are available?` Confirm the activity card reports `app.get_capabilities`, and the response reflects the installed catalogue's actual counts and statuses.
6. Ask: `Why is this workflow invalid?` Confirm the assistant requests `workflow.validate` or `workflow.get_diagnostics`, displays tool activity, and reports sanitized validation codes without raw diagnostic messages or configuration values.
7. Ask: `What does this workflow contain?` Confirm `workflow.get_snapshot` is used and the answer reflects actual graph nodes/edges. No workflow name, description, raw configuration, file path, query, schema, record data, or diagnostic prose should be included in the outbound safe projection.
8. While a response streams, select **Stop** and confirm generation ends without leaving the sidebar locked.
9. Select **Disconnect**. Confirm the account is disconnected and local protected session data is removed. If OpenAI revocation is unreachable, Flowbench must say remote revocation could not be confirmed while still reporting local disconnection.
10. Restart Flowbench and confirm the protected session resumes if not disconnected; after disconnect, confirm the app asks for sign-in again.

## Evidence to record

Record the Flowbench build/version, Windows version, date, whether login and scope consent succeeded, the model display label (not tokens or callback URL), which steps passed, and the exact user-visible error for any failure. For screenshots, use a synthetic workflow with no customer data and redact account identifiers. Never attach the credential file from the Flowbench app-data directory or OAuth callback/browser network logs.

## Current implementation boundaries

- The currently exposed agent registry is read-only. It cannot edit a graph, execute a workflow, read raw rows, browse arbitrary files, or perform database writes.
- Flowbench sends the user's explicitly submitted prompt and bounded results returned by the existing safe projections only after both the checkbox and native Windows confirmation are accepted. Rust owns the fixed tool definitions and conversation transcript, and verifies tool calls and outputs against native allowlists. The renderer cannot submit system/developer messages or prebuilt tool-result transcript entries. Sign-out and app restart clear the in-memory transcript and consent; requests disable server-side response storage.
- Native code projects and bounds each read-only result before adding it to the transcript. Unknown fields and diagnostic prose are rejected; graph snapshots and capability lists are truncated with explicit indicators when large. The app does not automatically send the open workflow's configuration, schema, or dataset.
- ChatGPT conversation history remains in native process memory for the current application session; the UI keeps its rendered copy in memory. No server-side response storage is enabled. The selected model slug and conversation are cleared at sign-out/restart.
- A deterministic mock-provider UI test proves stream rendering and function routing. It is not evidence that live account registration, a given plan's eligibility, or real account inference succeeded. Record owner-run evidence separately.
- Sign-in currently keeps one active ChatGPT account. The UI refreshes the available model list from that account. Multi-account selection is not included in this checkpoint.

## Official references

- [Sign in with ChatGPT: registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Sign in with ChatGPT: models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Sign in with ChatGPT: accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)

The implementation follows the public loopback/PKCE registration flow, requests the documented `chatgpt.tokens.use.direct` permission, checks the token response's granted scopes, filters `/v1/models` by `visibility == "list"`, and sends streaming `/v1/responses` requests with `store: false`. Recheck the linked OpenAI documentation when changing this integration because account eligibility and protocol details can evolve.
