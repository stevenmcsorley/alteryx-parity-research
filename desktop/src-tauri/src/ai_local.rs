//! Local model providers: Ollama (native /api/chat, so the context length can be set) and any
//! OpenAI-compatible /v1/chat/completions server (LM Studio, llama.cpp, vLLM, Jan, LocalAI).
//! The conversation stays in Responses-API item form, as for ChatGPT, and is translated per
//! request, so tool validation, approvals and result projection are shared with ChatGPT.
use super::*;

pub(super) const SETTINGS_FILE: &str = "ai-provider.json";
const DEFAULT_CONTEXT_TOKENS: u32 = 32768;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub(super) struct LocalSettings {
    /// "ollama" or "openai" (OpenAI-compatible chat completions).
    pub kind: String,
    pub base_url: String,
    pub context_tokens: u32,
}

impl LocalSettings {
    pub fn new(kind: &str, base_url: &str, context_tokens: Option<u32>) -> Result<Self, String> {
        if !matches!(kind, "ollama" | "openai") {
            return Err("Choose Ollama or an OpenAI-compatible server".into());
        }
        let context_tokens = context_tokens.unwrap_or(DEFAULT_CONTEXT_TOKENS);
        if !(2048..=1_048_576).contains(&context_tokens) {
            return Err("Context length must be between 2,048 and 1,048,576 tokens".into());
        }
        Ok(Self { kind: kind.into(), base_url: normalize_base_url(kind, base_url)?, context_tokens })
    }
    pub fn label(&self) -> &'static str {
        if self.kind == "ollama" { "Ollama" } else { "the local model server" }
    }
    /// Whether requests stay on this computer.
    pub fn is_loopback(&self) -> bool {
        Url::parse(&self.base_url).ok().and_then(|u| u.host_str().map(str::to_owned)).is_some_and(|host| {
            host == "localhost" || host == "[::1]" || host == "::1" || host.starts_with("127.")
        })
    }
    pub fn public(&self) -> Value {
        json!({"kind":self.kind,"base_url":self.base_url,"context_tokens":self.context_tokens,"loopback":self.is_loopback()})
    }
}

pub(super) fn normalize_base_url(kind: &str, raw: &str) -> Result<String, String> {
    let url = Url::parse(raw.trim()).map_err(|_| "Enter the server address, for example http://localhost:11434".to_string())?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() || !url.username().is_empty() || url.password().is_some() {
        return Err("The server address must be an http:// or https:// URL without a user name or password".into());
    }
    let mut base = url.as_str().trim_end_matches('/').to_owned();
    if let Some(query) = base.find(['?', '#']) { base.truncate(query); }
    if kind == "ollama" {
        for suffix in ["/v1", "/api"] {
            if let Some(stripped) = base.strip_suffix(suffix) { base = stripped.to_owned(); }
        }
    } else if url.path() == "/" || url.path().is_empty() {
        base.push_str("/v1");
    }
    Ok(base)
}

pub(super) fn load_settings(data_dir: &std::path::Path) -> Option<LocalSettings> {
    let text = std::fs::read_to_string(data_dir.join(SETTINGS_FILE)).ok()?;
    let saved: LocalSettings = serde_json::from_str(&text).ok()?;
    LocalSettings::new(&saved.kind, &saved.base_url, Some(saved.context_tokens)).ok()
}

pub(super) fn save_settings(data_dir: &std::path::Path, settings: Option<&LocalSettings>) -> Result<(), String> {
    let path = data_dir.join(SETTINGS_FILE);
    match settings {
        None => match std::fs::remove_file(&path) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err("Could not save the AI provider setting".into()),
            _ => Ok(()),
        },
        Some(s) => {
            std::fs::create_dir_all(data_dir).map_err(|_| "Could not save the AI provider setting")?;
            std::fs::write(path, serde_json::to_vec_pretty(s).unwrap()).map_err(|_| "Could not save the AI provider setting".into())
        }
    }
}

fn request(client: &reqwest::Client, method: reqwest::Method, url: String, key: Option<&str>) -> reqwest::RequestBuilder {
    let builder = client.request(method, url);
    match key { Some(key) if !key.is_empty() => builder.bearer_auth(key), _ => builder }
}

fn unreachable(settings: &LocalSettings) -> String {
    format!("Could not reach {} at {}. Check that it is running and the address is right.", settings.label(), settings.base_url)
}

/// Server error text is shown to the user (local servers explain problems such as a model that
/// does not support tools); it is shortened and stripped of control characters.
async fn server_error(response: reqwest::Response) -> String {
    let status = response.status().as_u16();
    let body = response.text().await.unwrap_or_default();
    let detail = serde_json::from_str::<Value>(&body).ok()
        .and_then(|v| v.pointer("/error/message").or_else(|| v.get("error")).and_then(Value::as_str).map(str::to_owned))
        .unwrap_or(body);
    let detail: String = detail.chars().filter(|c| !c.is_control()).take(300).collect();
    if detail.trim().is_empty() { format!("The local model server returned HTTP {status}") }
    else { format!("The local model server returned HTTP {status}: {}", detail.trim()) }
}

pub(super) async fn list_models(settings: &LocalSettings, key: Option<&str>) -> Result<Vec<Value>, String> {
    let client = http_client();
    let url = if settings.kind == "ollama" { format!("{}/api/tags", settings.base_url) } else { format!("{}/models", settings.base_url) };
    let response = request(&client, reqwest::Method::GET, url, key).timeout(Duration::from_secs(15)).send().await
        .map_err(|_| unreachable(settings))?;
    if !response.status().is_success() { return Err(server_error(response).await); }
    let value: Value = response.json().await.map_err(|_| "The local model list was not valid JSON".to_string())?;
    Ok(model_choices(&settings.kind, &value))
}

pub(super) fn model_choices(kind: &str, value: &Value) -> Vec<Value> {
    let (list, key) = if kind == "ollama" { ("/models", "name") } else { ("/data", "id") };
    let mut models: Vec<Value> = value.pointer(list).and_then(Value::as_array).into_iter().flatten()
        .filter_map(|m| m.get(key).and_then(Value::as_str))
        .filter(|id| !id.is_empty() && id.len() <= 160 && !id.contains("embed"))
        .map(|id| json!({"slug":id,"display_name":id,"supported_efforts":[],"default_effort":null}))
        .collect();
    models.sort_by(|a, b| a["slug"].as_str().cmp(&b["slug"].as_str()));
    models
}

fn item_text(item: &Value) -> String {
    match item.get("content") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Array(parts)) => parts.iter().filter_map(|p| p.get("text").and_then(Value::as_str)).collect::<Vec<_>>().join(""),
        _ => String::new(),
    }
}

/// Translates Responses-API history items into chat messages for either wire format.
pub(super) fn chat_messages(kind: &str, history: &[Value]) -> Vec<Value> {
    let ollama = kind == "ollama";
    let mut names = HashMap::new();
    let mut out: Vec<Value> = Vec::new();
    for item in history {
        match item.get("type").and_then(Value::as_str) {
            Some("reasoning") => {}
            Some("function_call") => {
                let id = item["call_id"].as_str().unwrap_or_default();
                let name = item["name"].as_str().unwrap_or_default();
                let raw = item["arguments"].as_str().unwrap_or("{}");
                names.insert(id.to_owned(), name.to_owned());
                let call = if ollama {
                    json!({"function":{"name":name,"arguments":serde_json::from_str::<Value>(raw).unwrap_or(json!({}))}})
                } else {
                    json!({"id":id,"type":"function","function":{"name":name,"arguments":raw}})
                };
                let extend = out.last().is_some_and(|m| m["role"] == "assistant");
                if !extend { out.push(json!({"role":"assistant","content":if ollama { json!("") } else { Value::Null }})); }
                let last = out.last_mut().unwrap();
                match last.get_mut("tool_calls").and_then(Value::as_array_mut) {
                    Some(calls) => calls.push(call),
                    None => last["tool_calls"] = json!([call]),
                }
            }
            Some("function_call_output") => {
                let id = item["call_id"].as_str().unwrap_or_default();
                let content = item["output"].as_str().unwrap_or_default();
                out.push(if ollama {
                    json!({"role":"tool","content":content,"tool_name":names.get(id).cloned().unwrap_or_default()})
                } else {
                    json!({"role":"tool","tool_call_id":id,"content":content})
                });
            }
            _ => {
                let role = match item.get("role").and_then(Value::as_str) {
                    Some("developer" | "system") => "system",
                    Some("assistant") => "assistant",
                    Some("user") => "user",
                    _ => continue,
                };
                out.push(json!({"role":role,"content":item_text(item)}));
            }
        }
    }
    out
}

pub(super) fn chat_tools() -> Value {
    Value::Array(native_tool_definitions().as_array().into_iter().flatten().map(|tool| json!({
        "type":"function",
        "function":{"name":tool["name"],"description":tool["description"],"parameters":tool["parameters"]}
    })).collect())
}

pub(super) fn request_body(settings: &LocalSettings, model: &str, messages: Vec<Value>, tools_enabled: bool) -> Value {
    let mut body = json!({"model":model,"messages":messages,"stream":true});
    if settings.kind == "ollama" {
        body["options"] = json!({"num_ctx":settings.context_tokens});
    } else {
        body["stream_options"] = json!({"include_usage":true});
    }
    if tools_enabled { body["tools"] = chat_tools(); }
    body
}

/// Splits a byte stream into lines; handles both SSE (`data: {...}`) and NDJSON.
#[derive(Default)]
pub(super) struct Lines { buffer: Vec<u8> }
impl Lines {
    pub fn push(&mut self, chunk: &[u8]) -> Result<Vec<String>, String> {
        self.buffer.extend_from_slice(chunk);
        let mut lines = Vec::new();
        while let Some(end) = self.buffer.iter().position(|b| *b == b'\n') {
            let line: Vec<u8> = self.buffer.drain(..=end).collect();
            let text = String::from_utf8(line).map_err(|_| "The local model stream was not UTF-8".to_string())?;
            let text = text.trim();
            if !text.is_empty() { lines.push(text.to_owned()); }
        }
        if self.buffer.len() > 16 * 1024 * 1024 { return Err("The local model stream sent an oversized line".into()); }
        Ok(lines)
    }
    pub fn finish(&mut self) -> Option<String> {
        let rest = String::from_utf8(std::mem::take(&mut self.buffer)).ok()?;
        Some(rest.trim().to_owned()).filter(|s| !s.is_empty())
    }
}

#[derive(Default, Debug)]
pub(super) struct LocalOutput {
    pub text: String,
    pub calls: Vec<(String, String, String)>,
    pub finish: Option<String>,
    pub done: bool,
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
}

impl LocalOutput {
    /// Applies one stream line and returns any new assistant text.
    pub fn apply(&mut self, kind: &str, line: &str) -> Result<Option<String>, String> {
        let payload = if kind == "ollama" { line } else {
            let Some(data) = line.strip_prefix("data:") else { return Ok(None) };
            let data = data.trim();
            if data == "[DONE]" { self.done = true; return Ok(None); }
            data
        };
        let value: Value = serde_json::from_str(payload).map_err(|_| "The local model stream sent invalid JSON".to_string())?;
        if let Some(error) = value.get("error") {
            let text = error.get("message").and_then(Value::as_str).or(error.as_str()).unwrap_or("unknown error");
            return Err(format!("The local model reported an error: {}", text.chars().take(300).collect::<String>()));
        }
        if kind == "ollama" { Ok(self.apply_ollama(&value)) } else { Ok(self.apply_openai(&value)) }
    }

    fn apply_ollama(&mut self, value: &Value) -> Option<String> {
        let delta = value.pointer("/message/content").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned);
        for call in value.pointer("/message/tool_calls").and_then(Value::as_array).into_iter().flatten() {
            let name = call.pointer("/function/name").and_then(Value::as_str).unwrap_or_default().to_owned();
            let args = match call.pointer("/function/arguments") {
                Some(Value::String(s)) => s.clone(),
                Some(v) => v.to_string(),
                None => "{}".into(),
            };
            let id = call.get("id").and_then(Value::as_str).filter(|s| valid_token(s, 160)).map(str::to_owned)
                .unwrap_or_else(|| format!("call_{}", random_token().chars().take(24).collect::<String>()));
            self.calls.push((id, name, args));
        }
        if value.get("done").and_then(Value::as_bool) == Some(true) {
            self.done = true;
            self.finish = value.get("done_reason").and_then(Value::as_str).map(str::to_owned);
            self.input_tokens = value.get("prompt_eval_count").and_then(Value::as_u64);
            self.output_tokens = value.get("eval_count").and_then(Value::as_u64);
        }
        if let Some(d) = &delta { self.text.push_str(d); }
        delta
    }

    fn apply_openai(&mut self, value: &Value) -> Option<String> {
        if let Some(usage) = value.get("usage").filter(|u| u.is_object()) {
            self.input_tokens = usage.get("prompt_tokens").and_then(Value::as_u64);
            self.output_tokens = usage.get("completion_tokens").and_then(Value::as_u64);
        }
        let choice = value.pointer("/choices/0")?;
        if let Some(reason) = choice.get("finish_reason").and_then(Value::as_str) { self.finish = Some(reason.to_owned()); }
        let delta = choice.get("delta")?;
        for call in delta.get("tool_calls").and_then(Value::as_array).into_iter().flatten() {
            let index = call.get("index").and_then(Value::as_u64).map_or(self.calls.len().saturating_sub(1), |i| i as usize);
            if index > 64 { continue; }
            while self.calls.len() <= index { self.calls.push(Default::default()); }
            let slot = &mut self.calls[index];
            if let Some(id) = call.get("id").and_then(Value::as_str).filter(|s| !s.is_empty()) { slot.0 = id.to_owned(); }
            if let Some(name) = call.pointer("/function/name").and_then(Value::as_str) { slot.1.push_str(name); }
            if let Some(args) = call.pointer("/function/arguments").and_then(Value::as_str) { slot.2.push_str(args); }
        }
        let text = delta.get("content").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned);
        if let Some(t) = &text { self.text.push_str(t); }
        text
    }

    /// Output in Responses-API item form, ready for the shared tool-call checks.
    pub fn items(&self) -> Vec<Value> {
        let mut items = Vec::new();
        if !self.text.trim().is_empty() {
            items.push(json!({"type":"message","role":"assistant","content":[{"type":"output_text","text":self.text}]}));
        }
        for (id, name, args) in &self.calls {
            let id = if id.is_empty() { format!("call_{}", random_token().chars().take(24).collect::<String>()) } else { id.clone() };
            let args = if args.trim().is_empty() { "{}".to_owned() } else { args.clone() };
            items.push(json!({"type":"function_call","call_id":id,"name":name,"arguments":args,"status":"completed"}));
        }
        items
    }
}

pub(super) struct LocalStream {
    pub output: LocalOutput,
    pub cancelled: bool,
}

/// Sends one request and streams the reply, emitting text deltas as ChatGPT does.
pub(super) async fn stream(
    app: &tauri::AppHandle,
    state: &AiService,
    settings: &LocalSettings,
    key: Option<&str>,
    body: Value,
    delta_event: impl Fn(&str) -> Value,
) -> Result<LocalStream, String> {
    let url = if settings.kind == "ollama" { format!("{}/api/chat", settings.base_url) } else { format!("{}/chat/completions", settings.base_url) };
    let client = http_client();
    let response = send_cancellable(
        request(&client, reqwest::Method::POST, url, key).timeout(Duration::from_secs(1800)).json(&body),
        &state.cancel,
    ).await.map_err(|e| if e == "Generation cancelled" { e } else { unreachable(settings) })?;
    if !response.status().is_success() { return Err(server_error(response).await); }
    let mut bytes = response.bytes_stream();
    let mut lines = Lines::default();
    let mut output = LocalOutput::default();
    loop {
        let next = tokio::select! {
            item = bytes.next() => item,
            _ = tokio::time::sleep(Duration::from_millis(150)) => {
                if state.cancel.load(Ordering::SeqCst) { return Ok(LocalStream { output, cancelled: true }); }
                continue;
            }
        };
        if state.cancel.load(Ordering::SeqCst) { return Ok(LocalStream { output, cancelled: true }); }
        let Some(chunk) = next else { break };
        let chunk = chunk.map_err(|_| format!("The connection to {} was interrupted", settings.label()))?;
        for line in lines.push(&chunk)? {
            if let Some(delta) = output.apply(&settings.kind, &line)? {
                let _ = app.emit("ai-stream", delta_event(&delta));
            }
        }
        if output.done && settings.kind == "ollama" { break; }
    }
    if let Some(line) = lines.finish() {
        if let Some(delta) = output.apply(&settings.kind, &line)? { let _ = app.emit("ai-stream", delta_event(&delta)); }
    }
    if output.finish.as_deref() == Some("length") {
        return Err(format!("The local model ran out of room (context length {} tokens). Increase the context length in AI settings, or start a new conversation.", settings.context_tokens));
    }
    Ok(LocalStream { output, cancelled: false })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base_urls_are_normalised_per_server_kind() {
        assert_eq!(normalize_base_url("ollama", "http://localhost:11434/").unwrap(), "http://localhost:11434");
        assert_eq!(normalize_base_url("ollama", "http://localhost:11434/v1").unwrap(), "http://localhost:11434");
        assert_eq!(normalize_base_url("openai", "http://localhost:1234").unwrap(), "http://localhost:1234/v1");
        assert_eq!(normalize_base_url("openai", "http://box:8080/v1/").unwrap(), "http://box:8080/v1");
        assert!(normalize_base_url("openai", "file:///etc").is_err());
        assert!(normalize_base_url("openai", "http://user:pw@host").is_err());
        assert!(LocalSettings::new("ollama", "http://127.0.0.1:11434", None).unwrap().is_loopback());
        assert!(!LocalSettings::new("openai", "http://192.168.1.5:1234", None).unwrap().is_loopback());
        assert!(LocalSettings::new("other", "http://localhost", None).is_err());
    }

    #[test]
    fn history_translates_to_both_chat_formats() {
        let history = vec![
            json!({"role":"developer","content":[{"type":"input_text","text":"rules"}]}),
            json!({"role":"user","content":[{"type":"input_text","text":"hi"}]}),
            json!({"type":"reasoning","summary":[]}),
            json!({"type":"message","role":"assistant","content":[{"type":"output_text","text":"Looking"}]}),
            json!({"type":"function_call","call_id":"c1","name":"workflow_validate","arguments":"{}"}),
            json!({"type":"function_call","call_id":"c2","name":"run_get_results","arguments":"{\"a\":1}"}),
            json!({"type":"function_call_output","call_id":"c1","output":"ok"}),
            json!({"type":"function_call_output","call_id":"c2","output":"rows"}),
        ];
        let openai = chat_messages("openai", &history);
        assert_eq!(openai.len(), 5);
        assert_eq!(openai[0], json!({"role":"system","content":"rules"}));
        assert_eq!(openai[2]["content"], "Looking");
        assert_eq!(openai[2]["tool_calls"][1]["function"]["arguments"], "{\"a\":1}");
        assert_eq!(openai[4], json!({"role":"tool","tool_call_id":"c2","content":"rows"}));
        let ollama = chat_messages("ollama", &history);
        assert_eq!(ollama[2]["tool_calls"][1]["function"]["arguments"], json!({"a":1}));
        assert_eq!(ollama[4], json!({"role":"tool","content":"rows","tool_name":"run_get_results"}));
    }

    #[test]
    fn streams_accumulate_text_and_tool_calls() {
        let mut o = LocalOutput::default();
        for line in [
            r#"data: {"choices":[{"delta":{"content":"Hel"}}]}"#,
            r#"data: {"choices":[{"delta":{"content":"lo","tool_calls":[{"index":0,"id":"x1","function":{"name":"workflow_","arguments":"{\"q"}}]}}]}"#,
            r#"data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"validate","arguments":"\":1}"}}]},"finish_reason":"tool_calls"}]}"#,
            r#"data: {"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":9}}"#,
            "data: [DONE]",
        ] { o.apply("openai", line).unwrap(); }
        assert_eq!(o.text, "Hello");
        assert_eq!(o.calls, vec![("x1".into(), "workflow_validate".into(), "{\"q\":1}".into())]);
        assert_eq!((o.input_tokens, o.output_tokens, o.done), (Some(120), Some(9), true));
        assert_eq!(o.items()[1]["call_id"], "x1");

        let mut o = LocalOutput::default();
        o.apply("ollama", r#"{"message":{"role":"assistant","content":"","tool_calls":[{"function":{"name":"app_get_status","arguments":{}}}]},"done":false}"#).unwrap();
        o.apply("ollama", r#"{"message":{"role":"assistant","content":""},"done":true,"done_reason":"stop","prompt_eval_count":50,"eval_count":4}"#).unwrap();
        let items = o.items();
        assert_eq!(items.len(), 1);
        assert_eq!(items[0]["name"], "app_get_status");
        assert!(items[0]["call_id"].as_str().unwrap().starts_with("call_"));
        assert_eq!(o.input_tokens, Some(50));
        assert!(LocalOutput::default().apply("ollama", r#"{"error":"model does not support tools"}"#).unwrap_err().contains("does not support tools"));
    }

    #[test]
    fn line_splitter_handles_chunk_boundaries() {
        let mut lines = Lines::default();
        assert!(lines.push(b"{\"a\":").unwrap().is_empty());
        assert_eq!(lines.push(b"1}\n\n{\"b\"").unwrap(), vec!["{\"a\":1}"]);
        assert_eq!(lines.finish().as_deref(), Some("{\"b\""));
    }

    #[test]
    fn model_lists_skip_embedding_models() {
        let ollama = model_choices("ollama", &json!({"models":[{"name":"qwen2.5:3b"},{"name":"nomic-embed-text:v1.5"}]}));
        assert_eq!(ollama, vec![json!({"slug":"qwen2.5:3b","display_name":"qwen2.5:3b","supported_efforts":[],"default_effort":null})]);
        assert_eq!(model_choices("openai", &json!({"data":[{"id":"b"},{"id":"a"}]}))[0]["slug"], "a");
    }
}

/// Runs against a real local server: `FLOWBENCH_TEST_OLLAMA_MODEL=qwen2.5:3b cargo test -- --ignored live_`.
#[cfg(test)]
mod live {
    use super::*;

    async fn ask(kind: &str, base: &str) -> LocalOutput {
        let model = std::env::var("FLOWBENCH_TEST_OLLAMA_MODEL").unwrap_or_else(|_| "qwen2.5:3b".into());
        let settings = LocalSettings::new(kind, base, Some(8192)).unwrap();
        assert!(list_models(&settings, None).await.unwrap().iter().any(|m| m["slug"] == model.as_str()));
        let history = vec![json!({"role":"developer","content":[{"type":"input_text","text":"You are Flowbench AI. Use tools to answer."}]}),
            json!({"role":"user","content":[{"type":"input_text","text":"Validate the open workflow using the workflow_validate tool."}]})];
        let body = request_body(&settings, &model, chat_messages(kind, &history), true);
        let url = if kind == "ollama" { format!("{}/api/chat", settings.base_url) } else { format!("{}/chat/completions", settings.base_url) };
        let mut response = http_client().post(url).timeout(Duration::from_secs(900)).json(&body).send().await.unwrap();
        assert!(response.status().is_success());
        let (mut lines, mut out) = (Lines::default(), LocalOutput::default());
        while let Some(chunk) = response.chunk().await.unwrap() {
            for line in lines.push(&chunk).unwrap() { out.apply(kind, &line).unwrap(); }
        }
        out
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore]
    async fn live_tool_calls_parse_from_both_wire_formats() {
        for (kind, base) in [("ollama", "http://localhost:11434"), ("openai", "http://localhost:11434/v1")] {
            let out = ask(kind, base).await;
            let call = out.items().into_iter().find(|i| i["type"] == "function_call").unwrap_or_else(|| panic!("{kind}: no call in {out:?}"));
            assert_eq!(call["name"], "workflow_validate", "{kind}");
            assert!(tool_argument_rejection("workflow_validate", call["arguments"].as_str().unwrap()).is_none(), "{kind}");
            assert!(out.input_tokens.unwrap() > 1000, "{kind}: tool definitions should be in the prompt");
        }
    }
}
