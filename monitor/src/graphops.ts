// Build-mode contract manipulation. Pure functions over the GraphModel; the
// Python backend re-derives authoritative semantics on save/validate.

import {
  DEFAULT_LABEL_TEMPLATE,
  GEN,
  modelAlias,
  type EdgeSpec,
  type GateSpec,
  type GraphModel,
  type NodeSpec,
  type NodeType,
} from "./types";

export const DEFAULT_GATE: GateSpec = { expression: "True" };

export function isDefaultGate(g: GateSpec | undefined | null): boolean {
  if (!g) return true;
  return (g.expression ?? "True").trim() === "True" && g.p_invocation == null;
}

function numberedId(base: string, taken: Set<string>): string {
  for (let i = 1; ; i++) if (!taken.has(`${base}${i}`)) return `${base}${i}`;
}

/** Per-subtype id prefixes. Every subtype must map to a distinct prefix —
 * never derive from the provider name's first letter (claude/codex collide). */
const LLM_ID_PREFIX: Record<string, string> = {
  claude: "LCl", claudeb: "LClb", codex: "LCo", codexb: "LCob", grok: "LGr",
};

function takenIds(g: GraphModel): Set<string> {
  return new Set([GEN, ...g.nodes.map((n) => n.id), ...g.edges.map((e) => e.id)]);
}

export function nextRank(g: GraphModel): number {
  return g.nodes.length ? Math.max(...g.nodes.map((n) => n.rank)) + 1 : 0;
}

/** Default label for a freshly created node, from the graph's label template
 * (%provider/%model substitution), uniquified with a 1, 2, … suffix. Labels
 * are assigned once at creation and never recomputed — later rank, model, or
 * template changes leave existing labels alone. */
export function defaultLabel(g: GraphModel, node: NodeSpec): string {
  const template = (g.label_template ?? "").trim() || DEFAULT_LABEL_TEMPLATE;
  const provider = node.type === "llm" ? node.provider ?? "llm" : node.type;
  const model =
    node.type === "llm" ? modelAlias(node.model, node.provider).toLowerCase() : "";
  let base = template.replace(/%provider/g, provider).replace(/%model/g, model);
  // Empty substitutions leave dangling separators ("script_"): collapse them.
  base = base.replace(/[_\-.\s]{2,}/g, (m) => m[0]).replace(/^[_\-.\s]+|[_\-.\s]+$/g, "");
  if (!base) base = node.type;
  const taken = new Set<string>();
  for (const n of g.nodes) {
    taken.add(n.id);
    const lab = (n.label ?? "").trim();
    if (lab) taken.add(lab);
  }
  if (!taken.has(base)) return base;
  for (let i = 1; ; i++) if (!taken.has(`${base}${i}`)) return `${base}${i}`;
}

export function makeNode(g: GraphModel, type: NodeType, provider?: string): NodeSpec {
  const taken = takenIds(g);
  const p = provider ?? "claude";
  const base =
    type === "script"
      ? "Scr"
      : type === "human"
        ? "Hum"
        : LLM_ID_PREFIX[p] ?? "L" + p[0].toUpperCase() + p.slice(1, 2).toLowerCase();
  const id = numberedId(base, taken);
  const common = {
    id,
    rank: nextRank(g),
    inputs: {},
    outputs: {},
    gate: null,
    output_expression: null,
  };
  const node: NodeSpec =
    type === "script"
      ? {
          ...common,
          type,
          code: `def ${id}_fn(inputs):\n    return {}\n`,
        }
      : type === "human"
        ? {
            ...common,
            type,
            prompt: "Review and respond.",
            outputs: { approved: "bool" },
            timeout_s: null,
          }
        : {
            ...common,
            type: "llm",
            provider: p,
            model: p.startsWith("codex") ? "gpt-5.6-luna" : p === "grok" ? "grok-4.6" : "sonnet",
            effort: "medium",
            prompt: "",
            outputs: { response: "str" },
            extra_args: [],
            timeout_s: 1800,
          };
  return { ...node, label: defaultLabel(g, node) };
}

export function makeEdge(
  g: GraphModel,
  inputs: string[] = [],
  outputs: string[] = [],
): EdgeSpec {
  const taken = takenIds(g);
  return normalizeEdgeGates({
    id: numberedId("e", taken),
    inputs,
    outputs,
    universal_gate: { ...DEFAULT_GATE },
    input_gates: {},
    output_gates: {},
  });
}

export function replaceNode(g: GraphModel, id: string, node: NodeSpec): GraphModel {
  const renamed = node.id !== id;
  return {
    ...g,
    nodes: g.nodes.map((n) => {
      const updated = n.id === id ? node : n;
      return renamed && updated.persist_from === id
        ? { ...updated, persist_from: node.id }
        : updated;
    }),
    edges: renamed
      ? g.edges.map((e) => ({
          ...e,
          inputs: e.inputs.map((x) => (x === id ? node.id : x)),
          outputs: e.outputs.map((x) => (x === id ? node.id : x)),
          input_gates: renameKey(e.input_gates, id, node.id),
          output_gates: renameKey(e.output_gates, id, node.id),
        }))
      : g.edges,
  };
}

function renameKey(gates: Record<string, GateSpec>, from: string, to: string) {
  if (!(from in gates)) return gates;
  const out: Record<string, GateSpec> = {};
  for (const [k, v] of Object.entries(gates)) out[k === from ? to : k] = v;
  return out;
}

export function deleteNode(g: GraphModel, id: string): GraphModel {
  return {
    ...g,
    nodes: g.nodes.filter((n) => n.id !== id),
    edges: g.edges
      .map((e) => ({
        ...e,
        inputs: e.inputs.filter((x) => x !== id),
        outputs: e.outputs.filter((x) => x !== id),
        input_gates: omitKey(e.input_gates, id),
        output_gates: omitKey(e.output_gates, id),
      }))
      .filter((e) => e.inputs.length > 0 && e.outputs.length > 0),
  };
}

function omitKey(gates: Record<string, GateSpec>, key: string) {
  if (!(key in gates)) return gates;
  const out = { ...gates };
  delete out[key];
  return out;
}

export function replaceEdge(g: GraphModel, id: string, edge: EdgeSpec): GraphModel {
  return { ...g, edges: g.edges.map((e) => (e.id === id ? edge : e)) };
}

export function deleteEdge(g: GraphModel, id: string): GraphModel {
  return { ...g, edges: g.edges.filter((e) => e.id !== id) };
}

export function addNode(g: GraphModel, node: NodeSpec): GraphModel {
  return { ...g, nodes: [...g.nodes, node] };
}

export function addEdge(g: GraphModel, edge: EdgeSpec): GraphModel {
  return { ...g, edges: [...g.edges, edge] };
}

/** Keep an edge's gate maps aligned with its endpoint sets. */
export function normalizeEdgeGates(e: EdgeSpec): EdgeSpec {
  const input_gates: Record<string, GateSpec> = {};
  for (const id of e.inputs) input_gates[id] = e.input_gates[id] ?? { ...DEFAULT_GATE };
  const output_gates: Record<string, GateSpec> = {};
  for (const id of e.outputs) output_gates[id] = e.output_gates[id] ?? { ...DEFAULT_GATE };
  return { ...e, input_gates, output_gates };
}
