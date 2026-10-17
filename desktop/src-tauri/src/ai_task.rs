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
    /// running | completed | unverified | failed | cancelled
    pub status: String,
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
        "agent.download_file" => Phase::Acquire,
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
            nudges: 0, status: "running".into(),
        }
    }

    pub fn call_started(&mut self, tool: &str, args: &Value) {
        let phase = if self.errors.len() > self.repaired() && matches!(phase_for(tool), Phase::Build | Phase::Validate) {
            Phase::Repair
        } else { phase_for(tool) };
        self.phase = phase;
        if tool == "workflow.propose_patch" {
            if let Some(rev) = args.get("base_revision").and_then(Value::as_str) { self.revisions.push(rev.chars().take(40).collect()); }
        }
        self.actions.push(Action { at: unix_now(), tool: tool.into(), phase, ok: None, summary: String::new() });
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
            _ => {}
        }
        if !ok && !matches!(tool, "workflow.request_run" | "run.get_results") {
            self.errors.push(json!({"tool":tool,"error":summary}));
        }
    }

    pub fn has_evidence(&self) -> bool { !self.evidence.is_empty() }

    /// Called when the model answers with text and no tool calls. Returns a message to send back to
    /// the model when the answer is not yet backed by execution, or None when the task may complete.
    pub fn completion_check(&mut self) -> Option<String> {
        if self.kind == Kind::Question || self.has_evidence() { return None; }
        if self.nudges >= MAX_EXECUTION_NUDGES { return None; }
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
        } else { status.into() };
        if self.status == "completed" { self.phase = Phase::Complete; }
        self.finished_at = Some(unix_now());
    }

    pub fn verification(&self) -> &'static str {
        match (self.kind, self.has_evidence(), self.classified_as_question.is_some()) {
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
            "errors":self.errors,"evidence":self.evidence,"started_at":self.started_at,"finished_at":self.finished_at})
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
