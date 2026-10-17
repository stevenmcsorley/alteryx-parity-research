//! Full tool results for Flowbench, bounded views for the model.
//!
//! Every tool result over STORE_OVER_BYTES is kept in full under ai-results/<task>/<call>.json. The model
//! receives the result itself only when it is at most MODEL_RESULT_MAX_BYTES; otherwise it receives a
//! structured view (shape, sizes, short previews) with a stable reference, and reads further parts on
//! demand with read_tool_result. Nothing is lost: the stored copy is the complete observation.
use super::*;

pub(super) const RESULTS_DIR: &str = "ai-results";
/// Results larger than this are stored on disk (so compaction can point to them later).
pub(super) const STORE_OVER_BYTES: usize = 2 * 1024;
/// Results larger than this reach the model as a bounded view instead of in full (about 6k tokens).
pub(super) const MODEL_RESULT_MAX_BYTES: usize = 24 * 1024;
const PREVIEW_CHARS: usize = 1500;
const READ_DEFAULT_CHARS: usize = 8000;
const READ_MAX_CHARS: usize = 16000;
const READ_DEFAULT_ITEMS: usize = 20;

/// Rough token estimate used for telemetry and the pre-request guard (about four bytes per token).
pub(super) fn estimate_tokens(bytes: usize) -> usize {
    bytes.div_ceil(4)
}

pub(super) fn result_ref(call_id: &str) -> String {
    let safe: String = call_id.chars().filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-').take(120).collect();
    format!("res_{safe}")
}

fn path_for(data_dir: &std::path::Path, task_id: &str, reference: &str) -> std::path::PathBuf {
    let task: String = task_id.chars().filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-').take(120).collect();
    data_dir.join(RESULTS_DIR).join(task).join(format!("{reference}.json"))
}

pub(super) fn store(data_dir: &std::path::Path, task_id: &str, call_id: &str, tool: &str, observation: &Value, bytes: usize) -> Option<String> {
    let reference = result_ref(call_id);
    let path = path_for(data_dir, task_id, &reference);
    std::fs::create_dir_all(path.parent()?).ok()?;
    let doc = json!({"ref":reference,"tool":tool,"call_id":call_id,"task_id":task_id,"stored_at":unix_now(),"bytes":bytes,"observation":observation});
    std::fs::write(&path, serde_json::to_vec(&doc).ok()?).ok()?;
    Some(reference)
}

fn load(data_dir: &std::path::Path, task_id: &str, reference: &str) -> Option<Value> {
    if !reference.starts_with("res_") || reference.len() > 130 { return None; }
    let direct = path_for(data_dir, task_id, reference);
    let path = if direct.is_file() { direct } else {
        // Results from earlier turns of the conversation live under their own task folders.
        std::fs::read_dir(data_dir.join(RESULTS_DIR)).ok()?.filter_map(Result::ok)
            .map(|d| d.path().join(format!("{reference}.json"))).find(|p| p.is_file())?
    };
    serde_json::from_slice(&std::fs::read(path).ok()?).ok()
}

/// The shape of a value with short previews, never larger than about `budget` characters.
pub(super) fn view(value: &Value, depth: usize, preview: usize) -> Value {
    match value {
        Value::String(s) => {
            let count = s.chars().count();
            if count <= preview { return json!(s); }
            json!({"text_preview": s.chars().take(preview).collect::<String>(), "chars": count, "truncated": true})
        }
        Value::Array(items) => {
            if depth >= 4 { return json!({"items": items.len()}); }
            let first: Vec<Value> = items.iter().take(3).map(|v| view(v, depth + 1, preview / 3)).collect();
            if items.len() <= 3 && serde_json::to_string(&first).map_or(0, |s| s.len()) <= preview { return Value::Array(first); }
            json!({"items": items.len(), "first": first, "truncated": items.len() > 3})
        }
        Value::Object(map) => {
            if depth >= 5 { return json!({"keys": map.keys().take(40).cloned().collect::<Vec<_>>(), "key_count": map.len()}); }
            let mut out = serde_json::Map::new();
            for (k, v) in map.iter().take(60) { out.insert(k.clone(), view(v, depth + 1, preview)); }
            if map.len() > 60 { out.insert("_more_keys".into(), json!(map.len() - 60)); }
            Value::Object(out)
        }
        other => other.clone(),
    }
}

/// What the model receives for a result too large to send in full.
pub(super) fn model_view(tool: &str, reference: &str, observation: &Value, bytes: usize) -> Value {
    let mut preview = PREVIEW_CHARS;
    let mut shape = view(observation, 0, preview);
    while serde_json::to_string(&shape).map_or(usize::MAX, |s| s.len()) > MODEL_RESULT_MAX_BYTES && preview > 100 {
        preview /= 3;
        shape = view(observation, 0, preview);
    }
    json!({"stored_result":{
        "ref": reference, "tool": tool, "bytes": bytes, "estimated_tokens": estimate_tokens(bytes),
        "note": "This result is too large to include in full, so Flowbench kept all of it and sends its shape with short previews. Read parts with read_tool_result (ref, a JSON pointer path such as /data/result/text, offset, limit). For data, prefer saving it to a file (download_file, or fetch_api with save_as) and processing it with workflow tools.",
        "view": shape}})
}

/// read_tool_result: a bounded part of a stored result.
pub(super) fn read(data_dir: &std::path::Path, task_id: &str, args: &Value) -> Value {
    let reference = args.get("ref").and_then(Value::as_str).unwrap_or("");
    let Some(doc) = load(data_dir, task_id, reference) else {
        return json!({"ok":false,"error":"Unknown result ref; use the ref given in a stored_result"});
    };
    let path = args.get("path").and_then(Value::as_str).unwrap_or("");
    let Some(target) = doc["observation"].pointer(path).or(if path.is_empty() { Some(&doc["observation"]) } else { None }) else {
        return json!({"ok":false,"error":format!("No value at {path}; use a JSON pointer from the result's view, such as /data/result")});
    };
    let offset = args.get("offset").and_then(Value::as_u64).unwrap_or(0) as usize;
    match target {
        Value::String(s) => {
            let limit = args.get("limit").and_then(Value::as_u64).map_or(READ_DEFAULT_CHARS, |l| l as usize).clamp(1, READ_MAX_CHARS);
            let total = s.chars().count();
            let text: String = s.chars().skip(offset).take(limit).collect();
            let end = (offset + limit).min(total);
            json!({"ok":true,"ref":reference,"path":path,"kind":"text","chars":total,"offset":offset,"returned":text.chars().count(),
                "next_offset": if end < total { json!(end) } else { Value::Null },"text":text})
        }
        Value::Array(items) => {
            let limit = args.get("limit").and_then(Value::as_u64).map_or(READ_DEFAULT_ITEMS, |l| l as usize).clamp(1, 200);
            let mut slice: Vec<Value> = items.iter().skip(offset).take(limit).cloned().collect();
            while serde_json::to_string(&slice).map_or(0, |s| s.len()) > MODEL_RESULT_MAX_BYTES && slice.len() > 1 {
                slice.truncate(slice.len() / 2);
            }
            let slice: Vec<Value> = slice.iter().map(|v| if serde_json::to_string(v).map_or(0, |s| s.len()) > MODEL_RESULT_MAX_BYTES { view(v, 1, PREVIEW_CHARS) } else { v.clone() }).collect();
            let end = offset + slice.len();
            json!({"ok":true,"ref":reference,"path":path,"kind":"array","items":items.len(),"offset":offset,"returned":slice.len(),
                "next_offset": if end < items.len() { json!(end) } else { Value::Null },"values":slice})
        }
        other => json!({"ok":true,"ref":reference,"path":path,"kind":"value","value":view(other, 0, PREVIEW_CHARS)}),
    }
}

pub(super) fn read_tool_definition() -> Value {
    json!({"type":"function","name":"read_tool_result","description":"Read part of a tool result that Flowbench stored because it was too large to send in full (see stored_result.ref). path is a JSON pointer into the stored result (\"\" for all of it, or for example /data/result/text or /data/result/rows); offset and limit page through text (characters) or lists (items).",
        "parameters":{"type":"object","properties":{"ref":{"type":"string"},"path":{"type":"string"},"offset":{"type":"integer"},"limit":{"type":"integer"}},
            "required":["ref","path","offset","limit"],"additionalProperties":false},"strict":true})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn views_are_bounded_whatever_the_size_of_the_result() {
        let csv: String = (0..40000).map(|i| format!("2026-07-{:02}T00:00:00Z,3{}.{}N,{},4.1,ml\n", i % 28 + 1, i % 9, i % 10, i)).collect();
        let huge = json!({"source":"x","data":{"ok":true,"operation":"fetch_page","result":{"text":csv,"links":vec![json!({"href":"u","text":"t"}); 5000]}}});
        let bytes = serde_json::to_string(&huge).unwrap().len();
        assert!(bytes > 1_000_000);
        let shown = model_view("agent.fetch_page", "res_c1", &huge, bytes);
        assert!(serde_json::to_string(&shown).unwrap().len() <= MODEL_RESULT_MAX_BYTES + 1500);
        assert_eq!(shown["stored_result"]["view"]["data"]["result"]["text"]["truncated"], true);
        assert_eq!(shown["stored_result"]["view"]["data"]["result"]["links"]["items"], 5000);
    }

    #[test]
    fn stored_results_are_complete_and_readable_in_parts() {
        let dir = std::env::temp_dir().join(format!("fb_results_{}", unix_now()));
        let text: String = (0..30000).map(|i| char::from(b'a' + (i % 26) as u8)).collect();
        let obs = json!({"data":{"result":{"text":text.clone(),"rows":(0..500).map(|i| json!([i, i * 2])).collect::<Vec<_>>()}}});
        let r = store(&dir, "turn_1", "call_9", "agent.fetch_page", &obs, 40000).unwrap();
        assert_eq!(r, "res_call_9");
        let first = read(&dir, "turn_1", &json!({"ref":r,"path":"/data/result/text","offset":0,"limit":10000}));
        assert_eq!((first["returned"].as_u64(), first["next_offset"].as_u64()), (Some(10000), Some(10000)));
        let last = read(&dir, "turn_1", &json!({"ref":r,"path":"/data/result/text","offset":29990,"limit":100}));
        assert_eq!(last["text"].as_str().unwrap(), &text[29990..]);
        assert!(last["next_offset"].is_null());
        let rows = read(&dir, "turn_1", &json!({"ref":r,"path":"/data/result/rows","offset":490,"limit":50}));
        assert_eq!((rows["items"].as_u64(), rows["returned"].as_u64()), (Some(500), Some(10)));
        assert_eq!(read(&dir, "another_turn", &json!({"ref":r,"path":"","offset":0,"limit":1}))["ok"], true, "found from a later turn");
        assert_eq!(read(&dir, "turn_1", &json!({"ref":"res_missing","path":"","offset":0,"limit":1}))["ok"], false);
        assert_eq!(read(&dir, "turn_1", &json!({"ref":r,"path":"/nope","offset":0,"limit":1}))["ok"], false);
        std::fs::remove_dir_all(dir).ok();
    }
}
