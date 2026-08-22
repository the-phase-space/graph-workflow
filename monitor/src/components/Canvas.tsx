// The graph canvas: nearly the whole window. SVG rendering with mouse-drag
// panning, scroll-wheel zooming, and a fit-to-view control at the top right.
// The default viewport shows the whole graph. Nodes are not user-movable.

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  computeLayout,
  edgeGeometry,
  GEN_H,
  GEN_W,
  type EdgeGeometry,
  type Rect,
} from "../layout";
import { nodeLiveTokens, visualState, type RunState, type VisualState } from "../runstate";
import {
  GEN,
  fmtDuration,
  fmtTokens,
  nodeFill,
  type GraphModel,
  type Selection,
} from "../types";

const GLOW: Record<VisualState, string | null> = {
  running: "#f2c521",
  ready: "#3f8cff",
  paused: "#e8a33d",
  error: "#ff4136",
  completed: "#2ecc71",
  idle: null,
};

/** Photoshop-style outer glow: the halo exists strictly outside the node's
 * silhouette — the blurred shape is composited `out` against the source
 * alpha, so a translucent node face is never tinted from behind. */
function OuterGlowFilter(props: { id: string; color: string }) {
  return (
    <filter id={props.id} x="-50%" y="-130%" width="200%" height="360%">
      {/* Harden the translucent face to a solid silhouette first, so the
          interior punch-out removes the halo completely. */}
      <feComponentTransfer in="SourceAlpha" result="solid">
        <feFuncA type="linear" slope="40" intercept="0" />
      </feComponentTransfer>
      <feMorphology in="solid" operator="dilate" radius="1.5" result="dil" />
      <feGaussianBlur in="dil" stdDeviation="5.5" result="blurNear" />
      <feComposite in="blurNear" in2="solid" operator="out" result="ringNear" />
      <feFlood floodColor={props.color} floodOpacity="0.95" result="colNear" />
      <feComposite in="colNear" in2="ringNear" operator="in" result="glowNear" />
      <feGaussianBlur in="dil" stdDeviation="14" result="blurFar" />
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
}) {
  const { model, run, selection, onSelect, onAddNode, onAddEdge } = props;
  const holder = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [view, setView] = useState<View>({ tx: 0, ty: 0, scale: 1 });
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number } | null>(null);
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null);
  const fittedFor = useRef<string>("");

  const layout = useMemo(() => computeLayout(model), [model]);
  const geoms = useMemo(
    () =>
      model.edges
        .map((e) => edgeGeometry(e, layout.rects, layout.bbox))
        .filter((g): g is EdgeGeometry => !!g),
    [model, layout],
  );

  useEffect(() => {
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

  // The default viewport shows the whole graph: fit on first layout and
  // whenever the topology or window geometry changes identity.
  useEffect(() => {
    const key = `${model.name}|${model.nodes.length}|${model.edges.length}|${size.w}x${size.h}`;
    if (fittedFor.current !== key) {
      fittedFor.current = key;
      fit();
    }
  }, [model, size, fit]);

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

  const moved = useRef(false);
  const onMouseDown = (e: React.MouseEvent) => {
    setCtxMenu(null);
    if (e.button !== 0) return;
    moved.current = false;
    drag.current = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty };
  };

  // Right-clicking empty canvas offers element creation in build mode; the
  // webview's own context menu is suppressed everywhere on the canvas.
  const onContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    if (!onAddNode) return;
    const rect = holder.current?.getBoundingClientRect();
    if (!rect) return;
    // Keep the menu (and room for its hover submenu) inside the canvas.
    const x = Math.min(e.clientX - rect.left, Math.max(0, rect.width - 290));
    const y = Math.min(e.clientY - rect.top, Math.max(0, rect.height - 100));
    setCtxMenu({ x, y });
  };

  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ctxMenu) return;
    const dismiss = () => setCtxMenu(null);
    const onDown = (e: MouseEvent) => {
      if (e.target instanceof Node && menuRef.current?.contains(e.target)) return;
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
  }, [ctxMenu]);
  useEffect(() => {
    const move = (e: MouseEvent) => {
      const d = drag.current;
      if (!d) return;
      if (Math.abs(e.clientX - d.x) + Math.abs(e.clientY - d.y) > 3) moved.current = true;
      setView((v) => ({ ...v, tx: d.tx + (e.clientX - d.x), ty: d.ty + (e.clientY - d.y) }));
    };
    const up = () => (drag.current = null);
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, []);

  const now = Date.now() / 1000;

  return (
    <div
      ref={holder}
      className="canvas-holder"
      onWheel={(e) => {
        setCtxMenu(null);
        onWheel(e);
      }}
      onMouseDown={onMouseDown}
      onContextMenu={onContextMenu}
      onClick={() => {
        // Clicking empty canvas minimizes the inspector; a pan drag that ends
        // over the canvas still emits a click, so suppress those.
        if (moved.current) {
          moved.current = false;
          return;
        }
        onSelect(null);
      }}
    >
      <svg width={size.w} height={size.h} className="canvas-svg">
        <defs>
          {Object.entries(GLOW).map(([state, color]) =>
            color ? (
              <OuterGlowFilter key={state} id={`glow-${state}`} color={color} />
            ) : null,
          )}
        </defs>
        <g transform={`translate(${view.tx} ${view.ty}) scale(${view.scale})`}>
          {geoms.map((g) => (
            <EdgePaths
              key={g.id}
              geom={g}
              selected={selection?.kind === "edge" && selection.id === g.id}
              onSelect={() => onSelect({ kind: "edge", id: g.id })}
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
            return (
              <g
                key={n.id}
                className="node"
                filter={glow ? `url(#glow-${vs})` : undefined}
                onMouseDown={(e) => e.stopPropagation()}
                onContextMenu={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                }}
                onClick={(e) => {
                  e.stopPropagation();
                  onSelect({ kind: "node", id: n.id });
                }}
              >
                <rect
                  x={r.x}
                  y={r.y}
                  width={r.w}
                  height={r.h}
                  rx={11}
                  fill={nodeFill(n)}
                  className={"node-rect" + (isSel ? " selected" : "")}
                  strokeDasharray={vs === "paused" ? "7 5" : undefined}
                />
                <text x={r.x + r.w / 2} y={r.y + 24} className="node-name" textAnchor="middle">
                  {n.id}
                </text>
                <text x={r.x + 12} y={r.y + r.h - 12} className="node-sub" textAnchor="start">
                  {n.type === "llm"
                    ? run
                      ? `${fmtTokens(tokens)} tok`
                      : n.model ?? ""
                    : run && rt?.commits
                      ? `${rt.commits}×`
                      : ""}
                </text>
                {timer != null && (
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
            onSelect={() => onSelect({ kind: "generator", id: GEN })}
          />
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
    </div>
  );
}

function GeneratorGlyph(props: {
  rect: Rect | undefined;
  fired: boolean;
  selected: boolean;
  onSelect: () => void;
}) {
  const r = props.rect;
  if (!r) return null;
  return (
    <g
      className="node"
      onMouseDown={(e) => e.stopPropagation()}
      onContextMenu={(e) => {
        e.preventDefault();
        e.stopPropagation();
      }}
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
      onMouseDown={(e) => e.stopPropagation()}
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
          r={5}
          className={"edge-junction" + (selected ? " selected" : "")}
        />
      )}
      {geom.segments.flatMap((s, i) =>
        s.arrows.map((a, j) => (
          <polygon
            key={`${i}-${j}`}
            points="0,-5.5 11,0 0,5.5"
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
