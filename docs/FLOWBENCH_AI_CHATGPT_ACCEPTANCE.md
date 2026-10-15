# Flowbench AI — ChatGPT account acceptance

**Live failure status:** The owner's reported empty-response failure is not considered
provider-diagnosed until safe diagnostics from an affected installed run are reviewed.
The native fix closes confirmed response-shape and stream-text recovery gaps; mock
tests cannot establish which response shape the owner's account/model returned.
Collect the metadata summary after any recurrence and attach it to the acceptance record.

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
5. In **•••**, temporarily clear **Include Flowbench read-only tools in requests**. Start a **New conversation**, send `hello`, and record the result in AI settings → **Show safe response diagnostics**. This makes a minimal, no-function-tool comparison using the same account-listed model. No inference parameter other than omitting `tools` is changed. Re-enable the checkbox, start another new conversation, and send the same `hello` as the tools-enabled comparison.
6. With tools enabled, send `hello` ten times, waiting for each answer before sending the next. Confirm every completed response appears once, no assistant bubble is blank, and the composer remains enabled.
7. In the same tools-enabled conversation ask three unrelated questions: `What tools are implemented in Flowbench?`, `What does the open workflow do?`, and `Why is this workflow invalid?` Confirm distinct answers and preserved history.
8. Ask `Which implemented Flowbench tools are available?` Confirm compact tool activity appears when `app.get_capabilities` is called and the final response reflects installed catalogue statuses. Confirm a tool-only response is followed by a second model request and then a final answer.
9. After that real function-call/output cycle, ask another question. The diagnostic input summary should report function-call and function-call-output history counts without showing their contents. Do not share private workflow information in screenshots.
10. If a turn fails or completes without user-facing text, open **••• → Show safe response diagnostics** before retrying. The summary records response/request IDs, HTTP status, model, turn/cycle, terminal status, safe error/incomplete codes, item/status/phase/content-type counts, text-delta count and aggregate usage. It excludes prompts, response text, reasoning text, function arguments/results and credentials. Share this summary with the owner to distinguish provider-empty output from local parsing/shape loss.
11. A reasoning-only or empty completion should show a specific inline error, leave the composer available, and permit retry. Refusal, `response.incomplete`, stream interruption and HTTP/usage failures should remain distinguishable in diagnostics. Never try to induce a refusal using sensitive content.
12. Switch to **Inspector** while the conversation has history, then return to **Flowbench AI**. Confirm the transcript is preserved. Open **••• → Inspect Flowbench** to test local inspection and catalogue search.
13. Cancel a response mid-stream with **Stop**. Confirm it is marked stopped, the composer unlocks, then send a new prompt successfully. Also retry after a temporary network failure if one occurs.
14. Use **New conversation** and confirm only the transcript is cleared; the account remains connected. Test disconnect/reconnect from the menu. After disconnect, confirm the protected local session is removed; if OpenAI revocation is unreachable, Flowbench must explain this while reporting local disconnection.
15. Restart Flowbench and confirm the protected account session resumes. Conversation text is intentionally not persisted across application restarts.

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
- When the completed response has no extractable user-facing text, actual text deltas are used as a bounded fallback only after `response.completed`. Refusals use the documented `refusal` content field; final-phase messages are preferred when output has multiple phases. No reasoning text is displayed.
- Up to 30 native, in-memory response diagnostics are available in AI settings. They contain structural counts and aggregate token usage only; there is no prompt/response/reasoning/tool payload logging. The read-only tool checkbox can be turned off for a fresh-conversation no-tools comparison; normal operation defaults to tools enabled. Disabling tools cannot add capabilities, and any unexpected function call in no-tools mode is rejected.
- The next focused PR will add revision-bound graph proposals, isolated validation and preview, explicit approval, stale-revision checks, atomic application with a single undo checkpoint, and separately authorized workflow execution. This checkpoint adds no AI graph mutation or run tool.

## Official references

- [Sign in with ChatGPT: registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Sign in with ChatGPT: models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Sign in with ChatGPT: accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)

The implementation follows the public loopback/PKCE registration flow, requests the documented `chatgpt.tokens.use.direct` permission, checks the token response's granted scopes, filters `/v1/models` by `visibility == "list"`, and sends streaming `/v1/responses` requests with `store: false`. Recheck the linked OpenAI documentation when changing this integration because account eligibility and protocol details can evolve.
