// Thin wrappers over the Tauri host commands and the Python backend
// (`python -m graph_workflow.gui`). Python is authoritative for all
// authoring semantics; the frontend never derives graph semantics itself.

import { invoke } from "@tauri-apps/api/core";
import type { Contract } from "./types";

export interface EnvInfo {
  args: string[];
  project_root: string | null;
  python: string;
  temp_runs: string;
}

export const envInfo = () => invoke<EnvInfo>("env_info");

export const readText = (path: string) =>
  invoke<string | null>("read_text", { path });

export interface Tail {
  exists: boolean;
  data: string;
  offset: number;
}

export const tailText = (path: string, offset: number) =>
  invoke<Tail>("tail_text", { path, offset });

export const writeText = (path: string, content: string) =>
  invoke<void>("write_text", { path, content });

export interface DirEntry {
  name: string;
  is_dir: boolean;
  mtime: number | null;
}

export const listDir = (path: string) => invoke<DirEntry[]>("list_dir", { path });

export const spawnRun = (file: string, runDir: string, seed: string | null, logPath: string) =>
  invoke<number>("spawn_run", { file, runDir, seed, logPath });

interface BackendRaw {
  code: number;
  stdout: string;
  stderr: string;
}

async function backend<T>(args: string[], stdin?: string): Promise<T> {
  const raw = await invoke<BackendRaw>("backend", {
    args,
    stdin: stdin ?? null,
    timeoutMs: 90_000,
  });
  try {
    return JSON.parse(raw.stdout) as T;
  } catch {
    // A graph definition may write to stdout out-of-band; recover the
    // payload from the last parseable JSON line.
    for (const line of raw.stdout.trim().split(/\r?\n/).reverse()) {
      const s = line.trim();
      if (s.startsWith("{")) {
        try {
          return JSON.parse(s) as T;
        } catch {
          /* keep looking */
        }
      }
    }
    throw new Error(
      `backend ${args[0]} produced no JSON (exit ${raw.code}): ` +
        (raw.stderr || raw.stdout || "no output").slice(0, 800),
    );
  }
}

export interface BackendError {
  ok: false;
  error: string;
  traceback?: string;
}

export type LoadResult = Contract | BackendError;

export const loadGraph = (path: string) => backend<LoadResult>(["load", path]);

export interface ValidateResult {
  ok: boolean;
  errors?: string[];
  warnings?: string[];
  error?: string;
}

export const validateGraph = (path: string) =>
  backend<ValidateResult>(["validate", path]);

export const saveGraph = (path: string, contract: Contract, force: boolean) =>
  backend<LoadResult>(
    force ? ["save", path, "--force"] : ["save", path],
    JSON.stringify(contract),
  );

export const templateGraph = (name: string) =>
  backend<LoadResult>(["template", name]);

export function isBackendError(r: LoadResult): r is BackendError {
  return !(r as Contract).ok;
}

export const controlRun = (runDir: string, command: "pause" | "resume" | "stop") =>
  writeText(joinPath(runDir, "control.json"), JSON.stringify({ command }));

export function joinPath(...parts: string[]): string {
  return parts
    .map((p, i) => (i === 0 ? p.replace(/[\\/]+$/, "") : p.replace(/^[\\/]+|[\\/]+$/g, "")))
    .join("\\");
}

export function dirname(path: string): string {
  const i = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
  return i > 0 ? path.slice(0, i) : path;
}

export function basename(path: string): string {
  const i = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
  return i >= 0 ? path.slice(i + 1) : path;
}
