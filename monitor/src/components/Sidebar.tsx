// The minimizable left sidebar: file/build actions, the input/output
// validator, graph-level statistics, and the run controls (orderly Pause /
// Resume above a smaller, immediate red Stop).

import { useState } from "react";
import { totalTokens, visualState, type RunState } from "../runstate";
import { fmtDuration, fmtTokens, type GraphModel, type Selection } from "../types";

export interface ValidationReport {
  errors: string[];
  warnings: string[];
  at: number;
}

export default function Sidebar(props: {
  mode: "build" | "exec";
  model: GraphModel | null;
  run: RunState | null;
  dirty: boolean;
  path: string | null;
  canBuild: boolean;
  validation: ValidationReport | null;
  onNew: () => void;
  onOpen: () => void;
  onSave: () => void;
  onSaveAs: () => void;
  onValidate: () => void;
  onRun: (seed: string) => void;
  onAttach: () => void;
  onAddNode: (kind: string) => void;
  onAddEdge: () => void;
  onSelect: (s: Selection | null) => void;
  onPause: () => void;
  onResume: () => void;
  onStop: () => void;
  onBackToBuild: () => void;
  theme: string;
  onToggleTheme: () => void;
}) {
  const [minimized, setMinimized] = useState(false);
  const [seed, setSeed] = useState("");
  const { mode, run } = props;

  if (minimized) {
    return (
      <div className="sidebar minimized">
        <button className="icon-btn" title="Expand sidebar" onClick={() => setMinimized(false)}>
          »
        </button>
      </div>
    );
  }

  const status = run?.status;
  const runState = status?.state ?? null;
  const ended = runState?.startsWith("ended") ?? false;
  const paused = runState === "paused";
  const pausing = runState === "pausing";

  let stats = null;
  if (mode === "exec" && run) {
    const d = run.derived;
    const nowS = Date.now() / 1000;
    const elapsed =
      d.runStartTs != null ? (d.runEndTs ?? nowS) - d.runStartTs : null;
    const counts: Record<string, number> = {
      running: 0,
      ready: 0,
      paused: 0,
      idle: 0,
      completed: 0,
      error: 0,
    };
    if (status)
      for (const [id, sn] of Object.entries(status.nodes)) {
        counts[visualState(sn.state, d.nodes.get(id))] += 1;
      }
    stats = (
      <div className="section">
        <div className="section-title">Run</div>
        <table className="stats">
          <tbody>
            <Row k="Run" v={d.runId ?? "—"} mono />
            <Row k="Seed" v={d.seed != null ? String(d.seed) : "—"} mono />
            <Row k="State" v={runState ?? "starting…"} />
            <Row k="Elapsed" v={elapsed != null ? fmtDuration(elapsed) : "—"} />
            <Row
              k="Tokens"
              v={fmtTokens(totalTokens(d, run.liveTokens, status?.nodes))}
            />
            <Row k="Events" v={String(d.eventCount)} />
          </tbody>
        </table>
        <div className="chips">
          <Chip label="running" n={counts.running} color="#f2c521" />
          <Chip label="ready" n={counts.ready} color="#3f8cff" />
          <Chip label="paused" n={counts.paused} color="#e8a33d" />
          <Chip label="done" n={counts.completed} color="#2ecc71" />
          <Chip label="error" n={counts.error} color="#ff4136" />
          <Chip label="idle" n={counts.idle} color="var(--muted)" />
        </div>
        {ended && (
          <div className="run-ended">run over: {runState?.slice("ended:".length)}</div>
        )}
      </div>
    );
  }

  return (
    <div className="sidebar">
      <div className="sidebar-head">
        <span className={"mode-badge " + mode}>{mode === "build" ? "BUILD" : "EXECUTION"}</span>
        <button className="icon-btn" title="Minimize sidebar" onClick={() => setMinimized(true)}>
          «
        </button>
      </div>

      {mode === "build" && (
        <>
          <div className="section">
            <div className="section-title">Graph definition</div>
            <div className="btn-col">
              <button onClick={props.onNew}>New</button>
              <button onClick={props.onOpen}>Open…</button>
              <button onClick={props.onSave} disabled={!props.model}>
                Save{props.dirty ? " •" : ""} <kbd>Ctrl+S</kbd>
              </button>
              <button onClick={props.onSaveAs} disabled={!props.model}>
                Save As…
              </button>
              <button onClick={props.onValidate} disabled={!props.model} className="validate-btn">
                ✓ Run input/output validator
              </button>
            </div>
            {props.path && <div className="path-note" title={props.path}>{props.path}</div>}
          </div>

          {props.model && (
            <div className="section">
              <div className="section-title">Compose</div>
              <div className="btn-grid">
                <button onClick={() => props.onAddNode("script")}>+ Script</button>
                <button onClick={() => props.onAddNode("human")}>+ Human</button>
                <button onClick={() => props.onAddNode("claude")}>+ Claude</button>
                <button onClick={() => props.onAddNode("codex")}>+ Codex</button>
                <button onClick={() => props.onAddNode("grok")}>+ Grok</button>
                <button onClick={props.onAddEdge}>+ Edge</button>
              </div>
              <div className="btn-col">
                <button onClick={() => props.onSelect({ kind: "graph", id: "graph" })}>
                  Graph settings…
                </button>
              </div>
            </div>
          )}

          <div className="section">
            <div className="section-title">Execute</div>
            <div className="seed-row">
              <label>Seed</label>
              <input
                value={seed}
                onChange={(e) => setSeed(e.target.value)}
                placeholder="random"
                spellCheck={false}
              />
            </div>
            <div className="btn-col">
              <button
                className="run-btn"
                disabled={!props.model || !props.path}
                title={!props.path ? "Save the graph definition first" : "Run this graph definition"}
                onClick={() => props.onRun(seed)}
              >
                ▶ Run
              </button>
              <button onClick={props.onAttach}>Attach to run…</button>
            </div>
          </div>
        </>
      )}

      {mode === "exec" && (
        <>
          <div className="section">
            <div className="btn-col">
              <button
                className={"pause-btn" + (paused ? " resume" : "")}
                disabled={!status || ended || pausing}
                onClick={paused ? props.onResume : props.onPause}
              >
                {pausing ? "Pausing …" : paused ? "▶ Resume" : "⏸ Pause"}
              </button>
              <button className="stop-btn" disabled={!status || ended} onClick={props.onStop}>
                ■ Stop
              </button>
            </div>
          </div>
          {stats}
          {run && run.derived.warnings.length > 0 && (
            <div className="section">
              <div className="section-title">Warnings ({run.derived.warnings.length})</div>
              <div className="issue-list">
                {run.derived.warnings.slice(-8).map((w, i) => (
                  <div className="issue warn" key={i}>
                    {w}
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="section">
            <div className="btn-col">
              <button onClick={props.onBackToBuild} disabled={!props.canBuild}>
                ← Back to build
              </button>
            </div>
          </div>
        </>
      )}

      {props.validation && (
        <div className="section">
          <div className="section-title">
            Validation {props.validation.errors.length === 0 ? "✓ passed" : "✗ failed"}
          </div>
          <div className="issue-list">
            {props.validation.errors.map((e, i) => (
              <div className="issue err" key={"e" + i}>
                {e}
              </div>
            ))}
            {props.validation.warnings.map((w, i) => (
              <div className="issue warn" key={"w" + i}>
                {w}
              </div>
            ))}
            {props.validation.errors.length === 0 &&
              props.validation.warnings.length === 0 && (
                <div className="issue ok">No problems found.</div>
              )}
          </div>
        </div>
      )}

      <div className="sidebar-foot">
        <button className="icon-btn" title="Toggle light/dark" onClick={props.onToggleTheme}>
          {props.theme === "dark" ? "☀" : "🌙"}
        </button>
      </div>
    </div>
  );
}

function Row(props: { k: string; v: string; mono?: boolean }) {
  return (
    <tr>
      <td className="k">{props.k}</td>
      <td className={props.mono ? "v mono" : "v"} title={props.v}>
        {props.v}
      </td>
    </tr>
  );
}

function Chip(props: { label: string; n: number; color: string }) {
  return (
    <span className="chip" style={{ borderColor: props.color }}>
      <span className="chip-dot" style={{ background: props.color }} />
      {props.n} {props.label}
    </span>
  );
}
