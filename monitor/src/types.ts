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
export type Provider = "claude" | "claudeb" | "codex" | "codexb" | "grok" | "openrouter";
/** Every provider the runtime knows, in dropdown order. The `b` variants run
 * the same CLI on its second config dir (~/.claudeb, ~/.codexb) — a second
 * login, i.e. a second rate limit — and take that CLI's models. `openrouter`
 * runs OpenRouter API models through the local DSH CLI (`dsh -p`).
 *
 * Menus do not list this directly: they list `Contract.providers`, the subset
 * whose config dir exists on this machine (via `providerOptions`). This full
 * list is only the fallback while no contract is loaded. */
export const PROVIDERS: Provider[] = ["claude", "claudeb", "codex", "codexb", "grok", "openrouter"];

/** Provider names a menu should offer: the providers available on this
 * machine (the backend's list; all known providers when none is loaded) plus
 * any `current` values that are not among them, so a node or semaphore bound
 * to a login that has since been deleted stays visible instead of silently
 * displaying as something else. */
export function providerOptions(
  available: readonly string[] | null | undefined,
  ...current: (string | null | undefined)[]
): string[] {
  const out = [...(available ?? PROVIDERS)];
  for (const c of current) if (c && !out.includes(c)) out.push(c);
  return out;
}

/** Display names for provider buttons and menu entries. */
export const PROVIDER_LABELS: Record<string, string> = {
  claude: "Claude",
  claudeb: "Claude B",
  codex: "Codex",
  codexb: "Codex B",
  grok: "Grok",
  openrouter: "OpenRouter",
};

/** Providers that get a one-click "+ <Provider>" affordance (sidebar buttons
 * and the canvas "New node" submenu): each CLI's default login. Second logins
 * are reached through the node dialog's Provider menu. */
const QUICK_ADD_PROVIDERS: Provider[] = ["claude", "codex", "grok", "openrouter"];

/** Quick-add entries for the providers available on this machine, in display
 * order. With no contract loaded every quick-add provider is offered; with
 * one, only logins whose config dir exists — so a machine without `grok`
 * never shows a "+ Grok" button. */
export function quickAddProviders(
  available: readonly string[] | null | undefined,
): Array<{ kind: string; label: string }> {
  return QUICK_ADD_PROVIDERS
    .filter((p) => !available || available.includes(p))
    .map((p) => ({ kind: p, label: PROVIDER_LABELS[p] ?? p }));
}

export interface NodeSpec {
  id: string;
  rank: number;
  type: NodeType;
  /** GUI-owned display name; the runtime only ever sees the node id. */
  label?: string | null;
  /**
   * Manual canvas position (Shift+drag); unset = automatic. Session-only
   * view state, overlaid onto the canvas model just before render — never
   * present on the contract and never persisted.
   */
  pos?: { x: number; y: number } | null;
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
  persist_from?: string | null;
  parse_ref?: string | null;
  parse_opaque?: string;
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
  /** Absolute run directory the definition declares; null = runtime picks %TEMP%/<hash>. */
  run_dir: string | null;
  /** Run seed the definition declares; null = the runtime draws one per run. */
  seed: number | null;
  /** Monitor GUI launch policy the definition declares: true = launch the
   * default exe, false = never, a string = that exe, null = auto (default
   * exe when installed and the console is interactive). */
  monitor: boolean | string | null;
  nodes: NodeSpec[];
  edges: EdgeSpec[];
  /** Template for freshly created nodes' default labels (GUI-owned). */
  label_template?: string | null;
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
  /** Providers whose login (config dir) exists on this machine, sorted;
   * computed by the backend on every load/save/template. */
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
  /** Context window of the last committed invocation's final model call. */
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

export interface ModelOption {
  alias: string;
  id: string;
  /** Efforts the CLI accepts for this model; empty = no effort dial. */
  efforts: string[];
}

const BASE_EFFORTS = ["low", "medium", "high", "xhigh", "max"];
const CLAUDE_EFFORTS = [...BASE_EFFORTS, "ultracode"];
const CODEX_ULTRA_EFFORTS = [...BASE_EFFORTS, "ultra"];
const GROK_EFFORTS = ["low", "medium", "high", "xhigh"];

/** Friendly aliases shown in the Model dropdown, with each model's accepted
 * efforts. The graph definition and the provider CLI flag always carry the
 * model id, never the display alias. Mirrors the model table in
 * guide\authoring.md. */
const CLAUDE_MODELS: ModelOption[] = [
  { alias: "Fable", id: "fable", efforts: CLAUDE_EFFORTS },
  { alias: "Opus", id: "opus", efforts: CLAUDE_EFFORTS },
  { alias: "Sonnet", id: "sonnet", efforts: CLAUDE_EFFORTS },
  { alias: "Haiku", id: "haiku", efforts: [] },
];
const CODEX_MODELS: ModelOption[] = [
  { alias: "Luna", id: "gpt-6-luna", efforts: BASE_EFFORTS },
  { alias: "Terra", id: "gpt-5.6-terra", efforts: CODEX_ULTRA_EFFORTS },
  { alias: "Sol", id: "gpt-6.1-sol", efforts: CODEX_ULTRA_EFFORTS },
  { alias: "Astra", id: "gpt-6-astra", efforts: CODEX_ULTRA_EFFORTS },
];
export const PROVIDER_MODELS: Record<string, ModelOption[]> = {
  claude: CLAUDE_MODELS,
  claudeb: CLAUDE_MODELS,
  codex: CODEX_MODELS,
  codexb: CODEX_MODELS,
  grok: [
    { alias: "Grok 4.6", id: "grok-4.6", efforts: GROK_EFFORTS },
  ],
  // The adapter expands these short aliases to their OpenRouter ids
  // (`meta/muse-spark-1.3-contributor`, `meta/muse-wonder`); other ids typed
  // into the Model field pass through to DSH untouched.
  openrouter: [
    { alias: "Muse Spark 1.3", id: "muse-spark-1.3", efforts: BASE_EFFORTS },
    { alias: "Muse Wonder", id: "muse-wonder", efforts: BASE_EFFORTS },
  ],
};

/** Efforts offered for a provider/model pair: the model's own list when the
 * model is known; the union over the provider's models for a custom id; the
 * base scale for an unknown provider. */
export function effortsFor(
  provider: string | null | undefined,
  model: string | null | undefined,
): string[] {
  const list = PROVIDER_MODELS[provider ?? ""] ?? [];
  const hit = list.find((m) => m.id === model);
  if (hit) return hit.efforts;
  if (!list.length) return BASE_EFFORTS;
  const union: string[] = [];
  for (const m of list) for (const e of m.efforts) if (!union.includes(e)) union.push(e);
  return union;
}

/** The effort to keep after a provider or model change: unchanged when the
 * new pair accepts it; otherwise the nearest lower tier on the base scale,
 * else the highest tier offered; null when the model has no dial. */
export function coerceEffort(
  provider: string | null | undefined,
  model: string | null | undefined,
  effort: string | null | undefined,
): string | null {
  if (!effort) return null;
  const allowed = effortsFor(provider, model);
  if (allowed.includes(effort)) return effort;
  if (!allowed.length) return null;
  for (let i = BASE_EFFORTS.indexOf(effort); i >= 0; i--) {
    if (allowed.includes(BASE_EFFORTS[i])) return BASE_EFFORTS[i];
  }
  return allowed[allowed.length - 1];
}

/** %provider/%model substitution source for default node labels. */
export const DEFAULT_LABEL_TEMPLATE = "%provider_%model";

/** Friendly alias for a model id (canvas display only — the definition and
 * the CLI always carry the id). Falls back to the id itself. */
export function modelAlias(model: string | null | undefined, provider?: string): string {
  if (!model) return "";
  const lists = provider && PROVIDER_MODELS[provider]
    ? [PROVIDER_MODELS[provider]]
    : Object.values(PROVIDER_MODELS);
  for (const list of lists) {
    const hit = list.find((m) => m.id === model);
    if (hit) return hit.alias;
  }
  return model;
}

/** Display name for a node: its label when set, otherwise its id. */
export function nodeLabel(n: Pick<NodeSpec, "id" | "label">): string {
  return (n.label ?? "").trim() || n.id;
}

export function nodeFill(node: Pick<NodeSpec, "type" | "provider">): string {
  if (node.type === "script") return "var(--fill-script)";
  if (node.type === "human") return "var(--fill-human)";
  switch (node.provider) {
    case "claude":
    case "claudeb":
      return "var(--fill-claude)";
    case "codex":
    case "codexb":
      return "var(--fill-codex)";
    case "grok":
      return "var(--fill-grok)";
    case "openrouter":
      return "var(--fill-openrouter)";
    default:
      return "var(--fill-script)";
  }
}

function num(u: Record<string, unknown>, k: string): number {
  return typeof u[k] === "number" ? (u[k] as number) : 0;
}

/** Tokens in one model call's context window (what Claude Code's footer
 * shows): fresh + cached input plus the reply. Handles the claude usage
 * shape, codex's `totalTokens`, DSH's camelCase pair, and OpenAI-style
 * `prompt_tokens`/`completion_tokens`. A call's snapshot, never a sum
 * across calls. */
export function contextTokens(usage: unknown): number {
  if (!usage || typeof usage !== "object") return 0;
  const u = usage as Record<string, unknown>;
  if (typeof u.totalTokens === "number") return u.totalTokens;
  if (typeof u.total_tokens === "number") return u.total_tokens;
  const claude =
    num(u, "input_tokens") +
    num(u, "cache_creation_input_tokens") +
    num(u, "cache_read_input_tokens") +
    num(u, "output_tokens");
  if (claude) return claude;
  const camel = num(u, "inputTokens") + num(u, "outputTokens");
  if (camel) return camel;
  return num(u, "prompt_tokens") + num(u, "completion_tokens");
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
