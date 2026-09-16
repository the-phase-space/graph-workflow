// Execution-mode state: polls the run directory's interchange surface —
// status.json (atomic snapshot), trace.jsonl (append-only event log), and
// sessions/*.jsonl (live provider streams) — into derived per-node runtime
// state and per-node activity logs. The runtime is authoritative; this file
// only reads and derives.

import { useEffect, useRef, useState } from "react";
import { joinPath, listDir, readText, tailText } from "./ipc";
import {
  GEN,
  outputTokens,
  type ActivityItem,
  type GraphModel,
  type NodeRuntime,
  type RunDerived,
  type StatusSnapshot,
  type TraceEvent,
} from "./types";

function freshNode(): NodeRuntime {
  return {
    everRan: false,
    lastStartTs: null,
    lastError: null,
    commits: 0,
    invocations: 0,
    tokensCommitted: 0,
    tokensLive: 0,
    contextSize: 0,
    sessionId: null,
    receivedInputs: new Set(),
    writtenOutputs: new Set(),
    outputs: {},
    outputState: "UNSET",
    durationS: null,
  };
}

/** Convert the trace's graph.to_json() form into the GraphModel shape the
 * canvas renders (they are already nearly identical). */
function graphFromTrace(g: any): GraphModel {
  return {
    name: g?.name ?? "run",
    semaphores: g?.semaphores ?? { script: 4, llm: 2, human: 1, providers: {} },
    pause_message: g?.pause_message ?? null,
    resume_message: g?.resume_message ?? null,
    run_dir: g?.run_dir ?? null,
    seed: g?.seed ?? null,
    monitor: g?.monitor ?? null,
    nodes: (g?.nodes ?? []).map((n: any) => ({
      id: n.id,
      rank: n.rank,
      type: n.type,
      inputs: n.inputs ?? {},
      outputs: n.outputs ?? {},
      gate: n.gate ?? null,
      output_expression: n.output_expression ?? null,
      provider: n.provider,
      model: n.model,
      effort: n.effort,
      prompt: n.prompt,
      persist_from: n.persist_from,
    })),
    edges: (g?.edges ?? []).map((e: any) => ({
      id: e.id,
      inputs: e.inputs ?? [],
      outputs: e.outputs ?? [],
      universal_gate: e.universal_gate ?? { expression: "True" },
      input_gates: e.input_gates ?? {},
      output_gates: e.output_gates ?? {},
    })),
  };
}

function applyEvent(d: RunDerived, ev: TraceEvent) {
  d.eventCount++;
  const node = ev.node ? d.nodes.get(ev.node) ?? freshNode() : null;
  if (ev.node && node) d.nodes.set(ev.node, node);
  switch (ev.event) {
    case "run_start":
      d.runId = ev.run_id ?? null;
      d.seed = ev.seed ?? null;
      d.runStartTs = ev.ts;
      d.graph = graphFromTrace(ev.graph);
      break;
    case "resumed_from_disk":
      d.runEndTs = null;
      d.endReason = null;
      break;
    case "ready":
      if (node) node.receivedInputs = new Set(Object.keys(ev.inputs ?? {}));
      break;
    case "start":
      if (node) {
        node.everRan = true;
        node.lastStartTs = ev.ts;
        node.invocations = Math.max(node.invocations, ev.invocation ?? 0);
        node.tokensLive = 0;
      }
      break;
    case "commit":
      if (node) {
        node.commits += 1;
        node.lastStartTs = null;
        node.lastError = ev.error ?? null;
        node.outputs = ev.outputs ?? {};
        node.writtenOutputs = new Set(Object.keys(ev.outputs ?? {}));
        node.outputState = ev.output_state;
        const m = ev.metrics ?? {};
        node.tokensCommitted += outputTokens(m.usage);
        node.tokensLive = 0;
        if (typeof m.context_size === "number") node.contextSize = m.context_size;
        if (typeof m.duration_s === "number") node.durationS = m.duration_s;
        if (typeof m.session_id === "string") node.sessionId = m.session_id;
      }
      break;
    case "warning":
      if (ev.message) d.warnings.push(ev.message);
      break;
    case "run_end":
      d.runEndTs = ev.ts;
      d.endReason = ev.reason ?? "quiescent";
      break;
  }
}

// ---------------------------------------------------------------------- //
// Provider stream parsing (sessions/*.jsonl)
// ---------------------------------------------------------------------- //

function clip(text: string, n = 4000): string {
  return text.length > n ? text.slice(0, n) + " …" : text;
}

function contentToText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content))
    return content
      .map((b: any) => (typeof b === "string" ? b : b?.text ?? ""))
      .filter(Boolean)
      .join("\n");
  return "";
}

/** One provider-stream line -> zero or more activity items. Handles the
 * claude stream-json vocabulary, the codex app-server JSON-RPC vocabulary,
 * and grok's terminal JSON blob; unknown lines are ignored. */
function parseStreamLine(obj: any, ts: number | null): { items: ActivityItem[]; liveTokens: number | null } {
  const items: ActivityItem[] = [];
  let liveTokens: number | null = null;

  const type = obj?.type;
  if (type === "assistant" || type === "user") {
    const msg = obj.message ?? {};
    for (const block of msg.content ?? []) {
      if (!block || typeof block !== "object") continue;
      if (block.type === "text" && block.text)
        items.push({ kind: "message", label: type === "user" ? "user" : "assistant", text: block.text, ts });
      else if (block.type === "thinking" && (block.thinking || block.summary))
        items.push({ kind: "thought", label: "thinking", text: block.thinking || block.summary, ts });
      else if (block.type === "tool_use")
        items.push({
          kind: "tool",
          label: `tool: ${block.name ?? "?"}`,
          text: clip(JSON.stringify(block.input ?? {}, null, 1) ?? "", 1500),
          ts,
        });
      else if (block.type === "tool_result")
        items.push({
          kind: "tool-result",
          label: "tool result",
          text: clip(contentToText(block.content), 1500),
          ts,
        });
    }
    // claude's assistant events carry the message-start usage snapshot
    // (output_tokens ~1-4 regardless of reply length); only the terminal
    // `result` event reports real output tokens, so no live update here.
    return { items, liveTokens };
  }
  if (type === "result") {
    if (obj.usage) liveTokens = outputTokens(obj.usage);
    items.push({
      kind: "info",
      label: obj.is_error ? "result (error)" : "result",
      text: clip(typeof obj.result === "string" ? obj.result : JSON.stringify(obj.result ?? "")),
      ts,
    });
    return { items, liveTokens };
  }
  if (type === "system") {
    if (obj.subtype === "init")
      items.push({ kind: "info", label: "session started", text: obj.model ? `model ${obj.model}` : "", ts });
    return { items, liveTokens };
  }

  // codex app-server notifications
  const method = obj?.method;
  if (typeof method === "string") {
    const params = obj.params ?? {};
    if (method === "item/completed") {
      const item = params.item ?? {};
      const itype = item.type ?? item.item_type;
      if (itype === "agentMessage" || itype === "agent_message")
        items.push({ kind: "message", label: "assistant", text: item.text ?? "", ts });
      else if (itype === "reasoning")
        items.push({
          kind: "thought",
          label: "reasoning",
          text: item.text ?? item.summary ?? contentToText(item.content),
          ts,
        });
      else
        items.push({
          kind: "tool",
          label: `item: ${itype ?? "?"}`,
          text: clip(item.command ?? item.text ?? JSON.stringify(item, null, 1) ?? "", 1500),
          ts,
        });
    } else if (method === "thread/tokenUsage/updated") {
      // `total` is cumulative across the thread — the live figure we want.
      const n = outputTokens(params.tokenUsage?.total ?? params.tokenUsage);
      if (n) liveTokens = n;
    } else if (method === "turn/started") {
      items.push({ kind: "info", label: "turn started", text: "", ts });
    } else if (method === "turn/completed" || method === "turn/failed") {
      items.push({
        kind: method === "turn/failed" ? "error" : "info",
        label: method,
        text: "",
        ts,
      });
    } else if (method === "error") {
      items.push({ kind: "error", label: "error", text: clip(JSON.stringify(params)), ts });
    }
    return { items, liveTokens };
  }

  // grok / generic terminal blob
  for (const key of ["result", "response", "text", "output", "content", "message"]) {
    const v = obj?.[key];
    if (typeof v === "string" && v) {
      items.push({ kind: "message", label: "assistant", text: v, ts });
      if (obj.usage) liveTokens = outputTokens(obj.usage);
      return { items, liveTokens };
    }
  }
  return { items, liveTokens };
}

// ---------------------------------------------------------------------- //
// The polling hook
// ---------------------------------------------------------------------- //

export interface RunState {
  status: StatusSnapshot | null;
  derived: RunDerived;
  activity: Map<string, ActivityItem[]>;
  liveTokens: Map<string, number>;
  runLogTail: string;
  tick: number; // bumps every poll so timers re-render
}

export function useRunState(runDir: string | null): RunState {
  const [tick, setTick] = useState(0);
  const [status, setStatus] = useState<StatusSnapshot | null>(null);
  const derivedRef = useRef<RunDerived>(newDerived());
  const activityRef = useRef<Map<string, ActivityItem[]>>(new Map());
  const liveTokensRef = useRef<Map<string, number>>(new Map());
  const traceOffset = useRef(0);
  const tracePartial = useRef("");
  const sessionOffsets = useRef<Map<string, number>>(new Map());
  const sessionPartial = useRef<Map<string, string>>(new Map());
  const runLogRef = useRef("");
  const busy = useRef(false);

  useEffect(() => {
    derivedRef.current = newDerived();
    activityRef.current = new Map();
    liveTokensRef.current = new Map();
    traceOffset.current = 0;
    tracePartial.current = "";
    sessionOffsets.current = new Map();
    sessionPartial.current = new Map();
    runLogRef.current = "";
    setStatus(null);
    setTick((t) => t + 1);
    if (!runDir) return;

    const poll = async () => {
      if (busy.current) return;
      busy.current = true;
      try {
        // status.json — atomic snapshot
        const statusText = await readText(joinPath(runDir, "status.json"));
        if (statusText) {
          try {
            setStatus(JSON.parse(statusText) as StatusSnapshot);
          } catch {
            /* torn write; next poll wins */
          }
        }
        // trace.jsonl — incremental
        const t = await tailText(joinPath(runDir, "trace.jsonl"), traceOffset.current);
        if (t.exists && t.data) {
          if (t.offset < traceOffset.current) {
            derivedRef.current = newDerived();
            tracePartial.current = "";
          }
          traceOffset.current = t.offset;
          const text = tracePartial.current + t.data;
          const lines = text.split("\n");
          tracePartial.current = lines.pop() ?? "";
          for (const line of lines) {
            const s = line.trim();
            if (!s) continue;
            try {
              applyEvent(derivedRef.current, JSON.parse(s) as TraceEvent);
            } catch {
              /* torn line */
            }
          }
        }
        // sessions/*.jsonl — incremental per file
        const entries = await listDir(joinPath(runDir, "sessions")).catch(() => []);
        for (const entry of entries) {
          if (entry.is_dir || !entry.name.endsWith(".jsonl")) continue;
          const m = entry.name.match(/^(\w+)-(\d{3})(-resume|-pause)?\.jsonl$/);
          if (!m) continue;
          const nodeId = m[1];
          const liveKey = `${nodeId}#${parseInt(m[2], 10)}`;
          const path = joinPath(runDir, "sessions", entry.name);
          const prev = sessionOffsets.current.get(entry.name) ?? 0;
          const tail = await tailText(path, prev);
          if (!tail.exists || !tail.data) continue;
          if (tail.offset < prev) sessionPartial.current.set(entry.name, "");
          sessionOffsets.current.set(entry.name, tail.offset);
          const text = (sessionPartial.current.get(entry.name) ?? "") + tail.data;
          const lines = text.split("\n");
          sessionPartial.current.set(entry.name, lines.pop() ?? "");
          const list = activityRef.current.get(nodeId) ?? [];
          for (const line of lines) {
            const s = line.trim();
            if (!s.startsWith("{")) continue;
            try {
              const parsed = parseStreamLine(JSON.parse(s), null);
              list.push(...parsed.items);
              if (parsed.liveTokens != null)
                liveTokensRef.current.set(liveKey, parsed.liveTokens);
            } catch {
              /* not JSON */
            }
          }
          if (list.length > 600) list.splice(0, list.length - 600);
          activityRef.current.set(nodeId, list);
        }
        // run.log — GUI-spawned runs land stdout/stderr here
        const log = await readText(joinPath(runDir, "run.log"));
        if (log != null) runLogRef.current = log.slice(-8000);
      } finally {
        busy.current = false;
        setTick((t) => t + 1);
      }
    };
    void poll();
    const iv = setInterval(poll, 450);
    return () => clearInterval(iv);
  }, [runDir]);

  return {
    status,
    derived: derivedRef.current,
    activity: activityRef.current,
    liveTokens: liveTokensRef.current,
    runLogTail: runLogRef.current,
    tick,
  };
}

function newDerived(): RunDerived {
  return {
    runId: null,
    seed: null,
    runStartTs: null,
    runEndTs: null,
    endReason: null,
    graph: null,
    nodes: new Map(),
    warnings: [],
    eventCount: 0,
  };
}

/** Visual scheduling state for a node, folding trace-derived outcome into
 * the status snapshot's lifecycle state. */
export type VisualState =
  | "running"
  | "ready"
  | "paused"
  | "error"
  | "completed"
  | "idle";

export function visualState(
  statusState: string | undefined,
  rt: NodeRuntime | undefined,
): VisualState {
  if (statusState === "running") return "running";
  if (statusState === "ready") return "ready";
  if (statusState === "paused") return "paused";
  if (rt?.lastError) return "error";
  if (rt && rt.commits > 0) return "completed";
  return "idle";
}

/** Live tokens for a node's currently-open invocation only — committed
 * invocations are already counted via trace commit usage. */
export function nodeLiveTokens(
  live: Map<string, number>,
  nodeId: string,
  statusNode: { state: string; invocations: number } | undefined,
): number {
  if (!statusNode) return 0;
  if (statusNode.state !== "running" && statusNode.state !== "paused") return 0;
  return live.get(`${nodeId}#${statusNode.invocations}`) ?? 0;
}

export function totalTokens(
  d: RunDerived,
  live: Map<string, number>,
  statusNodes: Record<string, { state: string; invocations: number }> | undefined,
): number {
  let total = 0;
  for (const [id, rt] of d.nodes.entries()) {
    total += rt.tokensCommitted;
    total += nodeLiveTokens(live, id, statusNodes?.[id]);
  }
  return total;
}

export { GEN };
