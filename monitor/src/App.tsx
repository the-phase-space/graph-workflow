// Monitor GUI top level. When no graph is executing this is a graph editor
// and validation environment (build mode); while a graph is executing it is
// a live visualization and operational control surface (execution mode).
// Python is the authoritative backend on both sides of that line.

import { listen } from "@tauri-apps/api/event";
import { getCurrentWebviewWindow } from "@tauri-apps/api/webviewWindow";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { confirm as confirmDialog, open as openDialog, save as saveDialog } from "@tauri-apps/plugin-dialog";
import { useCallback, useEffect, useMemo, useState } from "react";
import Canvas from "./components/Canvas";
import { EdgeDialog, GeneratorDialog, GraphDialog } from "./components/EdgeDialog";
import NodeDialog from "./components/NodeDialog";
import Sidebar, { type ValidationReport } from "./components/Sidebar";
import {
  addEdge,
  addNode,
  deleteEdge,
  deleteNode,
  makeEdge,
  makeNode,
  replaceEdge,
  replaceNode,
} from "./graphops";
import {
  controlRun,
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

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("gw-theme", theme);
  }, [theme]);

  const report = useCallback((text: string) => {
    setNotice(text);
    window.setTimeout(() => setNotice((n) => (n === text ? null : n)), 7000);
  }, []);

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
    [report],
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

  const mutate = useCallback(
    (fn: (c: Contract) => Contract) => {
      setContract((c) => (c ? fn(c) : c));
      setDirty(true);
    },
    [],
  );

  const doNew = useCallback(async () => {
    const result = await templateGraph("untitled");
    if (isBackendError(result)) {
      report(result.error);
      return;
    }
    setContract(result);
    setDirty(true);
    setRunDir(null);
    setSelection(null);
    setValidation(null);
  }, [report]);

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
      if (!contract) return null;
      setBusy("Saving…");
      try {
        const result = await saveGraph(path, contract, force);
        if (isBackendError(result)) {
          report(`Save failed: ${result.error}`);
          return null;
        }
        setContract(result);
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
        "This definition was not written by the Monitor: saving regenerates it in the structured format and may discard handwritten Python outside the contract. Overwrite?",
        { title: "Overwrite handwritten definition?", kind: "warning" },
      );
      if (!ok) return null;
      return doSaveTo(contract.path, true);
    }
    return doSaveTo(contract.path, true);
  }, [contract, doSaveAs, doSaveTo]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (mode === "build") void doSave();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode, doSave]);

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
    async (seed: string) => {
      if (!contract) return;
      let path = contract.path;
      if (!path || dirty) path = await doSave();
      if (!path) return;
      // Run dirs live in local temp, never next to a possibly-synced graph
      // file: sync clients briefly lock the interchange files mid-run.
      const slug =
        contract.graph.name.replace(/[^A-Za-z0-9_-]+/g, "-").slice(0, 40) || "graph";
      const rd = tempRuns
        ? joinPath(tempRuns, `${slug}-${stamp()}`)
        : joinPath(dirname(path), "runs", `gui-${stamp()}`);
      try {
        await spawnRun(path, rd, seed.trim() || null, joinPath(rd, "run.log"));
        setSelection(null);
        setRunDir(rd);
      } catch (e) {
        report(String(e));
      }
    },
    [contract, dirty, doSave, report, tempRuns],
  );

  const doAttach = useCallback(async () => {
    const picked = await openDialog({ directory: true, multiple: false });
    if (typeof picked === "string") {
      setSelection(null);
      setRunDir(picked);
    }
  }, []);

  // -- render ------------------------------------------------------------- //

  const model = mode === "exec" ? execModel : contract?.graph ?? null;
  const editable = mode === "build";
  const helpers = contract?.helpers ?? "";

  const selectedNode: NodeSpec | null = useMemo(() => {
    if (!model || selection?.kind !== "node") return null;
    return model.nodes.find((n) => n.id === selection.id) ?? null;
  }, [model, selection]);

  const selectedEdge = useMemo(() => {
    if (!model || selection?.kind !== "edge") return null;
    return model.edges.find((e) => e.id === selection.id) ?? null;
  }, [model, selection]);

  return (
    <div className="app">
      <Sidebar
        mode={mode === "exec" ? "exec" : "build"}
        model={model}
        run={mode === "exec" ? run : null}
        dirty={dirty}
        path={contract?.path ?? null}
        canBuild={!!contract}
        validation={validation}
        onNew={() => void doNew()}
        onOpen={() => void doOpen()}
        onSave={() => void doSave()}
        onSaveAs={() => void doSaveAs()}
        onValidate={() => void doValidate()}
        onRun={(s) => void doRun(s)}
        onAttach={() => void doAttach()}
        onAddNode={(kind) =>
          mutate((c) => {
            const node =
              kind === "script" || kind === "human"
                ? makeNode(c.graph, kind)
                : makeNode(c.graph, "llm", kind);
            setSelection({ kind: "node", id: node.id });
            return { ...c, graph: addNode(c.graph, node) };
          })
        }
        onAddEdge={() =>
          mutate((c) => {
            const edge = makeEdge(c.graph);
            setSelection({ kind: "edge", id: edge.id });
            return { ...c, graph: addEdge(c.graph, edge) };
          })
        }
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

      <div className="main">
        {model ? (
          <Canvas
            model={model}
            run={mode === "exec" ? run : null}
            selection={selection}
            onSelect={setSelection}
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

        {selectedNode && model && (
          <NodeDialog
            node={selectedNode}
            run={mode === "exec" ? run : null}
            editable={editable}
            onChange={(n) =>
              mutate((c) => {
                if (n.id !== selectedNode.id) setSelection({ kind: "node", id: n.id });
                return { ...c, graph: replaceNode(c.graph, selectedNode.id, n) };
              })
            }
            onDelete={() => {
              setSelection(null);
              mutate((c) => ({ ...c, graph: deleteNode(c.graph, selectedNode.id) }));
            }}
            onClose={() => setSelection(null)}
          />
        )}
        {selectedEdge && model && (
          <EdgeDialog
            edge={selectedEdge}
            model={model}
            editable={editable}
            onChange={(e) =>
              mutate((c) => {
                if (e.id !== selectedEdge.id) setSelection({ kind: "edge", id: e.id });
                return { ...c, graph: replaceEdge(c.graph, selectedEdge.id, e) };
              })
            }
            onDelete={() => {
              setSelection(null);
              mutate((c) => ({ ...c, graph: deleteEdge(c.graph, selectedEdge.id) }));
            }}
            onClose={() => setSelection(null)}
          />
        )}
        {selection?.kind === "generator" && (
          <GeneratorDialog
            fired={mode === "exec" && run.derived.runStartTs != null}
            onClose={() => setSelection(null)}
          />
        )}
        {selection?.kind === "graph" && model && (
          <GraphDialog
            model={model}
            helpers={helpers}
            editable={editable}
            onChange={(m) => mutate((c) => ({ ...c, graph: m }))}
            onHelpers={(h) => mutate((c) => ({ ...c, helpers: h }))}
            onClose={() => setSelection(null)}
          />
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
    </div>
  );
}
