// Automatic graph layout: layered, bottom-to-top (the Generator at the
// bottom, primary flow upward). Cyclic graphs keep the dominant orientation
// with return edges routed against it around the side. A node the user has
// moved by hand (Shift+drag, build mode) carries a stored position that
// overrides its automatic slot; everything else stays automatic.

import { GEN, type EdgeSpec, type GraphModel } from "./types";

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export const NODE_W = 184;
export const NODE_H = 66;
export const GEN_W = 96;
export const GEN_H = 38;
const GAP_X = 56;
const GAP_Y = 118;

export interface Layout {
  rects: Map<string, Rect>;
  bbox: Rect;
  maxLayer: number;
}

interface Pair {
  from: string;
  to: string;
}

function pairsOf(edges: EdgeSpec[]): Pair[] {
  const pairs: Pair[] = [];
  for (const e of edges)
    for (const i of e.inputs) for (const o of e.outputs) pairs.push({ from: i, to: o });
  return pairs;
}

/** DFS back-edge marking over the pair graph, so cycles do not break the
 * layering: a pair closing a cycle is ignored for layer assignment. */
function backPairs(ids: string[], pairs: Pair[]): Set<Pair> {
  const out = new Map<string, Pair[]>();
  for (const id of ids) out.set(id, []);
  for (const p of pairs) out.get(p.from)?.push(p);
  const back = new Set<Pair>();
  const state = new Map<string, 0 | 1 | 2>(); // unvisited | on-stack | done
  const visit = (start: string) => {
    const stack: { id: string; next: number }[] = [{ id: start, next: 0 }];
    state.set(start, 1);
    while (stack.length) {
      const frame = stack[stack.length - 1];
      const succ = out.get(frame.id) ?? [];
      if (frame.next < succ.length) {
        const pair = succ[frame.next++];
        const s = state.get(pair.to) ?? 0;
        if (s === 1) back.add(pair);
        else if (s === 0) {
          state.set(pair.to, 1);
          stack.push({ id: pair.to, next: 0 });
        }
      } else {
        state.set(frame.id, 2);
        stack.pop();
      }
    }
  };
  visit(GEN);
  for (const id of ids) if ((state.get(id) ?? 0) === 0) visit(id);
  return back;
}

export function computeLayout(model: GraphModel): Layout {
  const ids = [GEN, ...model.nodes.map((n) => n.id)];
  const pairs = pairsOf(model.edges).filter(
    (p) => ids.includes(p.from) && ids.includes(p.to) && p.to !== GEN,
  );
  const back = backPairs(ids, pairs);
  const fwd = pairs.filter((p) => !back.has(p) && p.from !== p.to);

  // Longest-path layering over the acyclic remainder, GEN pinned to 0.
  const layer = new Map<string, number>();
  layer.set(GEN, 0);
  const inbound = new Map<string, Pair[]>();
  for (const id of ids) inbound.set(id, []);
  for (const p of fwd) inbound.get(p.to)?.push(p);
  const indeg = new Map<string, number>();
  for (const id of ids) indeg.set(id, (inbound.get(id) ?? []).length);
  const queue = ids.filter((id) => (indeg.get(id) ?? 0) === 0);
  const outFwd = new Map<string, Pair[]>();
  for (const id of ids) outFwd.set(id, []);
  for (const p of fwd) outFwd.get(p.from)?.push(p);
  while (queue.length) {
    const id = queue.shift()!;
    if (!layer.has(id)) layer.set(id, 1); // roots other than GEN start above it
    for (const p of outFwd.get(id) ?? []) {
      const proposed = (layer.get(id) ?? 1) + 1;
      if ((layer.get(p.to) ?? 0) < proposed) layer.set(p.to, proposed);
      indeg.set(p.to, (indeg.get(p.to) ?? 1) - 1);
      if ((indeg.get(p.to) ?? 0) === 0) queue.push(p.to);
    }
  }
  for (const id of ids) if (!layer.has(id)) layer.set(id, 1);

  const maxLayer = Math.max(...ids.map((id) => layer.get(id) ?? 0));
  const layers: string[][] = Array.from({ length: maxLayer + 1 }, () => []);
  for (const id of ids) layers[layer.get(id) ?? 0].push(id);

  // Within-layer order: rank first, then two barycenter sweeps against the
  // layer below to reduce crossings.
  const rankOf = new Map(model.nodes.map((n) => [n.id, n.rank]));
  for (const l of layers) l.sort((a, b) => (rankOf.get(a) ?? 0) - (rankOf.get(b) ?? 0));
  const preds = new Map<string, string[]>();
  for (const id of ids) preds.set(id, []);
  for (const p of fwd) preds.get(p.to)?.push(p.from);
  for (let sweep = 0; sweep < 2; sweep++) {
    for (let li = 1; li <= maxLayer; li++) {
      const below = new Map(layers[li - 1].map((id, i) => [id, i]));
      layers[li].sort((a, b) => {
        const bary = (id: string) => {
          const ps = (preds.get(id) ?? []).filter((p) => below.has(p));
          if (!ps.length) return Number.MAX_SAFE_INTEGER; // keep rank order
          return ps.reduce((s, p) => s + (below.get(p) ?? 0), 0) / ps.length;
        };
        const d = bary(a) - bary(b);
        return d !== 0 ? d : (rankOf.get(a) ?? 0) - (rankOf.get(b) ?? 0);
      });
    }
  }

  const rects = new Map<string, Rect>();
  for (let li = 0; li <= maxLayer; li++) {
    const row = layers[li];
    const widths = row.map((id) => (id === GEN ? GEN_W : NODE_W));
    const total = widths.reduce((a, b) => a + b, 0) + GAP_X * Math.max(0, row.length - 1);
    let x = -total / 2;
    const y = (maxLayer - li) * (NODE_H + GAP_Y);
    for (let i = 0; i < row.length; i++) {
      const id = row[i];
      const w = widths[i];
      const h = id === GEN ? GEN_H : NODE_H;
      rects.set(id, { x, y: y + (NODE_H - h) / 2, w, h });
      x += w + GAP_X;
    }
  }

  // Manual overrides win over the automatic slots (and count toward the
  // bbox, so fit-to-view and return-edge routing follow moved nodes).
  for (const n of model.nodes) {
    const p = n.pos;
    if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) {
      const r = rects.get(n.id);
      if (r) rects.set(n.id, { ...r, x: p.x, y: p.y });
    }
  }

  let minX = Infinity,
    minY = Infinity,
    maxX = -Infinity,
    maxY = -Infinity;
  for (const r of rects.values()) {
    minX = Math.min(minX, r.x);
    minY = Math.min(minY, r.y);
    maxX = Math.max(maxX, r.x + r.w);
    maxY = Math.max(maxY, r.y + r.h);
  }
  if (!rects.size) {
    minX = minY = 0;
    maxX = maxY = 1;
  }
  return {
    rects,
    bbox: { x: minX, y: minY, w: maxX - minX, h: maxY - minY },
    maxLayer,
  };
}

/** Slide a dragged rect out of every obstacle it overlaps: push along the
 * axis of least penetration, iterating a few times so corners and clusters
 * resolve; null when no overlap-free spot emerges (caller keeps the last
 * good position). */
export function resolveOverlap(
  desired: Rect,
  obstacles: Rect[],
  margin = 8,
): Rect | null {
  const r = { ...desired };
  for (let iter = 0; iter < 8; iter++) {
    let hit = false;
    for (const o of obstacles) {
      const x1 = o.x - margin;
      const y1 = o.y - margin;
      const x2 = o.x + o.w + margin;
      const y2 = o.y + o.h + margin;
      const pushRight = x2 - r.x;
      const pushLeft = r.x + r.w - x1;
      const pushDown = y2 - r.y;
      const pushUp = r.y + r.h - y1;
      if (pushRight <= 0 || pushLeft <= 0 || pushDown <= 0 || pushUp <= 0) continue;
      hit = true;
      const m = Math.min(pushRight, pushLeft, pushDown, pushUp);
      if (m === pushRight) r.x = x2;
      else if (m === pushLeft) r.x = x1 - r.w;
      else if (m === pushDown) r.y = y2;
      else r.y = y1 - r.h;
    }
    if (!hit) return r;
  }
  return null;
}

// ---------------------------------------------------------------------- //
// Edge geometry
// ---------------------------------------------------------------------- //

export interface Segment {
  d: string;
  /** Points along the path for arrow placement: [point, tangent-angle-deg]. */
  arrows: { x: number; y: number; angle: number }[];
  golden: boolean;
}

export interface EdgeGeometry {
  id: string;
  segments: Segment[];
  junction: { x: number; y: number } | null;
  /** A representative point for labeling/selection affordances. */
  mid: { x: number; y: number };
}

function cubic(
  p0: { x: number; y: number },
  p1: { x: number; y: number },
  p2: { x: number; y: number },
  p3: { x: number; y: number },
) {
  const at = (t: number) => {
    const u = 1 - t;
    return {
      x: u * u * u * p0.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t * t * t * p3.x,
      y: u * u * u * p0.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t * t * t * p3.y,
    };
  };
  const tangent = (t: number) => {
    const u = 1 - t;
    const dx =
      3 * u * u * (p1.x - p0.x) + 6 * u * t * (p2.x - p1.x) + 3 * t * t * (p3.x - p2.x);
    const dy =
      3 * u * u * (p1.y - p0.y) + 6 * u * t * (p2.y - p1.y) + 3 * t * t * (p3.y - p2.y);
    return (Math.atan2(dy, dx) * 180) / Math.PI;
  };
  return {
    d: `M ${p0.x} ${p0.y} C ${p1.x} ${p1.y}, ${p2.x} ${p2.y}, ${p3.x} ${p3.y}`,
    at,
    tangent,
  };
}

function segment(
  from: { x: number; y: number },
  to: { x: number; y: number },
  golden: boolean,
  arrowTs: number[],
  bboxHalfW: number,
): Segment {
  const forward = to.y < from.y - 4; // upward is forward flow
  let c1, c2;
  if (forward || Math.abs(to.y - from.y) <= 4) {
    const dy = Math.max(30, Math.abs(to.y - from.y) * 0.45);
    c1 = { x: from.x, y: from.y - dy };
    c2 = { x: to.x, y: to.y + dy };
  } else {
    // Return edge: route around the side, against the dominant orientation.
    const side = (from.x + to.x) / 2 >= 0 ? 1 : -1;
    const bulge = side * (bboxHalfW + 130) - (from.x + to.x) / 2;
    c1 = { x: from.x + bulge, y: from.y + 30 };
    c2 = { x: to.x + bulge, y: to.y - 30 };
  }
  const c = cubic(from, c1, c2, to);
  return {
    d: c.d,
    arrows: arrowTs.map((t) => ({ ...c.at(t), angle: c.tangent(t) })),
    golden,
  };
}

function isDefaultGate(g: { expression: string; p_invocation?: number | null } | undefined): boolean {
  if (!g) return true;
  return (g.expression ?? "True").trim() === "True" && g.p_invocation == null;
}

export function edgeGeometry(edge: EdgeSpec, rects: Map<string, Rect>, bbox: Rect): EdgeGeometry | null {
  const srcRects = edge.inputs.map((id) => rects.get(id)).filter((r): r is Rect => !!r);
  const dstRects = edge.outputs.map((id) => rects.get(id)).filter((r): r is Rect => !!r);
  if (!srcRects.length || !dstRects.length) return null;
  const tops = edge.inputs
    .map((id) => ({ id, r: rects.get(id) }))
    .filter((x): x is { id: string; r: Rect } => !!x.r)
    .map(({ id, r }) => ({ id, x: r.x + r.w / 2, y: r.y }));
  const bottoms = edge.outputs
    .map((id) => ({ id, r: rects.get(id) }))
    .filter((x): x is { id: string; r: Rect } => !!x.r)
    .map(({ id, r }) => ({ id, x: r.x + r.w / 2, y: r.y + r.h }));

  const uniDefault = isDefaultGate(edge.universal_gate);
  const halfW = bbox.w / 2;
  const segments: Segment[] = [];

  if (tops.length === 1 && bottoms.length === 1) {
    const golden = !(
      uniDefault &&
      isDefaultGate(edge.input_gates[tops[0].id]) &&
      isDefaultGate(edge.output_gates[bottoms[0].id])
    );
    const seg = segment(tops[0], bottoms[0], golden, [0.5, 0.92], halfW);
    return {
      id: edge.id,
      segments: [seg],
      junction: null,
      mid: seg.arrows[0] ?? tops[0],
    };
  }

  // Convergent/divergent: shared trunk through one junction — still one
  // logical, selectable edge.
  const xs = [...tops, ...bottoms].map((p) => p.x);
  const jx = xs.reduce((a, b) => a + b, 0) / xs.length;
  const srcY = Math.min(...tops.map((p) => p.y));
  const dstY = Math.max(...bottoms.map((p) => p.y));
  const jy = (srcY + dstY) / 2;
  const j = { x: jx, y: jy };
  for (const t of tops) {
    const golden = !(uniDefault && isDefaultGate(edge.input_gates[t.id]));
    segments.push(segment(t, j, golden, [0.55], halfW));
  }
  for (const b of bottoms) {
    const golden = !(uniDefault && isDefaultGate(edge.output_gates[b.id]));
    segments.push(segment(j, b, golden, [0.55, 0.9], halfW));
  }
  return { id: edge.id, segments, junction: j, mid: j };
}
