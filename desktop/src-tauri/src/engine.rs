//! Engine sidecar: runs `python -m flowengine serve` and speaks newline-delimited JSON-RPC.
//!
//! Requests from the UI are assigned fresh ids, written to the child's stdin and resolved
//! when the matching response line arrives. Lines without an id are notifications
//! (`run.event`, `run.finished`) and are forwarded to the UI as `engine-notification` events.

use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::oneshot;

pub type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>>;

pub struct Engine {
    child: Mutex<Option<Child>>,
    stdin: Mutex<Option<ChildStdin>>,
    pending: Pending,
    next_id: AtomicU64,
    pub launch_error: Mutex<Option<String>>,
}

/// Where the flowengine package lives: FLOWENGINE_ROOT, else the bundled resources
/// (`<resources>/engine`), else the repository root when running from source.
pub fn engine_root(resource_dir: Option<PathBuf>) -> PathBuf {
    if let Ok(p) = std::env::var("FLOWENGINE_ROOT") {
        return PathBuf::from(p);
    }
    if let Some(r) = resource_dir {
        let bundled = r.join("engine");
        if bundled.join("flowengine").join("__init__.py").is_file() {
            return bundled;
        }
    }
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..")
}

pub fn python_command(root: &Path) -> PathBuf {
    if let Ok(p) = std::env::var("FLOWENGINE_PYTHON") {
        return PathBuf::from(p);
    }
    if let Some(resources) = root.parent() {
        let bundled = resources.join("runtime").join("python.exe");
        if bundled.is_file() {
            return bundled;
        }
    }
    PathBuf::from("python")
}

/// Parse one stdout line. Returns Some((id, value)) for responses, None + notification otherwise.
pub fn classify(line: &str) -> Option<Result<(u64, Value), Value>> {
    let v: Value = serde_json::from_str(line.trim()).ok()?;
    match v.get("id").and_then(|i| i.as_u64()) {
        Some(id) => Some(Ok((id, v))),
        None if v.get("method").is_some() => Some(Err(v)),
        None => None,
    }
}

impl Engine {
    pub fn start(root: &Path, history: &Path, notify: impl Fn(Value) + Send + 'static) -> Engine {
        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
        let python = python_command(root);
        let mut cmd = Command::new(&python);
        cmd.args(["-m", "flowengine", "serve", "--history"])
            .arg(history)
            .current_dir(root)
            .env("FLOWENGINE_HOME", history.parent().unwrap_or(root))
            .env("PYTHONIOENCODING", "utf-8")
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        match cmd.spawn() {
            Ok(mut child) => {
                let stdout = child.stdout.take().expect("piped stdout");
                let stdin = child.stdin.take();
                let p = pending.clone();
                std::thread::Builder::new()
                    .name("engine-reader".into())
                    .spawn(move || {
                        for line in BufReader::new(stdout).lines() {
                            let Ok(line) = line else { break };
                            match classify(&line) {
                                Some(Ok((id, v))) => {
                                    if let Some(tx) = p.lock().unwrap().remove(&id) {
                                        let _ = tx.send(v);
                                    }
                                }
                                Some(Err(n)) => notify(n),
                                None => {}
                            }
                        }
                        // engine exited: fail all waiters and tell the UI
                        for (_, tx) in p.lock().unwrap().drain() {
                            let _ = tx.send(json!({"error": {"code": -32000, "message": "The engine process exited"}}));
                        }
                        notify(json!({"jsonrpc": "2.0", "method": "engine.exited", "params": {}}));
                    })
                    .expect("spawn reader thread");
                Engine {
                    child: Mutex::new(Some(child)),
                    stdin: Mutex::new(stdin),
                    pending,
                    next_id: AtomicU64::new(1),
                    launch_error: Mutex::new(None),
                }
            }
            Err(e) => Engine {
                child: Mutex::new(None),
                stdin: Mutex::new(None),
                pending,
                next_id: AtomicU64::new(1),
                launch_error: Mutex::new(Some(format!(
                    "Could not start the bundled Flowbench engine with '{}': {e}. Reinstall the application or set FLOWENGINE_PYTHON for development.",
                    python.display()
                ))),
            },
        }
    }

    pub async fn request(&self, method: &str, params: Value, timeout: Duration) -> Result<Value, String> {
        if let Some(err) = self.launch_error.lock().unwrap().clone() {
            return Err(err);
        }
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(id, tx);
        let line = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}).to_string();
        {
            let mut guard = self.stdin.lock().unwrap();
            let stdin = guard.as_mut().ok_or("engine is not running")?;
            stdin
                .write_all(line.as_bytes())
                .and_then(|_| stdin.write_all(b"\n"))
                .and_then(|_| stdin.flush())
                .map_err(|e| format!("engine write failed: {e}"))?;
        }
        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(v)) => Ok(v),
            Ok(Err(_)) => Err("engine dropped the request".into()),
            Err(_) => {
                self.pending.lock().unwrap().remove(&id);
                Err(format!("engine did not answer '{method}' within {} s", timeout.as_secs()))
            }
        }
    }

    pub fn stop(&self) {
        self.stdin.lock().unwrap().take(); // closing stdin ends the serve loop
        if let Some(mut child) = self.child.lock().unwrap().take() {
            std::thread::sleep(Duration::from_millis(200));
            if let Ok(None) = child.try_wait() {
                let _ = child.kill();
            }
            let _ = child.wait();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_responses_and_notifications() {
        assert!(matches!(classify(r#"{"jsonrpc":"2.0","id":3,"result":1}"#), Some(Ok((3, _)))));
        assert!(matches!(classify(r#"{"jsonrpc":"2.0","method":"run.event","params":{}}"#), Some(Err(_))));
        assert!(classify("not json").is_none());
        assert!(classify(r#"{"jsonrpc":"2.0"}"#).is_none());
    }

    #[test]
    fn round_trip_with_real_engine() {
        // Requires Python with the repository's requirements; skipped if the engine cannot start.
        let root = engine_root(None);
        let dir = std::env::temp_dir().join(format!("flowbench-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let engine = Engine::start(&root, &dir.join("h.sqlite"), |_| {});
        if engine.launch_error.lock().unwrap().is_some() {
            eprintln!("python not available; skipping");
            return;
        }
        let rt = tokio::runtime::Builder::new_current_thread().enable_time().build().unwrap();
        let v = rt.block_on(engine.request("engine.info", json!({}), Duration::from_secs(60))).unwrap();
        assert_eq!(v["result"]["engine"], "flowengine");
        let v = rt.block_on(engine.request("catalog.list", json!({}), Duration::from_secs(60))).unwrap();
        assert_eq!(v["result"].as_array().unwrap().len(), 323);
        engine.stop();
    }
}
