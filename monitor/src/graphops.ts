// Build-mode contract manipulation. Pure functions over the GraphModel; the
// Python backend re-derives authoritative semantics on save/validate.

import {
  GEN,
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

function uniqueId(base: string, taken: Set<string>): string {
  if (!taken.has(base)) return base;
  for (let i = 2; ; i++) if (!taken.has(`${base}${i}`)) return `${base}${i}`;
}

function takenIds(g: GraphModel): Set<string> {
  return new Set([GEN, ...g.nodes.map((n) => n.id), ...g.edges.map((e) => e.id)]);
}

export function nextRank(g: GraphModel): number {
  return g.nodes.length ? Math.max(...g.nodes.map((n) => n.rank)) + 1 : 0;
}

export function makeNode(g: GraphModel, type: NodeType, provider?: string): NodeSpec {
  const taken = takenIds(g);
  const base =
    type === "script" ? "S" : type === "human" ? "H" : provider ? provider[0].toUpperCase() : "L";
  const id = uniqueId(base, taken);
  const common = {
    id,
    rank: nextRank(g),
    inputs: {},
    outputs: {},
    gate: null,
    output_expression: null,
  };
  if (type === "script")
    return {
      ...common,
      type,
      code: `def ${id}_fn(inputs):\n    return {}\n`,
    };
  if (type === "human")
    return {
      ...common,
      type,
      prompt: "Review and respond.",
      outputs: { approved: "bool" },
      timeout_s: null,
    };
  return {
    ...common,
    type: "llm",
    provider: provider ?? "claude",
    model:
      provider === "codex" ? "gpt-5.6-luna" : provider === "grok" ? "grok-4.5" : "sonnet",
    effort: "medium",
    prompt: "",
    outputs: { response: "str" },
    extra_args: [],
    timeout_s: 1800,
  };
}

export function makeEdge(g: GraphModel): EdgeSpec {
  const taken = takenIds(g);
  return {
    id: uniqueId("e1", taken),
    inputs: [],
    outputs: [],
    universal_gate: { ...DEFAULT_GATE },
    input_gates: {},
    output_gates: {},
  };
}

export function replaceNode(g: GraphModel, id: string, node: NodeSpec): GraphModel {
  const renamed = node.id !== id;
  return {
    ...g,
    nodes: g.nodes.map((n) => (n.id === id ? node : n)),
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
