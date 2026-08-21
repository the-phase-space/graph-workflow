// Shared types across the Monitor frontend: the structured Python/JSON
// contract (authoring), the run-dir interchange surface (status/trace), and
// derived per-node runtime state.

export const GEN = "GEN";

export interface GateSpec {
  expression: string;
  p_invocation?: number | null;
}

export interface EdgeSpec {
  id: string;
  inputs: string[];
  outputs: string[];
  universal_gate: GateSpec;
  input_gates: Record<string, GateSpec>;
  output_gates: Record<string, GateSpec>;
}

export type NodeType = "script" | "llm" | "human";
export type Provider = "claude" | "codex" | "grok";

export interface NodeSpec {
  id: string;
  rank: number;
  type: NodeType;
  inputs: Record<string, string>;
  outputs: Record<string, string>;
  gate: string | null;
  output_expression: string | null;
  // script
  code?: string | null;
  code_ref?: string | null;
  code_opaque?: string;
  // llm
  provider?: string;
  model?: string;
  effort?: string | null;
  prompt?: string | null;
  prompt_ref?: string | null;
  prompt_opaque?: boolean;
  settings?: string | null;
  cwd?: string | null;
  extra_args?: string[];
  timeout_s?: number | null;
  pause_message?: string | null;
  resume_message?: string | null;
  parse_ref?: string | null;
  // human
  shell_cmd?: string | null;
}

export interface Semaphores {
  script: number;
  llm: number;
  human: number;
  providers: Record<string, number>;
}

export interface GraphModel {
  name: string;
  semaphores: Semaphores;
  pause_message: string | null;
  resume_message: string | null;
  nodes: NodeSpec[];
  edges: EdgeSpec[];
}

export interface Contract {
  ok: boolean;
  version: number;
  path: string | null;
  structured: boolean;
  helpers: string;
  graph: GraphModel;
  errors: string[];
  warnings: string[];
  providers: string[];
}

// ---------------------------------------------------------------------- //
// Run-dir interchange
// ---------------------------------------------------------------------- //

export interface StatusNode {
  state: "idle" | "ready" | "running" | "paused";
  output_state: boolean | "UNSET";
  invocations: number;
  commits: number;
  metrics: Record<string, unknown>;
  outputs: Record<string, unknown>;
}

export interface StatusSnapshot {
  run_id: string;
  graph: string;
  seed: number;
  state: string; // running | pausing | paused | ended:<reason>
  generator: boolean;
  next_seq: number;
  nodes: Record<string, StatusNode>;
  ts: number;
}

export interface TraceEvent {
  seq: number;
  ts: number;
  event: string;
  node?: string;
  invocation?: number;
  outputs?: Record<string, unknown>;
  output_state?: unknown;
  metrics?: Record<string, unknown> & { usage?: Record<string, unknown> };
  error?: string | null;
  inputs?: Record<string, unknown>;
  message?: string;
  reason?: string;
  run_id?: string;
  seed?: number;
  graph?: unknown;
  resumed?: boolean;
  value?: boolean;
  terms?: Record<string, boolean>;
}

/** Per-node state derived from the trace (the status snapshot alone cannot
 * supply started-at timestamps, cumulative usage, or error history). */
export interface NodeRuntime {
  everRan: boolean;
  lastStartTs: number | null;
  lastError: string | null;
  commits: number;
  invocations: number;
  tokensCommitted: number;
  tokensLive: number;
  contextSize: number;
  sessionId: string | null;
  receivedInputs: Set<string>;
  writtenOutputs: Set<string>;
  outputs: Record<string, unknown>;
  outputState: unknown;
  durationS: number | null;
}

export interface RunDerived {
  runId: string | null;
  seed: number | null;
  runStartTs: number | null;
  runEndTs: number | null;
  endReason: string | null;
  graph: GraphModel | null;
  nodes: Map<string, NodeRuntime>;
  warnings: string[];
  eventCount: number;
}

export type ActivityKind =
  | "message"
  | "thought"
  | "tool"
  | "tool-result"
  | "info"
  | "error";

export interface ActivityItem {
  kind: ActivityKind;
  label: string;
  text: string;
  ts: number | null;
}

export interface Selection {
  kind: "node" | "edge" | "generator" | "graph";
  id: string;
}

export const PROVIDER_MODELS: Record<string, string[]> = {
  claude: ["fable", "opus", "sonnet", "haiku"],
  codex: ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"],
  grok: ["grok-4.5", "grok-4.6"],
};

export const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

export function nodeFill(node: Pick<NodeSpec, "type" | "provider">): string {
  if (node.type === "script") return "var(--fill-script)";
  if (node.type === "human") return "var(--fill-human)";
  switch (node.provider) {
    case "claude":
      return "var(--fill-claude)";
    case "codex":
      return "var(--fill-codex)";
    case "grok":
      return "var(--fill-grok)";
    default:
      return "var(--fill-script)";
  }
}

/** Sum every numeric usage entry whose key mentions tokens — mirrors the
 * runtime's best-effort context-size accounting. */
export function sumUsage(usage: unknown): number {
  if (!usage || typeof usage !== "object") return 0;
  let total = 0;
  for (const [k, v] of Object.entries(usage as Record<string, unknown>)) {
    if (typeof v === "number" && k.toLowerCase().includes("token")) total += v;
    else if (v && typeof v === "object") total += sumUsage(v);
  }
  return total;
}

export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(n >= 100_000 ? 0 : 1) + "k";
  return String(n);
}

export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
}
