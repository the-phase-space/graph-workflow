// The graph canvas: nearly the whole window. SVG rendering with mouse-drag
// panning, scroll-wheel (or Ctrl+plus/minus) zooming, and a fit-to-view
// control at the top right. The default viewport shows the whole graph.
// Layout is automatic; a pan drag may start anywhere, including on top of a
// node, edge, or the Generator. In build mode Shift+left-drag moves a node
// anywhere (nodes may never overlap), and a Reset-layout control clears
// every manual position.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  computeLayout,
  edgeGeometry,
  GEN_H,
  GEN_W,
  resolveOverlap,
  type EdgeGeometry,
  type NodeMode,
  type Rect,
} from "../layout";
import { nodeLiveTokens, visualState, type RunState, type VisualState } from "../runstate";
import {
  GEN,
  fmtDuration,
  fmtTokens,
  modelAlias,
  nodeFill,
  nodeLabel,
  type GraphModel,
  type NodeSpec,
  type Selection,
} from "../types";

// ---------------------------------------------------------------------- //
// Canvas text metrics: SVG <text> does not clip, so on-face strings are
// ellipsis-truncated against measured pixel widths (fonts match styles.css).
// ---------------------------------------------------------------------- //

const LABEL_FONT = '700 15px "Segoe UI", system-ui, sans-serif';
const SUB_FONT = "11.5px Consolas, monospace";
const ID_BADGE_FONT = "9.5px Consolas, monospace";

let measureCtx: CanvasRenderingContext2D | null = null;
const measureCache = new Map<string, number>();

function textWidth(text: string, font: string): number {
  const key = font + "|" + text;
  const hit = measureCache.get(key);
  if (hit != null) return hit;
  if (!measureCtx) measureCtx = document.createElement("canvas").getContext("2d");
  if (!measureCtx) return text.length * 8;
  measureCtx.font = font;
  const w = measureCtx.measureText(text).width;
  if (measureCache.size > 4000) measureCache.clear();
  measureCache.set(key, w);
  return w;
}

function fitText(text: string, maxPx: number, font: string): string {
  if (textWidth(text, font) <= maxPx) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (textWidth(text.slice(0, mid) + "…", font) <= maxPx) lo = mid;
    else hi = mid - 1;
  }
  return text.slice(0, lo) + "…";
}

const GLOW: Record<VisualState, string | null> = {
  running: "#f2c521",
  ready: "#3f8cff",
  paused: "#e8a33d",
  error: "#ff4136",
  completed: "#2ecc71",
  idle: null,
};

// Material Symbols "collapse_content" / "expand_content" (viewBox 0 -960 960 960).
const COLLAPSE_ICON = "M447-447v253h-73v-180H194v-73h253Zm140-320v180h180v73H514v-253h73Z";
const EXPAND_ICON = "M194-194v-253h73v180h180v73H194Zm500-320v-180H514v-73h253v253h-73Z";

/** Glow geometry per node mode: the compact bar gets a much tighter halo,
 * with the filter region widened relative to its narrow face. */
const GLOW_GEOMETRY: Record<
  NodeMode,
  { region: { x: string; y: string; w: string; h: string }; dilate: number; near: number; far: number }
> = {
  expanded: { region: { x: "-50%", y: "-130%", w: "200%", h: "360%" }, dilate: 1.5, near: 5.5, far: 14 },
  compact: { region: { x: "-90%", y: "-35%", w: "280%", h: "170%" }, dilate: 1, near: 2, far: 5 },
};

function glowId(mode: NodeMode, state: VisualState): string {
  return mode === "compact" ? `glow-compact-${state}` : `glow-${state}`;
}

/** Photoshop-style outer glow: the halo exists strictly outside the node's
 * silhouette — the blurred shape is composited `out` against the source
 * alpha, so a translucent node face is never tinted from behind. */
function OuterGlowFilter(props: { id: string; color: string; mode: NodeMode }) {
  const geo = GLOW_GEOMETRY[props.mode];
  return (
    <filter id={props.id} x={geo.region.x} y={geo.region.y} width={geo.region.w} height={geo.region.h}>
      {/* Harden the translucent face to a solid silhouette first, so the
          interior punch-out removes the halo completely. */}
      <feComponentTransfer in="SourceAlpha" result="solid">
        <feFuncA type="linear" slope="40" intercept="0" />
      </feComponentTransfer>
      <feMorphology in="solid" operator="dilate" radius={geo.dilate} result="dil" />
      <feGaussianBlur in="dil" stdDeviation={geo.near} result="blurNear" />
      <feComposite in="blurNear" in2="solid" operator="out" result="ringNear" />
      <feFlood floodColor={props.color} floodOpacity="0.95" result="colNear" />
      <feComposite in="colNear" in2="ringNear" operator="in" result="glowNear" />
      <feGaussianBlur in="dil" stdDeviation={geo.far} result="blurFar" />
      <feComposite in="blurFar" in2="solid" operator="out" result="ringFar" />
      <feFlood floodColor={props.color} floodOpacity="0.6" result="colFar" />
      <feComposite in="colFar" in2="ringFar" operator="in" result="glowFar" />
      <feMerge>
        <feMergeNode in="glowFar" />
        <feMergeNode in="glowNear" />
        <feMergeNode in="SourceGraphic" />
      </feMerge>
    </filter>
  );
}

interface View {
  tx: number;
  ty: number;
  scale: number;
}

// Node kinds offered by the right-click "New node" submenu; mirrors the
// Compose section of the sidebar.
const NODE_KINDS: Array<{ kind: string; label: string }> = [
  { kind: "script", label: "Script" },
  { kind: "human", label: "Human" },
  { kind: "claude", label: "Claude" },
  { kind: "codex", label: "Codex" },
  { kind: "grok", label: "Grok" },
];

export default function Canvas(props: {
  model: GraphModel;
  run: RunState | null;
  selection: Selection | null;
  onSelect: (s: Selection | null) => void;
  onAddNode?: (kind: string) => void;
  onAddEdge?: () => void;
  onAddEdgeBetween?: (from: string, to: string) => void;
  onSetLabel?: (id: string, label: string | null) => void;
  onSetPos?: (id: string, pos: { x: number; y: number }) => void;
  onResetLayout?: () => void;
}) {
  const {
    model,
    run,
    selection,
    onSelect,
    onAddNode,
    onAddEdge,
    onAddEdgeBetween,
    onSetLabel,
    onSetPos,
    onResetLayout,
  } = props;
  const holder = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [view, setView] = useState<View>({ tx: 0, ty: 0, scale: 1 });
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number } | null>(null);
  // Right-click menu on a node or the Generator ("Add edge to ▸").
  const [nodeMenu, setNodeMenu] = useState<{ x: number; y: number; id: string } | null>(null);
  // Dynamic edge connector ('e' on a selected node): a half-opacity edge
  // follows the cursor until a target node is clicked. Coordinates are in
  // graph space.
  const [link, setLink] = useState<{ from: string; cx: number; cy: number } | null>(null);
  // In-place label editor opened by double-clicking a node.
  const [labelEdit, setLabelEdit] = useState<{ id: string; value: string } | null>(null);
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null);
  // Shift+drag node movement (build mode): the grab offset keeps the cursor
  // anchored where it picked the node up.
  const nodeDrag = useRef<{ id: string; dx: number; dy: number } | null>(null);
  const fittedFor = useRef<string>("");
  // Node display mode; the toggle sits beside the fit-to-view control.
  const [nodeMode, setNodeMode] = useState<NodeMode>("expanded");
  const compact = nodeMode === "compact";

  const layout = useMemo(() => computeLayout(model, nodeMode), [model, nodeMode]);
  const geoms = useMemo(
    () =>
      model.edges
        .map((e) => edgeGeometry(e, layout.rects, layout.bbox))
        .filter((g): g is EdgeGeometry => !!g),
    [model, layout],
  );

  useLayoutEffect(() => {
    const el = holder.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      setSize({ w: el.clientWidth, h: el.clientHeight });
    });
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  const fit = useCallback(() => {
    const pad = 70;
    const b = layout.bbox;
    const scale = Math.min(
      1.35,
      Math.min((size.w - pad * 2) / Math.max(1, b.w), (size.h - pad * 2) / Math.max(1, b.h)),
    );
    // The Generator anchors the bottom of every graph, so when the graph is
    // shorter than the viewport, park its bottom near the viewport's bottom
    // edge — the spare room above is where the graph will grow.
    const tyCentered = size.h / 2 - (b.y + b.h / 2) * scale;
    const tyAnchored = size.h - 60 - (b.y + b.h) * scale;
    setView({
      tx: size.w / 2 - (b.x + b.w / 2) * scale,
      ty: Math.max(tyCentered, tyAnchored),
      scale,
    });
  }, [layout, size]);

  // The default viewport shows the whole graph: fit when a graph first
  // arrives (once the holder has real dimensions) or its topology changes
  // identity, or the node display mode flips (the bbox changes wholesale).
  // Never on geometry changes — the view must hold still through window and
  // inspector resizes; zoom belongs to the user alone.
  useEffect(() => {
    if (!size.w || !size.h) return;
    const key = `${model.name}|${model.nodes.length}|${model.edges.length}|${nodeMode}`;
    if (fittedFor.current !== key) {
      fittedFor.current = key;
      fit();
    }
  }, [model, size, fit, nodeMode]);

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    const rect = holder.current?.getBoundingClientRect();
    if (!rect) return;
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    setView((v) => {
      const factor = Math.exp(-e.deltaY * 0.0012);
      const scale = Math.min(4, Math.max(0.05, v.scale * factor));
      const k = scale / v.scale;
      return { scale, tx: mx - (mx - v.tx) * k, ty: my - (my - v.ty) * k };
    });
  };

  // Ctrl+plus / Ctrl+minus step the zoom about the viewport center (and are
  // intercepted before the webview can zoom its own page).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      if (e.key !== "+" && e.key !== "=" && e.key !== "-") return;
      e.preventDefault();
      const factor = e.key === "-" ? 1 / 1.2 : 1.2;
      setView((v) => {
        const scale = Math.min(4, Math.max(0.05, v.scale * factor));
        const k = scale / v.scale;
        const cx = size.w / 2;
        const cy = size.h / 2;
        return { scale, tx: cx - (cx - v.tx) * k, ty: cy - (cy - v.ty) * k };
      });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [size]);

  const moved = useRef(false);
  const onMouseDown = (e: React.MouseEvent) => {
    setCtxMenu(null);
    setNodeMenu(null);
    if (e.button !== 0) return;
    moved.current = false;
    drag.current = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty };
  };

  // Right-clicking empty canvas offers element creation in build mode; the
  // webview's own context menu is suppressed everywhere on the canvas.
  const onContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    if (link) {
      setLink(null);
      return;
    }
    if (!onAddNode) return;
    const rect = holder.current?.getBoundingClientRect();
    if (!rect) return;
    // Keep the menu (and room for its hover submenu) inside the canvas.
    const x = Math.min(e.clientX - rect.left, Math.max(0, rect.width - 290));
    const y = Math.min(e.clientY - rect.top, Math.max(0, rect.height - 100));
    setCtxMenu({ x, y });
  };

  // Right-clicking a node (or the Generator) offers edge creation from it.
  const onElementContextMenu = (e: React.MouseEvent, id: string) => {
    e.preventDefault();
    e.stopPropagation();
    setCtxMenu(null);
    if (link) {
      setLink(null);
      return;
    }
    if (!onAddEdgeBetween) return;
    const rect = holder.current?.getBoundingClientRect();
    if (!rect) return;
    const x = Math.min(e.clientX - rect.left, Math.max(0, rect.width - 300));
    const y = Math.min(e.clientY - rect.top, Math.max(0, rect.height - 90));
    setNodeMenu({ x, y, id });
  };

  // While the dynamic edge connector is live, the preview endpoint tracks
  // the cursor in graph coordinates.
  const onLinkMouseMove = (e: React.MouseEvent) => {
    const rect = holder.current?.getBoundingClientRect();
    if (!rect) return;
    const cx = (e.clientX - rect.left - view.tx) / view.scale;
    const cy = (e.clientY - rect.top - view.ty) / view.scale;
    setLink((l) => (l ? { ...l, cx, cy } : l));
  };

  // Clicking a node while linking completes the edge (clicking the source
  // node abandons it instead; the Generator can never be a target).
  const completeLink = (targetId: string) => {
    if (!link) return;
    if (link.from !== targetId) onAddEdgeBetween?.(link.from, targetId);
    setLink(null);
  };

  const menuRef = useRef<HTMLDivElement>(null);
  const nodeMenuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ctxMenu && !nodeMenu) return;
    const dismiss = () => {
      setCtxMenu(null);
      setNodeMenu(null);
    };
    const onDown = (e: MouseEvent) => {
      if (
        e.target instanceof Node &&
        (menuRef.current?.contains(e.target) || nodeMenuRef.current?.contains(e.target))
      )
        return;
      dismiss();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") dismiss();
    };
    window.addEventListener("mousedown", onDown, true);
    window.addEventListener("blur", dismiss);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown, true);
      window.removeEventListener("blur", dismiss);
      window.removeEventListener("keydown", onKey);
    };
  }, [ctxMenu, nodeMenu]);

  // 'e' with a node (or the Generator) selected starts the dynamic edge
  // connector — unless the keystroke belongs to a focused form field, where
  // 'e' must just type. Escape abandons an active connector.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setLink(null);
        return;
      }
      if (e.key !== "e" || e.ctrlKey || e.metaKey || e.altKey) return;
      if (!onAddEdgeBetween || !selection) return;
      if (selection.kind !== "node" && selection.kind !== "generator") return;
      const t = document.activeElement;
      if (
        t instanceof HTMLElement &&
        (t.tagName === "INPUT" ||
          t.tagName === "TEXTAREA" ||
          t.tagName === "SELECT" ||
          t.isContentEditable)
      )
        return;
      const from = selection.kind === "generator" ? GEN : selection.id;
      const r = layout.rects.get(from);
      if (!r) return;
      e.preventDefault();
      setLink({ from, cx: r.x + r.w / 2, cy: r.y - 70 });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onAddEdgeBetween, selection, layout]);

  // Abandon the connector or the label editor when its node leaves the model
  // (deletion, undo, load).
  useEffect(() => {
    if (link && !layout.rects.has(link.from)) setLink(null);
    if (labelEdit && !model.nodes.some((n) => n.id === labelEdit.id)) setLabelEdit(null);
  }, [layout, model, link, labelEdit]);
  // The global mouse handlers live in a mount-once effect, so current view,
  // layout, and callbacks are mirrored into refs.
  const viewRef = useRef(view);
  useEffect(() => {
    viewRef.current = view;
  }, [view]);
  const layoutRef = useRef(layout);
  useEffect(() => {
    layoutRef.current = layout;
  }, [layout]);
  const onSetPosRef = useRef(onSetPos);
  useEffect(() => {
    onSetPosRef.current = onSetPos;
  }, [onSetPos]);

  useEffect(() => {
    const move = (e: MouseEvent) => {
      const nd = nodeDrag.current;
      if (nd) {
        const rect = holder.current?.getBoundingClientRect();
        const setPos = onSetPosRef.current;
        const cur = layoutRef.current.rects.get(nd.id);
        if (!rect || !setPos || !cur) {
          nodeDrag.current = null;
          return;
        }
        moved.current = true;
        const v = viewRef.current;
        // Desired spot comes from the cursor plus the grab offset (never from
        // the node's current rect), so a blocked update cannot cause drift.
        const desired = {
          x: (e.clientX - rect.left - v.tx) / v.scale - nd.dx,
          y: (e.clientY - rect.top - v.ty) / v.scale - nd.dy,
          w: cur.w,
          h: cur.h,
        };
        const obstacles: Rect[] = [];
        for (const [id, r] of layoutRef.current.rects) if (id !== nd.id) obstacles.push(r);
        const spot = resolveOverlap(desired, obstacles);
        if (spot) setPos(nd.id, { x: spot.x, y: spot.y });
        return;
      }
      const d = drag.current;
      if (!d) return;
      if (Math.abs(e.clientX - d.x) + Math.abs(e.clientY - d.y) > 3) moved.current = true;
      setView((v) => ({ ...v, tx: d.tx + (e.clientX - d.x), ty: d.ty + (e.clientY - d.y) }));
    };
    const up = () => {
      drag.current = null;
      nodeDrag.current = null;
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, []);

  const now = Date.now() / 1000;

  // Selection changes only apply to genuine clicks: a pan drag that started
  // on top of an element still ends with that element's click event, and it
  // must not alter (or dismiss) the current selection.
  const guardedSelect = (s: Selection | null) => {
    if (moved.current) return;
    onSelect(s);
  };

  const commitLabelEdit = () => {
    if (!labelEdit) return;
    const node = model.nodes.find((n) => n.id === labelEdit.id);
    const next = labelEdit.value.trim() || null;
    if (node && next !== ((node.label ?? "").trim() || null)) onSetLabel?.(labelEdit.id, next);
    setLabelEdit(null);
  };

  // "Add edge to ▸" target list: every other node, sorted by rank.
  const edgeTargets = (from: string): NodeSpec[] =>
    model.nodes
      .filter((n) => n.id !== from)
      .sort((a, b) => a.rank - b.rank || a.id.localeCompare(b.id));

  const labelEditRect = labelEdit ? layout.rects.get(labelEdit.id) : undefined;

  return (
    <div
      ref={holder}
      className={"canvas-holder" + (link ? " linking" : "")}
      onWheel={(e) => {
        setCtxMenu(null);
        setNodeMenu(null);
        onWheel(e);
      }}
      onMouseDown={onMouseDown}
      onMouseMove={link ? onLinkMouseMove : undefined}
      onContextMenu={onContextMenu}
      onClick={() => {
        // Clicking empty canvas abandons an active edge connector, nothing
        // more: the selection (and with it the inspector) stays put until
        // another element is clicked or the panel is minimized explicitly.
        if (moved.current) return;
        if (link) setLink(null);
      }}
    >
      <svg width={size.w} height={size.h} className="canvas-svg">
        <defs>
          {(["expanded", "compact"] as NodeMode[]).flatMap((mode) =>
            Object.entries(GLOW).map(([state, color]) =>
              color ? (
                <OuterGlowFilter
                  key={`${mode}-${state}`}
                  id={glowId(mode, state as VisualState)}
                  color={color}
                  mode={mode}
                />
              ) : null,
            ),
          )}
        </defs>
        <g transform={`translate(${view.tx} ${view.ty}) scale(${view.scale})`}>
          {geoms.map((g) => (
            <EdgePaths
              key={g.id}
              geom={g}
              selected={selection?.kind === "edge" && selection.id === g.id}
              onSelect={() => {
                if (link) return;
                guardedSelect({ kind: "edge", id: g.id });
              }}
            />
          ))}
          {model.nodes.map((n) => {
            const r = layout.rects.get(n.id);
            if (!r) return null;
            const statusNode = run?.status?.nodes?.[n.id];
            const rt = run?.derived.nodes.get(n.id);
            const vs: VisualState = run ? visualState(statusNode?.state, rt) : "idle";
            const glow = GLOW[vs];
            const live = run ? nodeLiveTokens(run.liveTokens, n.id, statusNode) : 0;
            const tokens = (rt?.tokensCommitted ?? 0) + live;
            const timer =
              vs === "running" && rt?.lastStartTs ? now - rt.lastStartTs : null;
            const isSel = selection?.kind === "node" && selection.id === n.id;
            const label = nodeLabel(n);
            return (
              <g
                key={n.id}
                className="node"
                filter={glow ? `url(#${glowId(nodeMode, vs)})` : undefined}
                onMouseDown={
                  onSetPos
                    ? (e) => {
                        // Shift+LClick picks the node up for arbitrary
                        // placement; a plain press stays a pan.
                        if (e.button !== 0 || !e.shiftKey || link) return;
                        const rect = holder.current?.getBoundingClientRect();
                        if (!rect) return;
                        e.stopPropagation();
                        setCtxMenu(null);
                        setNodeMenu(null);
                        moved.current = false;
                        // Deliberately no selection here: opening the
                        // inspector mid-gesture would resize the canvas
                        // under the drag. A motionless Shift+click still
                        // selects via the click handler.
                        nodeDrag.current = {
                          id: n.id,
                          dx: (e.clientX - rect.left - view.tx) / view.scale - r.x,
                          dy: (e.clientY - rect.top - view.ty) / view.scale - r.y,
                        };
                      }
                    : undefined
                }
                onContextMenu={(e) => onElementContextMenu(e, n.id)}
                onDoubleClick={
                  onSetLabel
                    ? (e) => {
                        e.stopPropagation();
                        setLabelEdit({ id: n.id, value: n.label ?? "" });
                      }
                    : undefined
                }
                onClick={(e) => {
                  e.stopPropagation();
                  if (moved.current) return;
                  if (link) {
                    completeLink(n.id);
                    return;
                  }
                  guardedSelect({ kind: "node", id: n.id });
                }}
              >
                <rect
                  x={r.x}
                  y={r.y}
                  width={r.w}
                  height={r.h}
                  rx={compact ? 6 : 11}
                  fill={nodeFill(n)}
                  className={"node-rect" + (isSel ? " selected" : "")}
                  strokeDasharray={vs === "paused" ? "7 5" : undefined}
                />
                {!compact && (
                  <text x={r.x + r.w / 2} y={r.y + 24} className="node-name" textAnchor="middle">
                    {fitText(label, r.w - 22, LABEL_FONT)}
                  </text>
                )}
                {!compact && (
                  <text x={r.x + 12} y={r.y + r.h - 12} className="node-sub" textAnchor="start">
                    {fitText(
                      n.type === "llm"
                        ? run
                          ? `${fmtTokens(tokens)} tok`
                          : modelAlias(n.model, n.provider)
                        : run && rt?.commits
                          ? `${rt.commits}×`
                          : "",
                      r.w - 24,
                      SUB_FONT,
                    )}
                  </text>
                )}
                {!compact && label !== n.id && timer == null && (
                  <text
                    x={r.x + r.w - 10}
                    y={r.y + r.h - 12}
                    className="node-id-badge"
                    textAnchor="end"
                  >
                    {fitText(n.id, 64, ID_BADGE_FONT)}
                  </text>
                )}
                {!compact && timer != null && (
                  <text
                    x={r.x + r.w - 12}
                    y={r.y + r.h - 12}
                    className="node-timer"
                    textAnchor="end"
                  >
                    {fmtDuration(timer)}
                  </text>
                )}
              </g>
            );
          })}
          <GeneratorGlyph
            rect={layout.rects.get(GEN)}
            fired={run?.derived.runStartTs != null}
            selected={selection?.kind === "generator"}
            onSelect={() => {
              if (link) return; // the Generator can never be an edge target
              guardedSelect({ kind: "generator", id: GEN });
            }}
            onContextMenu={
              onAddEdgeBetween ? (e) => onElementContextMenu(e, GEN) : undefined
            }
          />
          {link &&
            (() => {
              const r = layout.rects.get(link.from);
              if (!r) return null;
              const sx = r.x + r.w / 2;
              const sy = r.y;
              const dy = Math.max(30, Math.abs(link.cy - sy) * 0.45);
              return (
                <g className="link-preview-group">
                  <path
                    d={`M ${sx} ${sy} C ${sx} ${sy - dy}, ${link.cx} ${link.cy + dy}, ${link.cx} ${link.cy}`}
                    className="edge-path link-preview"
                  />
                  <circle cx={link.cx} cy={link.cy} r={4.5} className="link-preview-dot" />
                </g>
              );
            })()}
        </g>
      </svg>
      <button
        className="fit-btn"
        title="Fit to view"
        onClick={(e) => {
          e.stopPropagation();
          fit();
        }}
      >
        ⛶
      </button>
      <button
        className="node-mode-btn"
        title={compact ? "Expand nodes" : "Collapse nodes"}
        onClick={(e) => {
          e.stopPropagation();
          setNodeMode((m) => (m === "compact" ? "expanded" : "compact"));
        }}
      >
        <svg viewBox="0 -960 960 960" width="19" height="19" fill="currentColor" aria-hidden="true">
          <path d={compact ? EXPAND_ICON : COLLAPSE_ICON} />
        </svg>
      </button>
      {onResetLayout && (
        <button
          className="reset-layout-btn"
          title="Return every node to its automatic position"
          disabled={!model.nodes.some((n) => n.pos)}
          onClick={(e) => {
            e.stopPropagation();
            onResetLayout();
          }}
        >
          Reset layout
        </button>
      )}
      {ctxMenu && onAddNode && (
        <div
          ref={menuRef}
          className="ctx-menu"
          style={{ left: ctxMenu.x, top: ctxMenu.y }}
          onMouseDown={(e) => e.stopPropagation()}
          onClick={(e) => e.stopPropagation()}
          onWheel={(e) => e.stopPropagation()}
          onContextMenu={(e) => e.preventDefault()}
        >
          <div className="ctx-item has-sub">
            <span>New node</span>
            <span className="ctx-caret">▸</span>
            <div className="ctx-sub">
              {NODE_KINDS.map(({ kind, label }) => (
                <button
                  key={kind}
                  className="ctx-item"
                  onClick={() => {
                    setCtxMenu(null);
                    onAddNode(kind);
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {onAddEdge && (
            <button
              className="ctx-item"
              onClick={() => {
                setCtxMenu(null);
                onAddEdge();
              }}
            >
              New edge
            </button>
          )}
        </div>
      )}
      {nodeMenu && onAddEdgeBetween && (
        <div
          ref={nodeMenuRef}
          className="ctx-menu"
          style={{ left: nodeMenu.x, top: nodeMenu.y }}
          onMouseDown={(e) => e.stopPropagation()}
          onClick={(e) => e.stopPropagation()}
          onWheel={(e) => e.stopPropagation()}
          onContextMenu={(e) => e.preventDefault()}
        >
          <div className="ctx-item has-sub">
            <span>Add edge to</span>
            <span className="ctx-caret">▸</span>
            <div className="ctx-sub ctx-scroll">
              {edgeTargets(nodeMenu.id).map((n) => (
                <button
                  key={n.id}
                  className="ctx-item"
                  onClick={() => {
                    setNodeMenu(null);
                    onAddEdgeBetween(nodeMenu.id, n.id);
                  }}
                >
                  <span className="ctx-label">{nodeLabel(n)}</span>
                  <span className="ctx-rank mono">#{n.rank}</span>
                </button>
              ))}
              {edgeTargets(nodeMenu.id).length === 0 && (
                <div className="ctx-empty">no other nodes</div>
              )}
            </div>
          </div>
        </div>
      )}
      {labelEdit && labelEditRect && (
        <input
          className="node-label-editor"
          style={{
            left: view.tx + labelEditRect.x * view.scale,
            top: view.ty + labelEditRect.y * view.scale + 6 * view.scale,
            width: labelEditRect.w * view.scale,
            height: Math.max(16, 26 * view.scale),
            fontSize: Math.max(9, 15 * view.scale),
          }}
          value={labelEdit.value}
          placeholder={labelEdit.id}
          spellCheck={false}
          ref={(el) => {
            if (el && document.activeElement !== el) {
              el.focus();
              el.select();
            }
          }}
          onChange={(e) =>
            setLabelEdit((le) => (le ? { ...le, value: e.target.value } : le))
          }
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === "Enter") commitLabelEdit();
            else if (e.key === "Escape") setLabelEdit(null);
          }}
          onBlur={commitLabelEdit}
          onMouseDown={(e) => e.stopPropagation()}
          onClick={(e) => e.stopPropagation()}
          onDoubleClick={(e) => e.stopPropagation()}
          onWheel={(e) => e.stopPropagation()}
          onContextMenu={(e) => {
            e.preventDefault();
            e.stopPropagation();
          }}
        />
      )}
    </div>
  );
}

function GeneratorGlyph(props: {
  rect: Rect | undefined;
  fired: boolean;
  selected: boolean;
  onSelect: () => void;
  onContextMenu?: (e: React.MouseEvent) => void;
}) {
  const r = props.rect;
  if (!r) return null;
  return (
    <g
      className="node"
      onContextMenu={
        props.onContextMenu ??
        ((e) => {
          e.preventDefault();
          e.stopPropagation();
        })
      }
      onClick={(e) => {
        e.stopPropagation();
        props.onSelect();
      }}
      filter={props.fired ? "url(#glow-completed)" : undefined}
    >
      <rect
        x={r.x}
        y={r.y}
        width={GEN_W}
        height={GEN_H}
        rx={GEN_H / 2}
        className={"gen-rect" + (props.selected ? " selected" : "")}
      />
      <text x={r.x + GEN_W / 2} y={r.y + GEN_H / 2 + 4.5} className="gen-label" textAnchor="middle">
        ◉ GEN
      </text>
    </g>
  );
}

function EdgePaths(props: {
  geom: EdgeGeometry;
  selected: boolean;
  onSelect: () => void;
}) {
  const { geom, selected, onSelect } = props;
  const cls = "edge-path" + (selected ? " selected" : "");
  return (
    <g
      className="edge"
      onContextMenu={(e) => {
        e.preventDefault();
        e.stopPropagation();
      }}
      onClick={(e) => {
        e.stopPropagation();
        onSelect();
      }}
    >
      {geom.segments.map((s, i) => (
        <path key={"hit" + i} d={s.d} className="edge-hit" />
      ))}
      {geom.segments.map((s, i) => (
        <path key={i} d={s.d} className={cls} />
      ))}
      {geom.junction && (
        <circle
          cx={geom.junction.x}
          cy={geom.junction.y}
          r={2.5}
          className={"edge-junction" + (selected ? " selected" : "")}
        />
      )}
      {geom.segments.flatMap((s, i) =>
        s.arrows.map((a, j) => (
          <polygon
            key={`${i}-${j}`}
            points="0,-2.75 5.5,0 0,2.75"
            transform={`translate(${a.x} ${a.y}) rotate(${a.angle})`}
            className={
              (s.golden ? "edge-arrow golden" : "edge-arrow") + (selected ? " selected" : "")
            }
          />
        )),
      )}
    </g>
  );
}
