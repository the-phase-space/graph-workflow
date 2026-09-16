// graph-workflow Monitor GUI — Tauri host.
//
// The frontend owns all presentation; Python remains the authoritative
// backend for authoring semantics, validation, execution, and run control.
// This host exposes exactly the plumbing the boundary needs: file IO for the
// run-dir interchange surface (status.json / trace.jsonl / sessions/*.jsonl /
// control.json), synchronous calls into `python -m graph_workflow.gui`, and
// detached spawning of graph-definition runs. Single-instance: a second
// launch (e.g. the runtime attaching a new run) forwards its argv to the
// running window.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime};

use serde::Serialize;
use tauri::{Emitter, Manager};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
#[cfg(windows)]
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
#[cfg(windows)]
const DETACHED_PROCESS: u32 = 0x0000_0008;

// ---------------------------------------------------------------------- //
// Environment resolution
// ---------------------------------------------------------------------- //

/// The project root: the directory containing the `graph_workflow` package.
/// GW_ROOT overrides; otherwise walk up from the exe (monitor/bin/<exe> in
/// the repo layout), then from the cwd.
fn resolve_project_root() -> Option<PathBuf> {
    if let Ok(root) = std::env::var("GW_ROOT") {
        let p = PathBuf::from(root);
        if p.join("graph_workflow").is_dir() {
            return Some(p);
        }
    }
    let mut starts: Vec<PathBuf> = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            starts.push(dir.to_path_buf());
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        starts.push(cwd);
    }
    for start in starts {
        let mut dir: Option<&Path> = Some(start.as_path());
        for _ in 0..6 {
            match dir {
                Some(d) => {
                    if d.join("graph_workflow").is_dir() {
                        return Some(d.to_path_buf());
                    }
                    dir = d.parent();
                }
                None => break,
            }
        }
    }
    None
}

fn python_exe() -> String {
    std::env::var("GW_PYTHON").unwrap_or_else(|_| "python".into())
}

fn python_command(root: Option<&Path>) -> Command {
    let mut cmd = Command::new(python_exe());
    cmd.env("PYTHONUTF8", "1");
    cmd.env("PYTHONUNBUFFERED", "1");
    if let Some(root) = root {
        cmd.current_dir(root);
        let mut pythonpath = root.as_os_str().to_os_string();
        if let Ok(existing) = std::env::var("PYTHONPATH") {
            pythonpath.push(if cfg!(windows) { ";" } else { ":" });
            pythonpath.push(existing);
        }
        cmd.env("PYTHONPATH", pythonpath);
    }
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd
}

// ---------------------------------------------------------------------- //
// Commands: environment
// ---------------------------------------------------------------------- //

#[derive(Serialize)]
struct EnvInfo {
    args: Vec<String>,
    project_root: Option<String>,
    python: String,
    /// Base directory for GUI-spawned run dirs: local temp, never a synced
    /// folder (sync clients briefly lock interchange files mid-run).
    temp_runs: String,
}

#[tauri::command]
fn env_info() -> EnvInfo {
    EnvInfo {
        args: std::env::args().skip(1).collect(),
        project_root: resolve_project_root().map(|p| p.to_string_lossy().into_owned()),
        python: python_exe(),
        temp_runs: std::env::temp_dir()
            .join("graph-workflow-runs")
            .to_string_lossy()
            .into_owned(),
    }
}

// ---------------------------------------------------------------------- //
// Commands: file IO (the run-dir interchange surface)
// ---------------------------------------------------------------------- //

#[tauri::command]
fn read_text(path: String) -> Option<String> {
    fs::read(&path)
        .ok()
        .map(|b| String::from_utf8_lossy(&b).into_owned())
}

#[derive(Serialize)]
struct Tail {
    exists: bool,
    data: String,
    offset: u64,
}

/// Incremental read from a byte offset — how the GUI tails trace.jsonl and
/// sessions/*.jsonl. A file shorter than the offset (truncated/replaced)
/// restarts from zero.
#[tauri::command]
fn tail_text(path: String, offset: u64) -> Tail {
    let mut f = match File::open(&path) {
        Ok(f) => f,
        Err(_) => {
            return Tail {
                exists: false,
                data: String::new(),
                offset,
            }
        }
    };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let start = if offset > len { 0 } else { offset };
    if f.seek(SeekFrom::Start(start)).is_err() {
        return Tail {
            exists: true,
            data: String::new(),
            offset: start,
        };
    }
    let mut buf = Vec::new();
    let _ = f.read_to_end(&mut buf);
    Tail {
        exists: true,
        data: String::from_utf8_lossy(&buf).into_owned(),
        offset: start + buf.len() as u64,
    }
}

#[tauri::command]
fn write_text(path: String, content: String) -> Result<(), String> {
    let p = PathBuf::from(&path);
    if let Some(parent) = p.parent() {
        let _ = fs::create_dir_all(parent);
    }
    fs::write(&p, content).map_err(|e| e.to_string())
}

fn mtime_ms(m: &fs::Metadata) -> Option<f64> {
    m.modified()
        .ok()
        .and_then(|t| t.duration_since(SystemTime::UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as f64)
}

#[tauri::command]
fn file_mtime(path: String) -> Option<f64> {
    fs::metadata(&path).ok().and_then(|m| mtime_ms(&m))
}

#[derive(Serialize)]
struct DirEntry {
    name: String,
    is_dir: bool,
    mtime: Option<f64>,
}

#[tauri::command]
fn list_dir(path: String) -> Vec<DirEntry> {
    let mut out = Vec::new();
    if let Ok(rd) = fs::read_dir(&path) {
        for entry in rd.flatten() {
            let meta = entry.metadata().ok();
            out.push(DirEntry {
                name: entry.file_name().to_string_lossy().into_owned(),
                is_dir: meta.as_ref().map(|m| m.is_dir()).unwrap_or(false),
                mtime: meta.as_ref().and_then(mtime_ms),
            });
        }
    }
    out
}

// ---------------------------------------------------------------------- //
// Commands: the Python backend (graph_workflow.gui)
// ---------------------------------------------------------------------- //

#[derive(Serialize)]
struct BackendResult {
    code: i32,
    stdout: String,
    stderr: String,
}

/// Synchronous `python -m graph_workflow.gui <args>` with optional stdin and
/// a hard timeout. Reader threads drain the pipes so large contracts cannot
/// deadlock the pipe buffer.
#[tauri::command]
fn backend(args: Vec<String>, stdin: Option<String>, timeout_ms: Option<u64>) -> Result<BackendResult, String> {
    let root = resolve_project_root();
    let mut cmd = python_command(root.as_deref());
    cmd.arg("-m").arg("graph_workflow.gui").args(&args);
    cmd.stdin(if stdin.is_some() {
        Stdio::piped()
    } else {
        Stdio::null()
    });
    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());
    let mut child = cmd.spawn().map_err(|e| format!("cannot start python: {e}"))?;

    if let Some(text) = stdin {
        if let Some(mut pipe) = child.stdin.take() {
            let _ = pipe.write_all(text.as_bytes());
        }
    }
    let out_pipe = child.stdout.take();
    let err_pipe = child.stderr.take();
    let t_out = std::thread::spawn(move || {
        let mut s = String::new();
        if let Some(mut p) = out_pipe {
            let _ = p.read_to_string(&mut s);
        }
        s
    });
    let t_err = std::thread::spawn(move || {
        let mut s = String::new();
        if let Some(mut p) = err_pipe {
            let _ = p.read_to_string(&mut s);
        }
        s
    });

    let deadline = Instant::now() + Duration::from_millis(timeout_ms.unwrap_or(60_000));
    let code = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status.code().unwrap_or(-1),
            Ok(None) => {
                if Instant::now() > deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    break -9;
                }
                std::thread::sleep(Duration::from_millis(30));
            }
            Err(e) => return Err(format!("wait failed: {e}")),
        }
    };
    Ok(BackendResult {
        code,
        stdout: t_out.join().unwrap_or_default(),
        stderr: t_err.join().unwrap_or_default(),
    })
}

// ---------------------------------------------------------------------- //
// Commands: run a graph definition
// ---------------------------------------------------------------------- //

#[tauri::command]
fn spawn_run(file: String, run_dir: String, log_path: String) -> Result<u32, String> {
    let file_path = PathBuf::from(&file);
    let cwd = file_path
        .parent()
        .map(|p| p.to_path_buf())
        .ok_or("graph definition has no parent directory")?;
    let _ = fs::create_dir_all(&run_dir);
    if let Some(parent) = Path::new(&log_path).parent() {
        let _ = fs::create_dir_all(parent);
    }
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| format!("cannot open run log: {e}"))?;
    let log2 = log.try_clone().map_err(|e| e.to_string())?;

    let root = resolve_project_root();
    let mut cmd = python_command(root.as_deref());
    cmd.arg("-u").arg(&file_path);
    cmd.current_dir(&cwd); // graph definitions resolve paths from their own directory
    // Seed and Monitor policy are the definition's own declarations; the
    // only launcher steer is the run dir for a graph that declares none.
    // Redirected stdio keeps the runtime's auto-launch of a second Monitor
    // quiet, and a declared launch just refocuses this single instance.
    cmd.env("GW_RUN_DIR", &run_dir);
    cmd.stdin(Stdio::null());
    cmd.stdout(Stdio::from(log));
    cmd.stderr(Stdio::from(log2));
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS);
    let child = cmd.spawn().map_err(|e| format!("cannot start run: {e}"))?;
    Ok(child.id())
}

#[tauri::command]
fn process_alive(pid: u32) -> bool {
    #[cfg(windows)]
    {
        let out = Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/NH", "/FO", "CSV"])
            .creation_flags(CREATE_NO_WINDOW)
            .output();
        match out {
            Ok(o) => String::from_utf8_lossy(&o.stdout).contains(&format!("\"{pid}\"")),
            Err(_) => false,
        }
    }
    #[cfg(not(windows))]
    {
        Path::new(&format!("/proc/{pid}")).exists()
    }
}

// ---------------------------------------------------------------------- //

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            // Both entry paths converge on one shared GUI session: forward
            // the second launch's argv (e.g. --run-dir <dir>) and focus.
            let _ = app.emit("gw-attach", args);
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .invoke_handler(tauri::generate_handler![
            env_info,
            read_text,
            tail_text,
            write_text,
            file_mtime,
            list_dir,
            backend,
            spawn_run,
            process_alive
        ])
        .run(tauri::generate_context!())
        .expect("monitor failed to start");
}
