//! Engine sidecar: runs `python -m flowengine serve` and speaks newline-delimited JSON-RPC.
//!
//! Requests from the UI are assigned fresh ids, written to the child's stdin and resolved
//! when the matching response line arrives. Lines without an id are notifications
//! (`run.event`, `run.finished`) and are forwarded to the UI as `engine-notification` events.

use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
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
        // Windows installers can expose either the resource root or its parent
        // through the platform resolver, depending on the bundle layout.
        for bundled in [r.join("engine"), r.join("resources").join("engine")] {
            if bundled.join("flowengine").join("__init__.py").is_file() {
                return bundled;
            }
        }
    }
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..")
}

/// The engine (and every Python it starts) uses only its own runtime's packages: packages in the
/// user's ~/.local site-packages or on a PYTHONPATH (for example an older NumPy) must never shadow
/// the bundled ones. FLOWBENCH_DESKTOP tells the engine to keep untitled work in ~/Flowbench.
pub const ISOLATION_ENV: &[(&str, &str)] = &[("PYTHONNOUSERSITE", "1"), ("FLOWBENCH_DESKTOP", "1")];
pub const REMOVED_ENV: &[&str] = &["PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"];

pub fn python_command(root: &Path) -> PathBuf {
    if let Ok(p) = std::env::var("FLOWENGINE_PYTHON") {
        return PathBuf::from(p);
    }
    if let Some(resources) = root.parent() {
        for runtime in [resources.join("runtime"), resources.join("resources/runtime")] {
            let executable = if cfg!(windows) { "python.exe" } else { "bin/python3" };
            let bundled = runtime.join(executable);
            if bundled.is_file() {
                return bundled;
            }
        }
    }
    PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
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
        let log_dir = history.parent().unwrap_or(root);
        record_startup(log_dir, &format!("launch root={} python={}", root.display(), python.display()));
        let mut cmd = Command::new(&python);
        cmd.args(["-m", "flowengine", "serve", "--history"])
            .arg(history)
            .current_dir(root)
            .env("FLOWENGINE_HOME", history.parent().unwrap_or(root))
            .env("PYTHONIOENCODING", "utf-8")
            .env("PYTHONUNBUFFERED", "1")
            .envs(ISOLATION_ENV.iter().copied())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for name in REMOVED_ENV {
            cmd.env_remove(name);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        match cmd.spawn() {
            Ok(mut child) => {
                let pid = child.id();
                let stderr_capture = Arc::new(Mutex::new(Vec::new()));
                let capture_startup = Arc::new(AtomicBool::new(true));
                if let Some(mut stderr) = child.stderr.take() {
                    let capture = stderr_capture.clone();
                    let active = capture_startup.clone();
                    std::thread::Builder::new().name("engine-startup-stderr".into()).spawn(move || {
                        let mut chunk = [0_u8; 2048];
                        loop {
                            match stderr.read(&mut chunk) {
                                Ok(0) | Err(_) => break,
                                Ok(n) if active.load(Ordering::Relaxed) => {
                                    let mut saved = capture.lock().unwrap();
                                    let keep = n.min(8192_usize.saturating_sub(saved.len()));
                                    saved.extend_from_slice(&chunk[..keep]);
                                }
                                Ok(_) => {}
                            }
                        }
                    }).expect("spawn stderr reader");
                }
                let (line_tx, line_rx) = mpsc::channel::<String>();
                if let Some(stdout) = child.stdout.take() {
                    std::thread::Builder::new().name("engine-stdout-reader".into()).spawn(move || {
                        for line in BufReader::new(stdout).lines() {
                            match line {
                                Ok(line) => {
                                    if line_tx.send(line).is_err() { break; }
                                }
                                _ => break,
                            }
                        }
                    }).expect("spawn stdout reader");
                }
                let mut stdin = child.stdin.take();
                let mut health = match stdin.as_mut() {
                    Some(input) => startup_rpc(input, &line_rx, 1, "engine.info", json!({})),
                    None => Err("engine stdin was not piped".to_owned()),
                };
                health = health.and_then(|info| {
                    if info.get("engine").and_then(Value::as_str) != Some("flowengine") {
                        return Err("engine.info returned an unexpected engine identity".to_owned());
                    }
                    Ok(info)
                });
                if health.is_ok() {
                    health = match stdin.as_mut() {
                        Some(input) => startup_rpc(input, &line_rx, 2, "catalog.list", json!({})),
                        None => Err("engine stdin was not piped".to_owned()),
                    }.and_then(|catalog| {
                        let count = catalog.as_array().map(Vec::len).unwrap_or(0);
                        if count != 323 { return Err(format!("catalog.list returned {count} entries; expected 323")); }
                        Ok(catalog)
                    });
                }
                if health.is_ok() {
                    if let Some(workflow_path) = std::env::var_os("FLOWBENCH_STARTUP_SMOKE_WORKFLOW") {
                        let result = (|| {
                            let path = PathBuf::from(workflow_path);
                            let workflow: Value = serde_json::from_slice(&std::fs::read(&path)
                                .map_err(|e| format!("reading startup smoke workflow: {e}"))?)
                                .map_err(|e| format!("parsing startup smoke workflow: {e}"))?;
                            let base_dir = path.parent().unwrap_or(root).to_string_lossy().to_string();
                            let params = json!({"workflow":workflow,"base_dir":base_dir,
                                "clock":"2026-01-15T09:30:00"});
                            let result = startup_rpc(stdin.as_mut().ok_or("engine stdin was not piped")?,
                                &line_rx, 3, "workflow.run", params)?;
                            if result.get("status").and_then(Value::as_str) != Some("succeeded") {
                                return Err(format!("workflow.run did not succeed: {}", result.get("status").unwrap_or(&Value::Null)));
                            }
                            Ok(())
                        })();
                        health = result.map(|_| json!({"smoke":"succeeded"}));
                        if health.is_ok() { record_startup(log_dir, "workflow.run succeeded for configured startup smoke workflow"); }
                    }
                }
                let health = health.map(|_| ());
                capture_startup.store(false, Ordering::Relaxed);
                if let Err(error) = health {
                    let stderr = String::from_utf8_lossy(&stderr_capture.lock().unwrap()).to_string();
                    let _ = child.kill();
                    let status = child.wait().ok().map(|s| s.to_string()).unwrap_or_else(|| "unknown".into());
                    let detail = if stderr.trim().is_empty() { String::new() } else { format!("; startup stderr: {}", stderr.trim()) };
                    let message = format!("engine startup health check failed for {} (pid {pid}, exit {status}): {error}{detail}", python.display());
                    record_startup(log_dir, &message);
                    return Engine {
                        child: Mutex::new(None), stdin: Mutex::new(None), pending,
                        next_id: AtomicU64::new(3), launch_error: Mutex::new(Some(message)),
                    };
                }
                record_startup(log_dir, &format!("engine.info succeeded; catalog.list entries=323; pid={pid}"));
                let p = pending.clone();
                std::thread::Builder::new()
                    .name("engine-reader".into())
                    .spawn(move || {
                        for line in line_rx {
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
                    next_id: AtomicU64::new(if std::env::var_os("FLOWBENCH_STARTUP_SMOKE_WORKFLOW").is_some() { 4 } else { 3 }),
                    launch_error: Mutex::new(None),
                }
            }
            Err(e) => {
                let message = format!("Could not start the bundled Flowbench engine with '{}': {e}", python.display());
                record_startup(log_dir, &message);
                Engine { child: Mutex::new(None), stdin: Mutex::new(None), pending,
                    next_id: AtomicU64::new(3), launch_error: Mutex::new(Some(message)) }
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

fn record_startup(directory: &Path, message: &str) {
    use std::fs::OpenOptions;
    use std::io::Write as _;
    let path = directory.join("engine-startup.log");
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{message}");
    }
}

fn startup_rpc(input: &mut ChildStdin, output: &mpsc::Receiver<String>, id: u64, method: &str, params: Value) -> Result<Value, String> {
    let request = json!({"jsonrpc":"2.0", "id":id, "method":method, "params":params}).to_string();
    input.write_all(request.as_bytes()).and_then(|_| input.write_all(b"\n"))
        .and_then(|_| input.flush()).map_err(|e| format!("writing {method} startup probe: {e}"))?;
    loop {
        let line = output.recv_timeout(Duration::from_secs(30))
            .map_err(|e| format!("waiting for {method} startup response: {e}"))?;
        let response: Value = serde_json::from_str(&line)
            .map_err(|e| format!("invalid JSON from engine during {method}: {e}"))?;
        if response.get("id").and_then(Value::as_u64) != Some(id) { continue; }
        if let Some(error) = response.get("error") {
            return Err(format!("{method} RPC error: {error}"));
        }
        return response.get("result").cloned().ok_or_else(|| format!("{method} response had no result"));
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn the_engine_ignores_user_site_packages_and_python_path() {
        assert!(super::ISOLATION_ENV.contains(&("PYTHONNOUSERSITE", "1")));
        assert!(super::REMOVED_ENV.contains(&"PYTHONPATH") && super::REMOVED_ENV.contains(&"PYTHONHOME"));
    }

    use super::*;

    #[test]
    fn classifies_responses_and_notifications() {
        assert!(matches!(classify(r#"{"jsonrpc":"2.0","id":3,"result":1}"#), Some(Ok((3, _)))));
        assert!(matches!(classify(r#"{"jsonrpc":"2.0","method":"run.event","params":{}}"#), Some(Err(_))));
        assert!(classify("not json").is_none());
        assert!(classify(r#"{"jsonrpc":"2.0"}"#).is_none());
    }

    #[test]
    fn discovers_bundled_engine_and_python_from_resource_root_or_parent() {
        let dir = std::env::temp_dir().join(format!("flowbench-resources-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let resources = dir.join("resources");
        std::fs::create_dir_all(resources.join("engine/flowengine")).unwrap();
        std::fs::write(resources.join("engine/flowengine/__init__.py"), "").unwrap();
        let executable = if cfg!(windows) { "runtime/python.exe" } else { "runtime/bin/python3" };
        std::fs::create_dir_all(resources.join(executable).parent().unwrap()).unwrap();
        std::fs::write(resources.join(executable), "").unwrap();

        let root = engine_root(Some(dir.clone()));
        assert_eq!(root, resources.join("engine"));
        assert_eq!(python_command(&root), resources.join(executable));
        let _ = std::fs::remove_dir_all(dir);
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
