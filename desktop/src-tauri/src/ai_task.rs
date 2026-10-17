//! Agent task lifecycle. Every conversation turn is a task with an objective, observable phases,
//! the actions taken, the runs, artifacts and workflow revisions they produced, and a completion
//! verdict. The verdict is decided here from tool observations, not from what the model says:
//! a request that asks for analysis, a workflow, a chart or data cannot complete on text alone.
use super::*;

pub(super) const TASK_DIR: &str = "ai-tasks";
/// How many times the model is sent back to execute before an unbacked answer is accepted as unverified.
const MAX_EXECUTION_NUDGES: u8 = 2;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum Kind {
    /// An ordinary question; text is a valid answer.
    Question,
    /// Analysis, building, running, downloading or charting: needs execution evidence.
    Execution,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum Phase { Discover, Plan, Acquire, Build, Validate, Execute, Inspect, Repair, Complete }

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(super) struct Action {
    pub at: u64,
    pub tool: String,
    pub phase: Phase,
    pub ok: Option<bool>,
    pub summary: String,
    /// Size of the full result Flowbench kept, and of what the model received (telemetry).
    #[serde(default)]
    pub result_bytes: Option<usize>,
    #[serde(default)]
    pub model_bytes: Option<usize>,
    #[serde(default)]
    pub result_ref: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(super) struct Task {
    pub id: String,
    pub objective: String,
    pub kind: Kind,
    /// Why the model said this request needs no execution, when it said so.
    pub classified_as_question: Option<String>,
    pub phase: Phase,
    pub started_at: u64,
    pub finished_at: Option<u64>,
    pub actions: Vec<Action>,
    pub runs: Vec<Value>,
    pub artifacts: Vec<Value>,
    pub revisions: Vec<String>,
    pub errors: Vec<Value>,
    pub evidence: Vec<Value>,
    pub nudges: u8,
    /// running | completed | incomplete | unverified | failed | cancelled
    pub status: String,
    /// The workspace folder relative artifact paths resolve against.
    pub workspace: Option<std::path::PathBuf>,
    /// What the request asks for, as planned by the model (task_plan) and checked by Flowbench (task_deliver).
    pub deliverables: Vec<Deliverable>,
    /// Minimum counts the request itself states ("two charts"), so a plan cannot quietly ask for less.
    pub required: Vec<(String, usize)>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(super) struct Deliverable {
    pub id: String,
    pub kind: String,
    pub description: String,
    pub artifact: Option<Value>,
    /// pending | verified | failed
    pub status: String,
    pub problem: Option<String>,
}

pub(super) const DELIVERABLE_KINDS: &[&str] = &["dataset", "source", "workflow", "run", "table", "statistic", "chart", "file", "report"];

/// Counts the request states explicitly, such as "two charts" or "3 tables".
pub(super) fn stated_requirements(prompt: &str) -> Vec<(String, usize)> {
    let text = prompt.to_lowercase();
    let words: Vec<&str> = text.split(|c: char| !c.is_alphanumeric()).filter(|w| !w.is_empty()).collect();
    let number = |w: &str| match w {
        "a" | "an" | "one" => Some(1), "two" | "both" => Some(2), "three" => Some(3), "four" => Some(4), "five" => Some(5),
        _ => w.parse::<usize>().ok().filter(|n| (1..=20).contains(n)),
    };
    let kind = |w: &str| match w {
        "chart" | "charts" | "graph" | "graphs" | "plot" | "plots" | "visualisation" | "visualisations" | "visualization" | "visualizations" => Some("chart"),
        "table" | "tables" => Some("table"),
        "report" | "reports" => Some("report"),
        "workflow" | "workflows" => Some("workflow"),
        _ => None,
    };
    // A thing counts as a deliverable only when the request asks for it to be produced: a number before
    // it ("two charts") or a creating verb shortly before it. "Report the results" or "Training dataset:"
    // name inputs or verbs, not files to make.
    const MAKE: &[&str] = &["create", "make", "build", "generate", "produce", "plot", "draw", "save", "write", "export",
                            "render", "add", "prepare", "construct", "include"];
    let mut out: Vec<(String, usize)> = Vec::new();
    for (i, w) in words.iter().enumerate() {
        let Some(k) = kind(w) else { continue };
        // "report the results", "chart the totals", "plot them": the word is a verb here.
        if words.get(i + 1).is_some_and(|n| matches!(*n, "the" | "on" | "back" | "to" | "that" | "what" | "how" | "which" | "any"
            | "all" | "its" | "their" | "them" | "it" | "this" | "these" | "those" | "your" | "my")) { continue; }
        let stated = words[i.saturating_sub(2)..i].iter().rev().find_map(|p| number(p).filter(|n| *n > 1 || !matches!(*p, "a" | "an")));
        let made = words[i.saturating_sub(5)..i].iter().any(|p| MAKE.contains(p));
        if stated.is_none() && !made { continue; }
        let n = stated.unwrap_or(1);
        match out.iter_mut().find(|(kk, _)| kk == k) {
            Some(entry) => entry.1 = entry.1.max(n),
            None => out.push((k.to_owned(), n)),
        }
    }
    if words.iter().any(|w| matches!(*w, "download" | "downloads" | "fetch" | "acquire" | "retrieve")) && !out.iter().any(|(k, _)| k == "dataset") {
        out.push(("dataset".into(), 1));
    }
    out
}

const ACTION_WORDS: &[&str] = &[
    "build", "create", "make", "run", "execute", "calculate", "compute", "chart", "plot", "graph", "visualise",
    "visualize", "download", "fetch", "analyse", "analyze", "analysis", "investigate", "investigation", "forecast",
    "predict", "summarise", "summarize", "aggregate", "total", "average", "import", "load", "clean", "join",
    "merge", "export", "report", "cluster", "regression", "correlate", "correlation", "compare",
];
const QUESTION_OPENINGS: &[&str] = &[
    "how do i", "how can i", "how would i", "how should i", "how does", "what is a", "what is an", "what are",
    "what does", "explain", "why is", "why does", "can you explain", "which tool", "what tool",
];

/// Execution unless the prompt reads as a question about how something works. The model can
/// reclassify with task_classify, which is recorded and shown to the user.
pub(super) fn classify(prompt: &str) -> Kind {
    let text = prompt.trim().to_lowercase();
    if QUESTION_OPENINGS.iter().any(|q| text.starts_with(q)) { return Kind::Question; }
    let words: Vec<&str> = text.split(|c: char| !c.is_alphanumeric()).filter(|w| !w.is_empty()).collect();
    if words.iter().any(|w| ACTION_WORDS.contains(w)) { Kind::Execution } else { Kind::Question }
}

pub(super) fn phase_for(tool: &str) -> Phase {
    match tool {
        "agent.download_file" | "agent.web_search" | "agent.search_datasets" | "agent.fetch_page" | "agent.find_data_files"
        | "agent.fetch_api" | "agent.inspect_api" | "agent.extract_archive" => Phase::Acquire,
        "agent.run_python" | "agent.run_command" | "agent.run_r" | "agent.docker" | "agent.start_process" => Phase::Execute,
        "task.plan" => Phase::Plan,
        "task.deliver" => Phase::Inspect,
        "workflow.propose_patch" | "workflow.request_save" => Phase::Build,
        "workflow.validate" | "workflow.get_diagnostics" => Phase::Validate,
        "workflow.request_run" => Phase::Execute,
        "run.get_results" | "run.get_result_page" | "agent.query_results" | "agent.run_evidence" => Phase::Inspect,
        "task.classify" => Phase::Plan,
        _ => Phase::Discover,
    }
}

impl Task {
    pub fn new(id: &str, objective: &str) -> Self {
        Task {
            id: id.into(), objective: objective.chars().take(4000).collect(), kind: classify(objective),
            classified_as_question: None, phase: Phase::Discover, started_at: unix_now(), finished_at: None,
            actions: vec![], runs: vec![], artifacts: vec![], revisions: vec![], errors: vec![], evidence: vec![],
            nudges: 0, status: "running".into(), workspace: None, deliverables: vec![], required: stated_requirements(objective),
        }
    }

    fn resolve(&self, path: &str) -> std::path::PathBuf {
        let p = std::path::PathBuf::from(path);
        if p.is_absolute() { p } else { self.workspace.clone().unwrap_or_default().join(p) }
    }

    fn run_succeeded(&self, run_id: &str) -> bool {
        self.runs.iter().chain(self.evidence.iter()).any(|r| r.get("run_id").and_then(Value::as_str) == Some(run_id)
            && r.get("status").map_or(true, |s| s == "succeeded"))
    }

    /// task_plan: the model lists what the request asks for. Replaces any earlier plan.
    pub fn plan(&mut self, args: &Value) -> Value {
        let Some(items) = args.get("deliverables").and_then(Value::as_array).filter(|a| !a.is_empty() && a.len() <= 50) else {
            return json!({"ok":false,"error":"Supply deliverables: a list of {id, kind, description}"});
        };
        let mut plan = Vec::new();
        for item in items {
            let id = item.get("id").and_then(Value::as_str).filter(|s| valid_token(s, 64));
            let kind = item.get("kind").and_then(Value::as_str).filter(|k| DELIVERABLE_KINDS.contains(k));
            let (Some(id), Some(kind)) = (id, kind) else {
                return json!({"ok":false,"error":format!("Each deliverable needs an id and a kind from {DELIVERABLE_KINDS:?}")});
            };
            plan.push(Deliverable { id: id.into(), kind: kind.into(), artifact: None, status: "pending".into(), problem: None,
                description: item.get("description").and_then(Value::as_str).unwrap_or("").chars().take(300).collect() });
        }
        let short: Vec<String> = self.required.iter()
            .filter(|(k, n)| plan.iter().filter(|d| &d.kind == k || (k == "dataset" && d.kind == "source")).count() < *n)
            .map(|(k, n)| format!("{n} {k}(s)")).collect();
        self.deliverables = plan;
        if self.kind == Kind::Question { self.kind = Kind::Execution; }
        if short.is_empty() { json!({"ok":true,"deliverables":self.deliverables.len()}) }
        else { json!({"ok":true,"deliverables":self.deliverables.len(),"warning":format!("The request asks for {}; add them to the plan.", short.join(", "))}) }
    }

    /// task_deliver: links a deliverable to its artifact; Flowbench checks the artifact exists and fits.
    pub fn deliver(&mut self, args: &Value) -> Value {
        let id = args.get("id").and_then(Value::as_str).unwrap_or("");
        let artifact = args.get("artifact").cloned().unwrap_or(Value::Null);
        let Some(index) = self.deliverables.iter().position(|d| d.id == id) else {
            return json!({"ok":false,"error":"Unknown deliverable id; call task_plan first"});
        };
        let kind = self.deliverables[index].kind.clone();
        let problem = self.check_artifact(&kind, &artifact);
        let d = &mut self.deliverables[index];
        d.artifact = Some(artifact);
        d.status = if problem.is_none() { "verified".into() } else { "failed".into() };
        d.problem = problem.clone();
        match problem {
            None => json!({"ok":true,"id":id,"status":"verified"}),
            Some(p) => json!({"ok":false,"id":id,"status":"failed","error":p}),
        }
    }

    fn check_artifact(&self, kind: &str, artifact: &Value) -> Option<String> {
        let path = artifact.get("path").and_then(Value::as_str);
        let run_id = artifact.get("run_id").and_then(Value::as_str);
        if let Some(run) = run_id {
            if !self.run_succeeded(run) { return Some(format!("Run {run} did not succeed in this task")); }
        }
        if let Some(p) = path {
            let full = self.resolve(p);
            let Ok(meta) = std::fs::metadata(&full) else { return Some(format!("{} does not exist", full.display())) };
            if meta.len() == 0 { return Some(format!("{} is empty", full.display())); }
            let ext = full.extension().and_then(|e| e.to_str()).unwrap_or("").to_lowercase();
            match kind {
                "chart" if !matches!(ext.as_str(), "html" | "htm" | "svg" | "png" | "jpg" | "jpeg" | "pdf") =>
                    return Some(format!("A chart must be an image, SVG, HTML or PDF file, not .{ext}")),
                "workflow" => {
                    let doc: Option<Value> = std::fs::read(&full).ok().and_then(|b| serde_json::from_slice(&b).ok());
                    if !doc.as_ref().is_some_and(|d| d["format"] == "flowengine.workflow" && d["nodes"].as_array().is_some_and(|n| !n.is_empty())) {
                        return Some("The workflow file is not a Flowbench workflow with nodes".into());
                    }
                }
                "source" => {
                    let sidecar = std::path::PathBuf::from(format!("{}.source.json", full.display()));
                    if !sidecar.is_file() && !self.artifacts.iter().any(|a| a["path"] == p) {
                        return Some("No provenance recorded for this file (download it with download_file or fetch_api)".into());
                    }
                }
                _ => {}
            }
            return None;
        }
        match kind {
            "run" | "table" | "statistic" if run_id.is_some() => None,
            "statistic" | "table" if artifact.get("value").is_some() && run_id.is_none() =>
                Some("A statistic or table needs the run_id that produced it".into()),
            _ => Some("Supply the artifact: a path for files, charts, datasets and saved workflows, or run_id (and node_id) for runs, tables and statistics".into()),
        }
    }

    fn missing(&self) -> Vec<String> {
        let mut out: Vec<String> = self.deliverables.iter().filter(|d| d.status != "verified")
            .map(|d| format!("{} ({}){}", if d.description.is_empty() { &d.id } else { &d.description }, d.kind,
                d.problem.as_ref().map(|p| format!(": {p}")).unwrap_or_default())).collect();
        for (kind, n) in &self.required {
            // A dataset with recorded provenance (kind source) also satisfies a requested dataset.
            let counts = |d: &&Deliverable| &d.kind == kind || (kind == "dataset" && d.kind == "source");
            let have = self.deliverables.iter().filter(counts).filter(|d| d.status == "verified").count();
            if have < *n { out.push(format!("{} verified {kind}(s) requested, {have} delivered", n)); }
        }
        out
    }

    pub fn call_started(&mut self, tool: &str, args: &Value) {
        let phase = if self.errors.len() > self.repaired() && matches!(phase_for(tool), Phase::Build | Phase::Validate) {
            Phase::Repair
        } else { phase_for(tool) };
        self.phase = phase;
        if tool == "workflow.propose_patch" {
            if let Some(rev) = args.get("base_revision").and_then(Value::as_str) { self.revisions.push(rev.chars().take(40).collect()); }
        }
        self.actions.push(Action { at: unix_now(), tool: tool.into(), phase, ok: None, summary: String::new(),
            result_bytes: None, model_bytes: None, result_ref: None });
    }

    fn repaired(&self) -> usize { self.actions.iter().filter(|a| a.phase == Phase::Repair).count().min(self.errors.len()) }

    /// Records the outcome of the latest action for `tool` and collects runs, artifacts, errors and evidence.
    pub fn call_finished(&mut self, tool: &str, result: &Value) {
        let (ok, summary) = outcome(tool, result);
        if let Some(action) = self.actions.iter_mut().rev().find(|a| a.tool == tool && a.ok.is_none()) {
            action.ok = Some(ok);
            action.summary = summary.clone();
        }
        let data = result.get("data").unwrap_or(result);
        match tool {
            "workflow.request_run" | "run.get_results" => {
                let status = data.get("status").and_then(Value::as_str).unwrap_or("");
                let run = json!({"run_id":data.get("run_id"),"status":status,"outputs":data.get("outputs").and_then(Value::as_array).map_or(0, Vec::len)});
                if tool == "workflow.request_run" { self.runs.push(run.clone()); }
                if status == "succeeded" && data.get("outputs").and_then(Value::as_array).is_some_and(|o| !o.is_empty()) {
                    self.evidence.push(json!({"kind":"run","tool":tool,"run_id":data.get("run_id")}));
                }
                if status == "failed" || status == "invalid" { self.errors.push(json!({"tool":tool,"error":data.get("error")})); }
            }
            "run.get_result_page" if data.get("rows").is_some() => self.evidence.push(json!({"kind":"result_page","node_ref":data.get("node_ref")})),
            "agent.query_results" | "agent.run_evidence" if data["ok"] == true => {
                self.evidence.push(json!({"kind":tool.trim_start_matches("agent."),"run_id":data.pointer("/result/run_id")}));
            }
            "agent.download_file" if data["ok"] == true => {
                self.artifacts.push(json!({"kind":"download","path":data.pointer("/result/path"),"url":data.pointer("/result/url"),"sha256":data.pointer("/result/sha256")}));
            }
            "agent.fetch_api" if data["ok"] == true && data.pointer("/result/saved_as").is_some() => {
                self.artifacts.push(json!({"kind":"download","path":data.pointer("/result/saved_as"),"url":data.pointer("/result/url"),"sha256":data.pointer("/result/sha256")}));
            }
            "agent.write_text_file" if data["ok"] == true => {
                self.artifacts.push(json!({"kind":"file","path":data.pointer("/result/path"),"sha256":data.pointer("/result/sha256")}));
            }
            "agent.run_python" | "agent.run_command" | "agent.run_r" | "agent.docker" if data["ok"] == true => {
                let exit = data.pointer("/result/exit_code").and_then(Value::as_i64);
                self.artifacts.push(json!({"kind":"process","tool":tool,"script":data.pointer("/result/script"),"argv":data.pointer("/result/argv"),
                    "exit_code":exit,"files_written":data.pointer("/result/files_written")}));
                if exit != Some(0) { self.errors.push(json!({"tool":tool,"error":format!("exit code {}", exit.unwrap_or(-1))})); }
            }
            _ => {}
        }
        if !ok && !matches!(tool, "workflow.request_run" | "run.get_results") {
            self.errors.push(json!({"tool":tool,"error":summary}));
        }
    }

    pub fn has_evidence(&self) -> bool { !self.evidence.is_empty() }

    /// Records how large a result was and how much of it reached the model.
    pub fn record_sizes(&mut self, tool: &str, result_bytes: usize, model_bytes: usize, reference: Option<&str>) {
        if let Some(action) = self.actions.iter_mut().rev().find(|a| a.tool == tool && a.result_bytes.is_none()) {
            action.result_bytes = Some(result_bytes);
            action.model_bytes = Some(model_bytes);
            action.result_ref = reference.map(str::to_owned);
        }
    }

    /// Called when the model answers with text and no tool calls. Returns a message to send back to
    /// the model when the answer is not yet backed by execution, or None when the task may complete.
    pub fn completion_check(&mut self) -> Option<String> {
        if self.kind == Kind::Question { return None; }
        if self.nudges >= MAX_EXECUTION_NUDGES { return None; }
        if self.has_evidence() {
            let missing = self.missing();
            if missing.is_empty() && (!self.deliverables.is_empty() || self.required.is_empty()) { return None; }
            self.nudges += 1;
            if self.deliverables.is_empty() {
                let stated = self.required.iter().map(|(k, n)| format!("{n} {k}")).collect::<Vec<_>>().join(", ");
                return Some(format!(concat!(
                    "Completion check (Flowbench): list what the request asks for with task_plan (deliverables such as dataset, source, workflow, run, statistic, chart, file), ",
                    "then link each to its artifact with task_deliver (a path, or the run_id and node_id) so Flowbench can verify it.{} (attempt {} of {})"),
                    if stated.is_empty() { String::new() } else { format!(" The request states: {stated}.") }, self.nudges, MAX_EXECUTION_NUDGES));
            }
            return Some(format!("Completion check (Flowbench): these deliverables are not verified yet: {}. Produce them and call task_deliver for each, or explain in your answer why one cannot be produced. (attempt {} of {})",
                missing.join("; "), self.nudges, MAX_EXECUTION_NUDGES));
        }
        self.nudges += 1;
        Some(format!(concat!(
            "Completion check (Flowbench): this request needs Flowbench to do the work, and no workflow run or result query backs your answer yet. ",
            "Build or extend the workflow, run it with workflow_request_run, and inspect the outputs with query_results or run_get_result_page before reporting. ",
            "Do not answer from your own calculations. If the request genuinely needs no execution, call task_classify with kind \"question\" and a reason. ",
            "(attempt {} of {})"), self.nudges, MAX_EXECUTION_NUDGES))
    }

    pub fn finish(&mut self, status: &str) {
        self.status = if status == "completed" && self.kind == Kind::Execution && !self.has_evidence() {
            "unverified".into()
        } else if status == "completed" && self.kind == Kind::Execution && !self.missing().is_empty() {
            "incomplete".into()
        } else { status.into() };
        if self.status == "completed" { self.phase = Phase::Complete; }
        self.finished_at = Some(unix_now());
    }

    pub fn verification(&self) -> &'static str {
        match (self.kind, self.has_evidence(), self.classified_as_question.is_some()) {
            (Kind::Execution, true, _) if !self.missing().is_empty() => "incomplete",
            (_, true, _) => "verified",
            (Kind::Question, false, true) => "answered_without_execution",
            (Kind::Question, false, false) => "not_required",
            (Kind::Execution, false, _) => "unverified",
        }
    }

    pub fn public(&self) -> Value {
        json!({"id":self.id,"objective":self.objective,"kind":self.kind,"phase":self.phase,"status":self.status,
            "verification":self.verification(),"classified_as_question":self.classified_as_question,
            "actions":self.actions,"runs":self.runs,"artifacts":self.artifacts,"revisions":self.revisions,
            "errors":self.errors,"evidence":self.evidence,"started_at":self.started_at,"finished_at":self.finished_at,
            "deliverables":self.deliverables,"required":self.required.iter().map(|(k, n)| json!({"kind":k,"count":n})).collect::<Vec<_>>(),
            "missing":self.missing()})
    }
}

fn outcome(tool: &str, result: &Value) -> (bool, String) {
    let data = result.get("data").unwrap_or(result);
    if let Some(ok) = data.get("ok").and_then(Value::as_bool) {
        let summary = if ok { "ok".to_owned() } else {
            data.pointer("/error/message").and_then(Value::as_str).unwrap_or("failed").chars().take(300).collect()
        };
        return (ok, summary);
    }
    if data.get("code").and_then(Value::as_str).is_some_and(|c| c.ends_with("failed") || c.contains("violation")) {
        return (false, data.get("error").and_then(Value::as_str).unwrap_or("failed").chars().take(300).collect());
    }
    match data.get("status").and_then(Value::as_str) {
        Some(s @ ("succeeded" | "approved" | "saved" | "valid")) => (true, s.into()),
        Some(s @ ("failed" | "invalid" | "rejected" | "cancelled" | "stale" | "validation_failed" | "revalidation_failed" | "not_started" | "timed_out" | "save_failed")) => {
            let message = data.pointer("/error/message").and_then(Value::as_str).map(|m| format!("{s}: {m}")).unwrap_or_else(|| s.into());
            (false, message.chars().take(300).collect())
        }
        Some(s) => (true, s.into()),
        None => (true, if tool == "workflow.validate" { format!("{} problem(s)", data.get("count").and_then(Value::as_u64).unwrap_or(0)) } else { "ok".into() }),
    }
}

/// task_classify: the model may say a request needs no execution; the reason is recorded and shown.
pub(super) fn classify_tool_definition() -> Value {
    json!({"type":"function","name":"task_classify","description":"Declare that the current request is an ordinary question that needs no Flowbench execution (or that it does). The reason is shown to the user. Only use kind \"question\" when no data, workflow, calculation or chart is involved.",
        "parameters":{"type":"object","properties":{"kind":{"enum":["question","execution"]},"reason":{"type":"string"}},"required":["kind","reason"],"additionalProperties":false},"strict":true})
}

pub(super) fn plan_tool_definitions() -> Vec<Value> {
    vec![
        json!({"type":"function","name":"task_plan","description":"List the deliverables the request asks for (for example the dataset and its source, the workflow, the statistics, each chart, saved files). Flowbench verifies each one before the task can complete. Call early; calling again replaces the plan.",
            "parameters":{"type":"object","properties":{"deliverables":{"type":"array","items":{"type":"object","properties":{
                "id":{"type":"string"},"kind":{"enum":DELIVERABLE_KINDS},"description":{"type":"string"}},"required":["id","kind","description"],"additionalProperties":false}}},
                "required":["deliverables"],"additionalProperties":false},"strict":true}),
        json!({"type":"function","name":"task_deliver","description":"Link a planned deliverable to its artifact so Flowbench can verify it: a path (files, charts, datasets, sources, saved workflows), or the run_id (and node_id) of the run that produced a table or statistic.",
            "parameters":{"type":"object","properties":{"id":{"type":"string"},"artifact":{"type":"object","properties":{
                "path":{"type":["string","null"]},"run_id":{"type":["string","null"]},"node_id":{"type":["string","null"]},"value":{"type":["string","null"]}},
                "required":["path","run_id","node_id","value"],"additionalProperties":false}},"required":["id","artifact"],"additionalProperties":false},"strict":true}),
    ]
}

pub(super) fn apply_classification(task: &mut Task, args: &Value) -> Value {
    let kind = args.get("kind").and_then(Value::as_str);
    let reason: String = args.get("reason").and_then(Value::as_str).unwrap_or("").chars().take(500).collect();
    match kind {
        Some("question") if !reason.trim().is_empty() => {
            task.kind = Kind::Question;
            task.classified_as_question = Some(reason);
            json!({"ok":true,"kind":"question","note":"Recorded. Your answer will be shown as answered without Flowbench execution."})
        }
        Some("execution") => { task.kind = Kind::Execution; json!({"ok":true,"kind":"execution"}) }
        _ => json!({"ok":false,"error":"Supply kind (question or execution) and a non-empty reason"}),
    }
}

pub(super) fn save(data_dir: &std::path::Path, task: &Task) {
    let dir = data_dir.join(TASK_DIR);
    if std::fs::create_dir_all(&dir).is_ok() {
        let _ = std::fs::write(dir.join(format!("{}.json", task.id)), serde_json::to_vec_pretty(&task.public()).unwrap_or_default());
    }
}

pub(super) fn load_recent(data_dir: &std::path::Path, limit: usize) -> Vec<Value> {
    let Ok(entries) = std::fs::read_dir(data_dir.join(TASK_DIR)) else { return vec![] };
    let mut tasks: Vec<Value> = entries.filter_map(Result::ok)
        .filter_map(|e| serde_json::from_slice::<Value>(&std::fs::read(e.path()).ok()?).ok())
        .collect();
    tasks.sort_by_key(|t| std::cmp::Reverse(t["started_at"].as_u64().unwrap_or(0)));
    tasks.truncate(limit);
    tasks
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn requests_are_classified_by_what_they_ask_for() {
        assert_eq!(classify("Create a workflow that totals sales by region"), Kind::Execution);
        assert_eq!(classify("What is the average revenue by region?"), Kind::Execution);
        assert_eq!(classify("Download the fuel prices and chart them"), Kind::Execution);
        assert_eq!(classify("How do I join two tables?"), Kind::Question);
        assert_eq!(classify("Why is this workflow invalid?"), Kind::Question);
        assert_eq!(classify("hello"), Kind::Question);
    }

    #[test]
    fn text_alone_never_completes_an_execution_task() {
        let mut task = Task::new("turn_1", "Build a workflow that groups sales by category and charts the totals");
        assert!(task.completion_check().unwrap().contains("attempt 1 of 2"));
        assert!(task.completion_check().unwrap().contains("attempt 2 of 2"));
        assert!(task.completion_check().is_none(), "after two nudges the answer is accepted, but not as verified");
        task.finish("completed");
        assert_eq!((task.status.as_str(), task.verification()), ("unverified", "unverified"));
    }

    #[test]
    fn a_successful_run_is_evidence_and_failures_lead_to_repair() {
        let mut task = Task::new("turn_2", "Calculate total sales by category");
        task.call_started("workflow.request_run", &json!({}));
        task.call_finished("workflow.request_run", &json!({"data":{"status":"failed","outputs":[],"run_id":"r1","error":{"message":"Unknown field"}}}));
        assert_eq!(task.errors.len(), 1);
        task.call_started("workflow.propose_patch", &json!({"base_revision":"rev_9"}));
        assert_eq!(task.phase, Phase::Repair);
        task.call_finished("workflow.propose_patch", &json!({"data":{"status":"approved"}}));
        task.call_started("workflow.request_run", &json!({}));
        task.call_finished("workflow.request_run", &json!({"data":{"status":"succeeded","run_id":"r2","outputs":[{"node_ref":"node_3"}]}}));
        assert!(task.completion_check().is_none());
        task.finish("completed");
        assert_eq!((task.status.as_str(), task.verification(), task.phase), ("completed", "verified", Phase::Complete));
        assert_eq!(task.revisions, vec!["rev_9"]);
        assert_eq!(task.runs.len(), 2);
        assert_eq!(task.actions.iter().map(|a| a.ok).collect::<Vec<_>>(), vec![Some(false), Some(true), Some(true)]);
    }

    #[test]
    fn the_model_can_reclassify_with_a_recorded_reason() {
        let mut task = Task::new("turn_3", "Summarise what Fuzzy Match does");
        assert_eq!(task.kind, Kind::Execution);
        assert_eq!(apply_classification(&mut task, &json!({"kind":"question","reason":""}))["ok"], false);
        apply_classification(&mut task, &json!({"kind":"question","reason":"Describing a tool, no data involved"}));
        assert!(task.completion_check().is_none());
        task.finish("completed");
        assert_eq!(task.verification(), "answered_without_execution");
    }

    #[test]
    fn stated_counts_are_read_from_the_request() {
        let r = stated_requirements("Download the real dataset, build an editable workflow, calculate the difference and generate two charts");
        assert!(r.contains(&("chart".into(), 2)));
        assert!(r.contains(&("workflow".into(), 1)));
        assert!(r.contains(&("dataset".into(), 1)));
        assert_eq!(stated_requirements("Make 3 plots and a table"), vec![("chart".to_string(), 3), ("table".to_string(), 1)]);
    }

    #[test]
    fn inputs_and_verbs_are_not_mistaken_for_deliverables() {
        let churn = "Create a Flowbench workflow called Predict Customer Churn. Training dataset: https://x/subscriptions.csv \
            Use native Flowbench machine-learning tools to import the training customers. Show the predictions. \
            Save the executable workflow and report the native execution results.";
        assert_eq!(stated_requirements(churn), vec![("workflow".to_string(), 1)]);
        assert_eq!(stated_requirements("Explain this workflow and report on its tables"), Vec::<(String, usize)>::new());
        assert_eq!(stated_requirements("Write a short findings report"), vec![("report".to_string(), 1)]);
        assert_eq!(stated_requirements("Fetch the latest prices"), vec![("dataset".to_string(), 1)]);
    }

    #[test]
    fn deliverables_are_verified_against_real_artifacts() {
        let dir = std::env::temp_dir().join(format!("fb_task_{}", unix_now()));
        std::fs::create_dir_all(dir.join("out")).unwrap();
        std::fs::write(dir.join("out/premium.html"), "<svg></svg>").unwrap();
        std::fs::write(dir.join("out/notes.txt"), "x").unwrap();
        std::fs::write(dir.join("fuel.csv"), "a\n1\n").unwrap();
        std::fs::write(dir.join("fuel.csv.source.json"), "{}").unwrap();
        std::fs::write(dir.join("fuel.flow.json"), r#"{"format":"flowengine.workflow","formatVersion":1,"nodes":[{"id":"a"}],"edges":[]}"#).unwrap();
        let mut t = Task::new("turn_d", "Download fuel prices, build a workflow and make two charts");
        t.workspace = Some(dir.clone());
        t.call_started("workflow.request_run", &json!({}));
        t.call_finished("workflow.request_run", &json!({"data":{"status":"succeeded","run_id":"r1","outputs":[{"node_ref":"node_1"}]}}));
        assert!(t.completion_check().unwrap().contains("task_plan"), "a run alone does not complete a request with stated deliverables");
        let warn = t.plan(&json!({"deliverables":[{"id":"data","kind":"source","description":"fuel csv"},
            {"id":"flow","kind":"workflow","description":"the workflow"},{"id":"c1","kind":"chart","description":"prices"}]}));
        assert!(warn["warning"].as_str().unwrap().contains("2 chart(s)"));
        assert_eq!(t.deliver(&json!({"id":"data","artifact":{"path":"fuel.csv"}}))["status"], "verified");
        assert_eq!(t.deliver(&json!({"id":"flow","artifact":{"path":"fuel.flow.json"}}))["status"], "verified");
        assert!(t.deliver(&json!({"id":"c1","artifact":{"path":"out/notes.txt"}}))["error"].as_str().unwrap().contains("chart must be"));
        assert!(t.deliver(&json!({"id":"c1","artifact":{"path":"out/missing.png"}}))["error"].as_str().unwrap().contains("does not exist"));
        assert!(t.deliver(&json!({"id":"c1","artifact":{"run_id":"r9"}}))["error"].as_str().unwrap().contains("did not succeed"));
        assert_eq!(t.deliver(&json!({"id":"c1","artifact":{"path":"out/premium.html"}}))["status"], "verified");
        let missing = t.missing();
        assert_eq!(missing, vec!["2 verified chart(s) requested, 1 delivered"]);
        t.finish("completed");
        assert_eq!((t.status.as_str(), t.verification()), ("incomplete", "incomplete"));
        assert_eq!(t.public()["deliverables"][2]["status"], "verified");
        std::fs::remove_dir_all(dir).ok();
    }

    #[test]
    fn simple_requests_complete_on_run_evidence_without_a_plan() {
        let mut t = Task::new("turn_s", "Total sales by region");
        t.call_started("agent.query_results", &json!({}));
        t.call_finished("agent.query_results", &json!({"data":{"ok":true,"result":{"run_id":"r1"}}}));
        assert!(t.completion_check().is_none());
        t.finish("completed");
        assert_eq!(t.verification(), "verified");
    }

    #[test]
    fn downloads_and_queries_are_recorded() {
        let mut task = Task::new("turn_4", "Download the data and average it");
        task.call_started("agent.download_file", &json!({}));
        task.call_finished("agent.download_file", &json!({"data":{"ok":true,"result":{"path":"/w/data/a.csv","url":"https://x/a.csv","sha256":"ab"}}}));
        assert_eq!(task.artifacts[0]["path"], "/w/data/a.csv");
        assert!(!task.has_evidence(), "a download alone is not an analysis");
        task.call_started("agent.query_results", &json!({}));
        task.call_finished("agent.query_results", &json!({"data":{"ok":true,"result":{"run_id":"r9"}}}));
        assert!(task.has_evidence());
        assert_eq!(task.actions[0].phase, Phase::Acquire);
    }
}
