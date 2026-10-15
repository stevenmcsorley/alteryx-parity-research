# Flowbench AI — ChatGPT account acceptance

This procedure verifies the live OpenAI integration in a Windows Tauri build. Automated tests use deterministic provider mocks and do not prove OAuth eligibility or live inference.

## Before starting

- Use a current Flowbench Windows development build or installer containing the Flowbench AI checkpoint.
- Sign in with a ChatGPT account that is eligible to grant ChatGPT plan usage to this locally hosted open-source client.
- Ensure the machine can open the system browser and reach `auth.openai.com` and `api.openai.com`.
- Do not capture or share browser URLs containing callback parameters, credential files, tokens, or screenshots showing private workflow content.

Flowbench uses the public Sign in with ChatGPT OAuth flow. First registration starts with `dynamic_agent_client`; the issued client ID is saved with the account and reused. It requests `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`, verifies the ID token signature and claims, and refuses inference unless the token response granted `chatgpt.tokens.use.direct`. Tokens are protected using Windows DPAPI for the current user. The public inference route is `POST https://api.openai.com/v1/responses`; each request sets `store: false` and `stream: true`.

## Acceptance steps

1. Launch Flowbench and open **Flowbench AI** in the right dock. Confirm the Inspector tab remains available and the conversation dock can be resized.
2. Open the **•••** menu and select **Continue with ChatGPT**. Complete the OpenAI flow in the system browser and grant the requested plan usage permission. Return to Flowbench after loopback callback confirmation.
3. Confirm a connected indicator and an account-specific model selector appear. Select one of the visible models returned for this account.
4. In **••• → Allow prompt and bounded tool results to be sent to ChatGPT**, enable routing and accept the native Windows confirmation. The composer should show whether external routing is allowed.
5. Send `hello` ten times, waiting for each answer before sending the next. Confirm every completed response appears once, no assistant bubble is blank, and the composer remains enabled.
6. In the same conversation ask three unrelated questions: `What tools are implemented in Flowbench?`, `What does the open workflow do?`, and `Why is this workflow invalid?` Confirm distinct answers and preserved history.
7. Ask `Which implemented Flowbench tools are available?` Confirm compact tool activity appears when `app.get_capabilities` is called and the final response reflects installed catalogue statuses.
8. Ask `Why is this workflow invalid?` Confirm the assistant uses `workflow.validate` or `workflow.get_diagnostics` when appropriate and reports sanitized validation codes, not raw diagnostics or configuration values.
9. Ask `What does this workflow contain?` Confirm `workflow.get_snapshot` is used and the answer reflects actual graph nodes/edges. The outbound projection must exclude workflow names/descriptions, raw configuration, paths, queries, schemas, records and diagnostic prose.
10. Switch to **Inspector** while the conversation has history, then return to **Flowbench AI**. Confirm the transcript is preserved. Open **••• → Inspect Flowbench** to test local inspection and catalogue search.
11. Cancel a response mid-stream with **Stop**. Confirm it is marked stopped, the composer unlocks, then send a new prompt successfully. Also retry after a temporary network failure if one occurs.
12. Use **New conversation** and confirm only the transcript is cleared; the account remains connected. Test disconnect/reconnect from the menu. After disconnect, confirm the protected local session is removed; if OpenAI revocation is unreachable, Flowbench must explain this while reporting local disconnection.
13. Restart Flowbench and confirm the protected account session resumes. Conversation text is intentionally not persisted across application restarts.

## Evidence to record

Record the Flowbench build/version, Windows version, date, whether login and scope consent succeeded, the model display label (not tokens or callback URL), which steps passed, and the exact user-visible error for any failure. For screenshots, use a synthetic workflow with no customer data and redact account identifiers. Never attach the credential file from the Flowbench app-data directory or OAuth callback/browser network logs.

## Current implementation boundaries

- The currently exposed agent registry is read-only. It cannot edit a graph, execute a workflow, read raw rows, browse arbitrary files, or perform database writes.
- Flowbench sends the user's explicitly submitted prompt and bounded results returned by the existing safe projections only after both the checkbox and native Windows confirmation are accepted. Rust owns the fixed tool definitions and conversation transcript, and verifies tool calls and outputs against native allowlists. The renderer cannot submit system/developer messages or prebuilt tool-result transcript entries. Sign-out and app restart clear the in-memory transcript and consent; requests disable server-side response storage.
- Native code projects and bounds each read-only result before adding it to the transcript. Unknown fields and diagnostic prose are rejected; graph snapshots and capability lists are truncated with explicit indicators when large. The app does not automatically send the open workflow's configuration, schema, or dataset.
- ChatGPT conversation history remains in native process memory for the current application session; the UI keeps a correlated in-memory copy that survives Inspector/AI tab changes and dock resizing. No server-side response storage is enabled. New Conversation clears both copies; conversation text is cleared at sign-out/restart.
- A deterministic mock-provider UI test proves stream rendering and function routing. It is not evidence that live account registration, a given plan's eligibility, or real account inference succeeded. Record owner-run evidence separately.
- Sign-in currently keeps one active ChatGPT account. The UI refreshes the available model list from that account. Multi-account selection is not included in this checkpoint.
- Deltas are tagged with turn, message and response-cycle identifiers. The native command also returns the text from the completed Responses API output as the authoritative recovery path. This protects the renderer against missed/late event delivery but still requires the live acceptance above to verify the installed account/session and provider route.
- The next focused PR will add revision-bound graph proposals, isolated validation and preview, explicit approval, stale-revision checks, atomic application with a single undo checkpoint, and separately authorized workflow execution. This checkpoint adds no AI graph mutation or run tool.

## Official references

- [Sign in with ChatGPT: registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Sign in with ChatGPT: models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Sign in with ChatGPT: accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)

The implementation follows the public loopback/PKCE registration flow, requests the documented `chatgpt.tokens.use.direct` permission, checks the token response's granted scopes, filters `/v1/models` by `visibility == "list"`, and sends streaming `/v1/responses` requests with `store: false`. Recheck the linked OpenAI documentation when changing this integration because account eligibility and protocol details can evolve.
