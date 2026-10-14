mod engine;

use engine::{engine_root, Engine};
use serde_json::Value;
use std::time::Duration;
use tauri::{Emitter, Manager};

/// Forward one JSON-RPC request to the engine sidecar and return the full response object.
#[tauri::command]
async fn engine_request(state: tauri::State<'_, Engine>, method: String, params: Value) -> Result<Value, String> {
    // Synchronous workflow runs can legitimately take long; everything else should be quick.
    let timeout = if method == "workflow.run" { 3600 } else { 300 };
    state.request(&method, params, Duration::from_secs(timeout)).await
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let root = engine_root(app.path().resource_dir().ok());
            let data = app.path().app_data_dir()?;
            std::fs::create_dir_all(&data)?;
            let handle = app.handle().clone();
            let engine = Engine::start(&root, &data.join("history.sqlite"), move |msg| {
                let _ = handle.emit("engine-notification", msg);
            });
            app.manage(engine);
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                window.app_handle().state::<Engine>().stop();
            }
        })
        .invoke_handler(tauri::generate_handler![engine_request])
        .run(tauri::generate_context!())
        .expect("error while running Flowbench");
}
