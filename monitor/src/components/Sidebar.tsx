// The minimizable left sidebar: file/build actions, the input/output
// validator, graph-level statistics, and the run controls (orderly Pause /
// Resume above a smaller, immediate red Stop).

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { totalOutputTokens, visualState, type RunState } from "../runstate";
import { fmtDuration, fmtTokens, type GraphModel, type Selection } from "../types";

// Resizable via the right-edge handle; dragging below the collapse threshold
// snaps to the minimized rail (the » button restores the previous width).
const SIDEBAR_MIN_W = 150;
const SIDEBAR_MAX_W = 420;
const SIDEBAR_COLLAPSE_W = 110;
const SIDEBAR_DEFAULT_W = 202;

export interface ValidationReport {
  errors: string[];
  warnings: string[];
  at: number;
}

export default function Sidebar(props: {
  mode: "build" | "exec";
  model: GraphModel | null;
  run: RunState | null;
  path: string | null;
  canBuild: boolean;
  validation: ValidationReport | null;
  onNew: () => void;
  onOpen: () => void;
  onSave: () => void;
  onSaveAs: () => void;
  onValidate: () => void;
  onRun: () => void;
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
  const [width, setWidth] = useState<number>(() => {
    const stored = Number(localStorage.getItem("gw-sidebar-width"));
    return Number.isFinite(stored) && stored >= SIDEBAR_MIN_W && stored <= SIDEBAR_MAX_W
      ? stored
      : SIDEBAR_DEFAULT_W;
  });
  const widthRef = useRef(width);
  const elRef = useRef<HTMLDivElement>(null);
  const resizing = useRef(false);
  const { mode, run } = props;

  // The add-node/edge grid is two columns only when every button label fits
  // on a single line at half the grid's width (measured against the rendered
  // font); below that it stacks in one column. Never more than two columns.
  const gridRef = useRef<HTMLDivElement>(null);
  const [gridTwoCol, setGridTwoCol] = useState(true);
  useLayoutEffect(() => {
    const el = gridRef.current;
    if (!el) return;
    const btns = Array.from(el.querySelectorAll("button"));
    if (btns.length === 0) return;
    const cs = getComputedStyle(btns[0]);
    const ctx = document.createElement("canvas").getContext("2d");
    if (!ctx) return;
    ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    const chrome =
      parseFloat(cs.paddingLeft) +
      parseFloat(cs.paddingRight) +
      parseFloat(cs.borderLeftWidth) +
      parseFloat(cs.borderRightWidth);
    const need =
      Math.max(...btns.map((b) => ctx.measureText(b.textContent ?? "").width)) +
      chrome;
    const gap = parseFloat(getComputedStyle(el).columnGap) || 0;
    setGridTwoCol((el.clientWidth - gap) / 2 >= need + 1);
  }, [width, minimized, props.model]);

  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    resizing.current = true;
    document.body.classList.add("col-resizing");
  };

  useEffect(() => {
    const stop = () => {
      resizing.current = false;
      document.body.classList.remove("col-resizing");
    };
    const move = (e: MouseEvent) => {
      if (!resizing.current) return;
      const left = elRef.current?.getBoundingClientRect().left ?? 0;
      const raw = e.clientX - left;
      if (raw < SIDEBAR_COLLAPSE_W) {
        stop();
        setMinimized(true);
        return;
      }
      const w = Math.min(SIDEBAR_MAX_W, Math.max(SIDEBAR_MIN_W, Math.round(raw)));
      widthRef.current = w;
      setWidth(w);
    };
    const up = () => {
      if (!resizing.current) return;
      stop();
      localStorage.setItem("gw-sidebar-width", String(widthRef.current));
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, []);

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
            <Row k="Output tokens" v={fmtTokens(totalOutputTokens(d))} />
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
    <div className="sidebar" ref={elRef} style={{ width }}>
      <div
        className="sidebar-handle"
        style={{ left: width - 4 }}
        title="Drag to resize"
        onMouseDown={startResize}
      />
      <div className="sidebar-head">
        <span className={"mode-badge " + mode}>{mode === "build" ? "BUILD" : "EXECUTION"}</span>
        <button className="icon-btn" title="Minimize sidebar" onClick={() => setMinimized(true)}>
          «
        </button>
      </div>

      {mode === "build" && (
        <>
          <div className="section">
            <div className="btn-col">
              <button onClick={props.onNew}>New</button>
              <button onClick={props.onOpen}>Open</button>
              <button onClick={props.onSave} disabled={!props.model}>
                Save
              </button>
              <button onClick={props.onSaveAs} disabled={!props.model}>
                Save As…
              </button>
              <button onClick={props.onValidate} disabled={!props.model} className="validate-btn">
                Run input/output validator
              </button>
            </div>
            {props.path && <div className="path-note" title={props.path}>{props.path}</div>}
          </div>

          {props.model && (
            <div className="section">
              <div
                className={"btn-grid" + (gridTwoCol ? "" : " one-col")}
                ref={gridRef}
              >
                {/* DOM order is row-major for the two-column grid: columns
                    read Script/Human/Edge and Claude/Codex/Grok/OpenRouter;
                    the odd provider button is pinned to the right column. */}
                <button onClick={() => props.onAddNode("script")}>+ Script</button>
                <button onClick={() => props.onAddNode("claude")}>+ Claude</button>
                <button onClick={() => props.onAddNode("human")}>+ Human</button>
                <button onClick={() => props.onAddNode("codex")}>+ Codex</button>
                <button onClick={props.onAddEdge}>+ Edge</button>
                <button onClick={() => props.onAddNode("grok")}>+ Grok</button>
                <button className="col-2" onClick={() => props.onAddNode("openrouter")}>
                  + OpenRouter
                </button>
              </div>
              <div className="btn-col">
                <button onClick={() => props.onSelect({ kind: "graph", id: "graph" })}>
                  Graph settings…
                </button>
              </div>
            </div>
          )}

          <div className="section">
            <div className="btn-col">
              <button
                className="run-btn"
                disabled={!props.model || !props.path}
                title={
                  !props.path
                    ? "Save the graph definition first"
                    : "Run this graph definition (seed and Monitor policy come from Graph settings)"
                }
                onClick={props.onRun}
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
