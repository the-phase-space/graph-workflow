// graph-workflow human-input shell.
//
// Invoked by the runtime as:
//   human-shell --request <request.json> --response <response.json>
//               [--auto-response <mapping.json>]
//
// The request file carries the node's prompt, captured inputs, and declared
// output schema. The shell blocks until the human submits, then writes
// {"outputs": {...}} to the response path and exits. With --auto-response a
// mapping file ({"<node_id>": {...}, "*": {...}}) answers headlessly — no
// window — which is how automated tests exercise human nodes end to end.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs;
use std::path::PathBuf;
use std::process::exit;

use serde_json::{json, Map, Value};

struct ShellState {
    request: Value,
    response_path: PathBuf,
}

#[tauri::command]
fn get_request(state: tauri::State<ShellState>) -> Value {
    state.request.clone()
}

#[tauri::command]
fn submit(
    outputs: Value,
    state: tauri::State<ShellState>,
    app: tauri::AppHandle,
) -> Result<(), String> {
    let body = serde_json::to_string_pretty(&json!({ "outputs": outputs }))
        .map_err(|e| e.to_string())?;
    fs::write(&state.response_path, body).map_err(|e| e.to_string())?;
    app.exit(0);
    Ok(())
}

fn read_json(path: &PathBuf, what: &str) -> Value {
    let text = fs::read_to_string(path)
        .unwrap_or_else(|e| die(&format!("cannot read {what} file {path:?}: {e}")));
    serde_json::from_str(&text)
        .unwrap_or_else(|e| die(&format!("cannot parse {what} file {path:?}: {e}")))
}

fn die(msg: &str) -> ! {
    eprintln!("human-shell: {msg}");
    exit(2);
}

fn main() {
    let mut request_path: Option<PathBuf> = None;
    let mut response_path: Option<PathBuf> = None;
    let mut auto_path: Option<PathBuf> = None;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--request" => request_path = args.next().map(PathBuf::from),
            "--response" => response_path = args.next().map(PathBuf::from),
            "--auto-response" => auto_path = args.next().map(PathBuf::from),
            other => die(&format!("unknown argument {other:?}")),
        }
    }
    let request_path = request_path.unwrap_or_else(|| die("--request is required"));
    let response_path = response_path.unwrap_or_else(|| die("--response is required"));
    let request = read_json(&request_path, "request");

    if let Some(auto) = auto_path {
        let mapping = read_json(&auto, "auto-response");
        let node = request.get("node").and_then(Value::as_str).unwrap_or("");
        let outputs = mapping
            .get(node)
            .or_else(|| mapping.get("*"))
            .cloned()
            .unwrap_or(Value::Object(Map::new()));
        let body = serde_json::to_string_pretty(&json!({ "outputs": outputs }))
            .unwrap_or_else(|e| die(&format!("serialize response: {e}")));
        fs::write(&response_path, body)
            .unwrap_or_else(|e| die(&format!("write response {response_path:?}: {e}")));
        return; // headless: no window
    }

    tauri::Builder::default()
        .manage(ShellState { request, response_path })
        .invoke_handler(tauri::generate_handler![get_request, submit])
        .run(tauri::generate_context!())
        .unwrap_or_else(|e| die(&format!("shell failed: {e}")));
}
