# Interrupted ChatGPT response recovery

An SSE connection can end without a terminal response event. This is an incomplete inference, even if text
or complete-looking function arguments arrived. Flowbench continues to require `response.completed` before
publishing tool calls or appending a response to the native conversation. Official guidance:
https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference .

Previously, the error handler aborted the entire user turn. That removed earlier completed response cycles
and their tool observations from native history while leaving their workflow changes on the canvas. Manual
Retry then repeated the original prompt without those observations.

For clean EOF without a terminal event or an interrupted network stream, the native command now returns a
typed retryable outcome with turn/message/cycle identity and no output or calls. The UI discards the failed
cycle's partial text, rejects late events, and retries only that response up to twice with short backoff.
Completed tool cycles are retained, so they are not replayed. Stop cancels recovery. Explicit provider failures,
usage-limit errors, incomplete responses, cancellations, and malformed streams do not use this retry path.

If all three attempts are interrupted, the app closes the turn without rolling back its native history.
The error remains visible and manual Retry submits a continuation instruction to inspect the current workflow
and finish remaining work, rather than repeating the original request. This cannot guarantee identical model
decisions after reconnecting; workflow edits still use validation and revision checks.

Validation: 49 frontend tests and 35 Rust tests passed. Regression coverage includes recovery after an actual
Full Control edit without a second edit, bounded retries, continuation after exhaustion, suppression of calls
in an interrupted outcome, ignored late deltas, and Stop during backoff. Tests simulate transport interruption;
they do not claim that an intermittent live provider/network failure has been reproduced.

## Tool argument correction

Completed responses containing an unknown function or invalid arguments now receive native-generated
`function_call_output` validation feedback. Invalid graph patches report their operation index and expected
fields; rejected payload values are not included in diagnostics. A rejected batch executes nothing, including
otherwise valid calls that may depend on a rejected call. Every call in that batch receives a matching output,
and the AI can correct the calls in the same conversation. Three consecutive rejected batches stop correction
with a visible error and keep completed history available for continuation.

The graph-edit schema's example is now plain JSON encoded once, rather than an over-escaped example. Native
patch validation checks missing operation fields and the same 128 KB size bound as the editor. The safe
response diagnostics include validation reasons without rejected argument values. Existing renderer validation,
approval, revision, and permission checks still apply to corrected calls.

Validation after this change: 51 frontend tests and 39 native Rust tests pass. This includes mixed rejected
batches, unknown functions, malformed JSON, required graph-operation fields, privacy of rejection feedback,
automatic correction, and bounded repeated rejection. The exact original live rejected call was not captured;
these are regression fixtures for the previously fatal rejection path. Function-output guidance:
https://developers.openai.com/api/docs/guides/function-calling .
