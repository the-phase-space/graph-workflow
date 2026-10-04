// Monitor GUI top level. When no graph is executing this is a graph editor
// and validation environment (build mode); while a graph is executing it is
// a live visualization and operational control surface (execution mode).
// Python is the authoritative backend on both sides of that line.

import { listen } from "@tauri-apps/api/event";
import { getCurrentWebviewWindow } from "@tauri-apps/api/webviewWindow";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { confirm as confirmDialog, open as openDialog, save as saveDialog } from "@tauri-apps/plugin-dialog";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import Canvas from "./components/Canvas";
import { EdgeDialog, GeneratorDialog, GraphDialog } from "./components/EdgeDialog";
import NodeDialog from "./components/NodeDialog";
import Sidebar, { type ValidationReport } from "./components/Sidebar";
import {
  addEdge,
  addNode,
  makeEdge,
  makeNode,
  replaceEdge,
  replaceNode,
} from "./graphops";
import {
  controlRun,
  deleteGraphElement,
  dirname,
  envInfo,
  isBackendError,
  joinPath,
  listDir,
  loadGraph,
  saveGraph,
  spawnRun,
  templateGraph,
  validateGraph,
} from "./ipc";
import { useRunState } from "./runstate";
import type { Contract, NodeSpec, Selection } from "./types";

function parseRunDirArg(args: string[]): string | null {
  const i = args.indexOf("--run-dir");
  if (i >= 0 && args[i + 1]) return args[i + 1];
  return null;
}

function parseGraphFileArg(args: string[]): string | null {
  return args.find((a) => a.toLowerCase().endsWith(".py")) ?? null;
}

function stamp(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

export default function App() {
  const [theme, setTheme] = useState<string>(() =>
    localStorage.getItem("gw-theme") ??
    (window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light"),
  );
  const [contract, setContract] = useState<Contract | null>(null);
  // A new/opened definition gets a new canvas identity; edits and saves do not.
  const [graphSession, setGraphSession] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [runDir, setRunDir] = useState<string | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [validation, setValidation] = useState<ValidationReport | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [tempRuns, setTempRuns] = useState<string | null>(null);
  const [dropHover, setDropHover] = useState(false);

  const run = useRunState(runDir);
  const mode: "welcome" | "build" | "exec" = runDir ? "exec" : contract ? "build" : "welcome";

  // -- inspector panel geometry ------------------------------------------- //
  // A docked right sidebar hosting the element dialogs: 33% of the workspace
  // by default, freely resizable via its left-edge handle, width persisted.

  const appRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLDivElement>(null);
  const [inspectorWidth, setInspectorWidth] = useState<number>(() => {
    const stored = Number(localStorage.getItem("gw-inspector-width"));
    return Number.isFinite(stored) && stored >= 280 ? stored : 0;
  });
  const inspectorWidthRef = useRef(inspectorWidth);
  const resizing = useRef(false);

  const startInspectorResize = (e: React.MouseEvent) => {
    e.preventDefault();
    resizing.current = true;
    document.body.classList.add("col-resizing");
  };

  useEffect(() => {
    const move = (e: MouseEvent) => {
      if (!resizing.current || !appRef.current) return;
      const rect = appRef.current.getBoundingClientRect();
      const w = Math.min(
        Math.max(280, Math.round(rect.right - e.clientX)),
        Math.max(320, rect.width - 300),
      );
      inspectorWidthRef.current = w;
      setInspectorWidth(w);
    };
    const up = () => {
      if (!resizing.current) return;
      resizing.current = false;
      document.body.classList.remove("col-resizing");
      localStorage.setItem("gw-inspector-width", String(inspectorWidthRef.current));
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("gw-theme", theme);
  }, [theme]);

  const report = useCallback((text: string) => {
    setNotice(text);
    window.setTimeout(() => setNotice((n) => (n === text ? null : n)), 7000);
  }, []);

  // Undo/redo state: every build-mode mutation funnels through mutate()
  // (defined with the other build-mode operations below), so history is a
  // bounded snapshot stack over the whole contract.
  const contractRef = useRef<Contract | null>(null);
  useEffect(() => {
    contractRef.current = contract;
  }, [contract]);
  const pastRef = useRef<Contract[]>([]);
  const futureRef = useRef<Contract[]>([]);
  const lastEditRef = useRef<{ key: string; ts: number } | null>(null);
  const pendingDeletionRef = useRef<Promise<void> | null>(null);

  const resetHistory = useCallback(() => {
    pastRef.current = [];
    futureRef.current = [];
    lastEditRef.current = null;
  }, []);

  // Manual node positions are session-only view state: they overlay the
  // displayed canvas but never enter the contract, the undo history, the
  // dirty flag, or the saved definition.
  const [nodePos, setNodePos] = useState<Record<string, { x: number; y: number }>>({});

  // -- opening / attaching ------------------------------------------------ //

  const openPath = useCallback(
    async (path: string) => {
      setBusy("Loading graph definition…");
      try {
        const result = await loadGraph(path);
        if (isBackendError(result)) {
          report(`Load failed: ${result.error}`);
          return;
        }
        setContract(result);
        setGraphSession((s) => s + 1);
        resetHistory();
        setNodePos({});
        setDirty(false);
        setSelection(null);
        setRunDir(null);
        setValidation(
          result.errors.length || result.warnings.length
            ? { errors: result.errors, warnings: result.warnings, at: Date.now() }
            : null,
        );
      } catch (e) {
        report(String(e));
      } finally {
        setBusy(null);
      }
    },
    [report, resetHistory],
  );

  useEffect(() => {
    void (async () => {
      const env = await envInfo();
      setTempRuns(env.temp_runs);
      const rd = parseRunDirArg(env.args);
      if (rd) setRunDir(rd);
      else {
        const file = parseGraphFileArg(env.args);
        if (file) void openPath(file);
      }
    })();
    const un = listen<string[]>("gw-attach", (ev) => {
      const rd = parseRunDirArg(ev.payload ?? []);
      if (rd) {
        setSelection(null);
        setRunDir(rd);
      } else {
        const file = parseGraphFileArg(ev.payload ?? []);
        if (file) void openPath(file);
      }
    });
    return () => {
      void un.then((f) => f());
    };
  }, [openPath]);

  // Native file drag-drop: a .py loads as a graph definition; a directory
  // containing a trace attaches as a run.
  useEffect(() => {
    const un = getCurrentWebviewWindow().onDragDropEvent(async (event) => {
      const t = event.payload.type;
      if (t === "enter" || t === "over") setDropHover(true);
      else if (t === "leave") setDropHover(false);
      else if (t === "drop") {
        setDropHover(false);
        const paths = event.payload.paths ?? [];
        const py = paths.find((p) => p.toLowerCase().endsWith(".py"));
        if (py) {
          void openPath(py);
          return;
        }
        for (const p of paths) {
          const entries = await listDir(p).catch(() => []);
          if (entries.some((e) => e.name === "trace.jsonl" || e.name === "status.json")) {
            setSelection(null);
            setRunDir(p);
            return;
          }
        }
        if (paths.length) report("Drop a graph definition (.py) or a run directory.");
      }
    });
    return () => {
      void un.then((f) => f());
    };
  }, [openPath, report]);

  // -- window title: the graph name lives here, not in a top bar ---------- //

  const execModel = run.derived.graph;
  useEffect(() => {
    const name =
      mode === "exec"
        ? execModel?.name ?? run.status?.graph ?? "attaching…"
        : contract?.graph.name ?? "graph-workflow";
    const suffix = mode === "build" && dirty ? " •" : "";
    void getCurrentWindow().setTitle(`${name}${suffix} — graph-workflow Monitor`);
  }, [mode, contract, dirty, execModel, run.status]);

  // -- build-mode operations ---------------------------------------------- //

  const mutate = useCallback((fn: (c: Contract) => Contract, coalesce?: string) => {
    const cur = contractRef.current;
    if (!cur) return;
    const now = Date.now();
    const last = lastEditRef.current;
    if (!(coalesce && last && last.key === coalesce && now - last.ts < 900)) {
      pastRef.current.push(cur);
      if (pastRef.current.length > 100) pastRef.current.shift();
    }
    lastEditRef.current = coalesce ? { key: coalesce, ts: now } : null;
    futureRef.current = [];
    const next = fn(cur);
    contractRef.current = next;
    setContract(next);
    setDirty(true);
  }, []);

  const doDelete = useCallback((kind: "node" | "edge", id: string) => {
    const current = contractRef.current;
    if (!current || pendingDeletionRef.current || busy) return;
    const task = (async () => {
      setBusy("Deleting and updating gates…");
      try {
        const result = await deleteGraphElement(current.graph, kind, id);
        if (!result.ok) {
          report(`Delete failed: ${result.error}`);
          return;
        }
        if (contractRef.current !== current) {
          report("The graph changed while deleting. Please retry the deletion.");
          return;
        }
        mutate((c) => ({ ...c, graph: result.graph }));
        setSelection(null);
        setValidation(null);
      } catch (e) {
        report(`Delete failed: ${String(e)}`);
      } finally {
        setBusy(null);
      }
    })();
    pendingDeletionRef.current = task;
    void task.finally(() => {
      if (pendingDeletionRef.current === task) pendingDeletionRef.current = null;
    });
  }, [busy, mutate, report]);

  const undo = useCallback(() => {
    const cur = contractRef.current;
    const prev = pastRef.current.pop();
    if (!cur || !prev) return;
    futureRef.current.push(cur);
    lastEditRef.current = null;
    contractRef.current = prev;
    setContract(prev);
    setDirty(true);
  }, []);

  const redo = useCallback(() => {
    const cur = contractRef.current;
    const next = futureRef.current.pop();
    if (!cur || !next) return;
    pastRef.current.push(cur);
    lastEditRef.current = null;
    contractRef.current = next;
    setContract(next);
    setDirty(true);
  }, []);

  const doNew = useCallback(async () => {
    const result = await templateGraph("untitled");
    if (isBackendError(result)) {
      report(result.error);
      return;
    }
    setContract(result);
    setGraphSession((s) => s + 1);
    resetHistory();
    setNodePos({});
    setDirty(true);
    setRunDir(null);
    setSelection(null);
    setValidation(null);
  }, [report, resetHistory]);

  const doOpen = useCallback(async () => {
    const picked = await openDialog({
      multiple: false,
      filters: [{ name: "Graph definition", extensions: ["py"] }],
    });
    if (typeof picked === "string") await openPath(picked);
  }, [openPath]);

  const doSaveAs = useCallback(async (): Promise<string | null> => {
    if (!contract) return null;
    const picked = await saveDialog({
      defaultPath: contract.path ?? `${contract.graph.name || "graph"}.py`,
      filters: [{ name: "Graph definition", extensions: ["py"] }],
    });
    if (!picked) return null;
    return doSaveTo(picked, true);
  }, [contract]);

  const doSaveTo = useCallback(
    async (path: string, force: boolean): Promise<string | null> => {
      await pendingDeletionRef.current;
      const current = contractRef.current;
      if (!current) return null;
      setBusy("Saving…");
      try {
        const result = await saveGraph(path, current, force);
        if (isBackendError(result)) {
          report(`Save failed: ${result.error}`);
          return null;
        }
        setContract(result);
        // The post-save contract is a regeneration; keep undo history across
        // the save, but never coalesce a later edit into a pre-save one.
        lastEditRef.current = null;
        setDirty(false);
        setValidation(
          result.errors.length || result.warnings.length
            ? { errors: result.errors, warnings: result.warnings, at: Date.now() }
            : null,
        );
        return result.path;
      } catch (e) {
        report(String(e));
        return null;
      } finally {
        setBusy(null);
      }
    },
    [contract, report],
  );

  const doSave = useCallback(async (): Promise<string | null> => {
    if (!contract) return null;
    if (!contract.path) return doSaveAs();
    if (!contract.structured) {
      const ok = await confirmDialog(
        "This definition was not written by the Monitor: saving converts it to the structured format. Module-level helper code is preserved in the helpers section; the graph construction itself is regenerated. Overwrite?",
        { title: "Overwrite handwritten definition?", kind: "warning" },
      );
      if (!ok) return null;
      return doSaveTo(contract.path, true);
    }
    return doSaveTo(contract.path, true);
  }, [contract, doSaveAs, doSaveTo]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Delete") {
        if (
          mode !== "build" || busy || selection?.kind !== "node" ||
          e.defaultPrevented || e.isComposing || e.repeat ||
          e.ctrlKey || e.metaKey || e.altKey || e.shiftKey
        ) return;
        // Delete belongs to the focused editor while entering text.
        const target = document.activeElement;
        if (
          target instanceof HTMLElement &&
          (target.tagName === "INPUT" || target.tagName === "TEXTAREA" ||
            target.tagName === "SELECT" || target.isContentEditable)
        ) return;
        e.preventDefault();
        doDelete("node", selection.id);
        return;
      }
      if (!(e.ctrlKey || e.metaKey)) return;
      const k = e.key.toLowerCase();
      if (k === "s") {
        e.preventDefault();
        if (mode !== "build") return;
        if (e.shiftKey) void doSaveAs();
        else void doSave();
      } else if (k === "z" && !e.shiftKey) {
        if (mode !== "build") return;
        e.preventDefault();
        undo();
      } else if ((k === "z" && e.shiftKey) || k === "y") {
        if (mode !== "build") return;
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode, busy, selection, doDelete, doSave, doSaveAs, undo, redo]);

  const doValidate = useCallback(async () => {
    if (!contract) return;
    let path = contract.path;
    if (!path || dirty) path = await doSave();
    if (!path) return;
    setBusy("Validating…");
    try {
      const result = await validateGraph(path);
      setValidation({
        errors: result.errors ?? (result.error ? [result.error] : []),
        warnings: result.warnings ?? [],
        at: Date.now(),
      });
    } catch (e) {
      report(String(e));
    } finally {
      setBusy(null);
    }
  }, [contract, dirty, doSave, report]);

  const doRun = useCallback(
    async () => {
      if (!contract) return;
      let path = contract.path;
      if (!path || dirty) path = await doSave();
      if (!path) return;
      // The definition's declared run dir wins; otherwise pick one in local
      // temp, never next to a possibly-synced graph file: sync clients
      // briefly lock the interchange files mid-run.
      const slug =
        contract.graph.name.replace(/[^A-Za-z0-9_-]+/g, "-").slice(0, 40) || "graph";
      const rd =
        contract.graph.run_dir ||
        (tempRuns
          ? joinPath(tempRuns, `${slug}-${stamp()}`)
          : joinPath(dirname(path), "runs", `gui-${stamp()}`));
      try {
        await spawnRun(path, rd, joinPath(rd, "run.log"));
        setSelection(null);
        setRunDir(rd);
      } catch (e) {
        report(String(e));
      }
    },
    [contract, dirty, doSave, report, tempRuns],
  );

  const doAddNode = useCallback(
    (kind: string) =>
      mutate((c) => {
        const node =
          kind === "script" || kind === "human"
            ? makeNode(c.graph, kind)
            : makeNode(c.graph, "llm", kind);
        setSelection({ kind: "node", id: node.id });
        return { ...c, graph: addNode(c.graph, node) };
      }),
    [mutate],
  );

  const doAddEdge = useCallback(
    () =>
      mutate((c) => {
        const edge = makeEdge(c.graph);
        setSelection({ kind: "edge", id: edge.id });
        return { ...c, graph: addEdge(c.graph, edge) };
      }),
    [mutate],
  );

  const doAddEdgeBetween = useCallback(
    (from: string, to: string) =>
      mutate((c) => {
        const edge = makeEdge(c.graph, [from], [to]);
        setSelection({ kind: "edge", id: edge.id });
        return { ...c, graph: addEdge(c.graph, edge) };
      }),
    [mutate],
  );

  const doSetLabel = useCallback(
    (id: string, label: string | null) =>
      mutate((c) => ({
        ...c,
        graph: {
          ...c.graph,
          nodes: c.graph.nodes.map((n) => (n.id === id ? { ...n, label } : n)),
        },
      })),
    [mutate],
  );

  // Shift+drag node movement: view-only, so no mutate() — a move is not an
  // edit to the graph definition.
  const doSetPos = useCallback(
    (id: string, pos: { x: number; y: number }) =>
      setNodePos((m) => ({ ...m, [id]: pos })),
    [],
  );

  const doResetLayout = useCallback(() => setNodePos({}), []);

  const doAttach = useCallback(async () => {
    const picked = await openDialog({ directory: true, multiple: false });
    if (typeof picked === "string") {
      setSelection(null);
      setRunDir(picked);
    }
  }, []);

  // -- render ------------------------------------------------------------- //

  const model = mode === "exec" ? execModel : contract?.graph ?? null;
  const editable = mode === "build" && !busy;
  const helpers = contract?.helpers ?? "";

  // The canvas sees the session's manual positions overlaid; everything else
  // (inspector, sidebar, save) works off the position-free model.
  const canvasModel = useMemo(() => {
    if (!model) return null;
    let moved = false;
    const nodes = model.nodes.map((n) => {
      const p = nodePos[n.id];
      if (!p) return n;
      moved = true;
      return { ...n, pos: p };
    });
    return moved ? { ...model, nodes } : model;
  }, [model, nodePos]);

  const selectedNode: NodeSpec | null = useMemo(() => {
    if (!model || selection?.kind !== "node") return null;
    return model.nodes.find((n) => n.id === selection.id) ?? null;
  }, [model, selection]);

  const selectedEdge = useMemo(() => {
    if (!model || selection?.kind !== "edge") return null;
    return model.edges.find((e) => e.id === selection.id) ?? null;
  }, [model, selection]);

  const inspectorContent =
    selectedNode && model ? (
      <NodeDialog
        node={selectedNode}
        run={mode === "exec" ? run : null}
        editable={editable}
        onChange={(n) =>
          mutate((c) => {
            if (n.id !== selectedNode.id) setSelection({ kind: "node", id: n.id });
            return { ...c, graph: replaceNode(c.graph, selectedNode.id, n) };
          }, `insp:node:${selectedNode.id}`)
        }
        onDelete={() => doDelete("node", selectedNode.id)}
        onClose={() => setSelection(null)}
      />
    ) : selectedEdge && model ? (
      <EdgeDialog
        edge={selectedEdge}
        model={model}
        editable={editable}
        onChange={(e) =>
          mutate((c) => {
            if (e.id !== selectedEdge.id) setSelection({ kind: "edge", id: e.id });
            return { ...c, graph: replaceEdge(c.graph, selectedEdge.id, e) };
          }, `insp:edge:${selectedEdge.id}`)
        }
        onDelete={() => doDelete("edge", selectedEdge.id)}
        onClose={() => setSelection(null)}
      />
    ) : selection?.kind === "generator" ? (
      <GeneratorDialog
        fired={mode === "exec" && run.derived.runStartTs != null}
        onClose={() => setSelection(null)}
      />
    ) : selection?.kind === "graph" && model ? (
      <GraphDialog
        model={model}
        helpers={helpers}
        editable={editable}
        onChange={(m) => mutate((c) => ({ ...c, graph: m }), "insp:graph")}
        onHelpers={(h) => mutate((c) => ({ ...c, helpers: h }), "insp:helpers")}
        onClose={() => setSelection(null)}
      />
    ) : null;

  // Default the panel to 33% of the workspace the first time it opens
  // (before paint, so no flash at the fallback width).
  const inspectorOpen = inspectorContent != null;
  useLayoutEffect(() => {
    if (inspectorOpen && inspectorWidth === 0 && mainRef.current) {
      const w = Math.round(mainRef.current.clientWidth * 0.33);
      inspectorWidthRef.current = w;
      setInspectorWidth(w);
    }
  }, [inspectorOpen, inspectorWidth]);

  return (
    <div className="app" ref={appRef}>
      <Sidebar
        mode={mode === "exec" ? "exec" : "build"}
        model={model}
        run={mode === "exec" ? run : null}
        path={contract?.path ?? null}
        canBuild={!!contract}
        validation={validation}
        onNew={() => void doNew()}
        onOpen={() => void doOpen()}
        onSave={() => void doSave()}
        onSaveAs={() => void doSaveAs()}
        onValidate={() => void doValidate()}
        onRun={() => void doRun()}
        onAttach={() => void doAttach()}
        onAddNode={doAddNode}
        onAddEdge={doAddEdge}
        onSelect={setSelection}
        onPause={() => runDir && void controlRun(runDir, "pause")}
        onResume={() => runDir && void controlRun(runDir, "resume")}
        onStop={() => runDir && void controlRun(runDir, "stop")}
        onBackToBuild={() => {
          setSelection(null);
          setRunDir(null);
        }}
        theme={theme}
        onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
      />

      <div className="main" ref={mainRef}>
        {canvasModel ? (
          <Canvas
            graphKey={mode === "exec" ? `run:${runDir}:${run.derived.runId}` : `build:${graphSession}`}
            model={canvasModel}
            run={mode === "exec" ? run : null}
            selection={selection}
            onSelect={setSelection}
            onAddNode={editable ? doAddNode : undefined}
            onAddEdge={editable ? doAddEdge : undefined}
            onAddEdgeBetween={editable ? doAddEdgeBetween : undefined}
            onSetLabel={editable ? doSetLabel : undefined}
            onSetPos={editable ? doSetPos : undefined}
            onResetLayout={editable ? doResetLayout : undefined}
          />
        ) : mode === "exec" ? (
          <div className="welcome">
            <h2>Attaching to run…</h2>
            <p className="mono path-note">{runDir}</p>
            <p>Waiting for the runtime's trace to appear.</p>
            {run.runLogTail && <pre className="runlog">{run.runLogTail}</pre>}
          </div>
        ) : (
          <div className="welcome">
            <h1>graph-workflow</h1>
            <p>
              Compose and execute directed graphs of script, LLM, and human nodes.
            </p>
            <div className="btn-row">
              <button onClick={() => void doNew()}>New graph</button>
              <button onClick={() => void doOpen()}>Open definition…</button>
              <button onClick={() => void doAttach()}>Attach to run…</button>
            </div>
          </div>
        )}

        {dropHover && (
          <div className="drop-overlay">Drop graph definition (.py) or run directory</div>
        )}
        {busy && <div className="toast busy">{busy}</div>}
        {notice && (
          <div className="toast notice" onClick={() => setNotice(null)}>
            {notice}
          </div>
        )}
      </div>

      {inspectorContent && (
        <aside
          className="inspector"
          style={{ width: inspectorWidth ? inspectorWidth : "33%" }}
        >
          <div
            className="inspector-handle"
            title="Drag to resize"
            onMouseDown={startInspectorResize}
          />
          {inspectorContent}
        </aside>
      )}
    </div>
  );
}
