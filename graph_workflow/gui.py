"""The Monitor GUI's Python backend: the structured Python/JSON contract.

Python is the authoritative backend for authoring semantics, validation,
execution, monitoring, and run control; JSON is the interchange layer at the
application boundary. This module is the authoring half of that boundary,
invoked by the Monitor GUI as a CLI::

    python -m graph_workflow.gui load <graphdef.py>      # -> contract JSON
    python -m graph_workflow.gui validate <graphdef.py>  # -> {ok, errors, warnings}
    python -m graph_workflow.gui save <graphdef.py>      # contract JSON on stdin
    python -m graph_workflow.gui template [name]         # -> fresh contract

Loading *executes* the graph definition (the file is the entry point and
composition root) with ``run()`` intercepted, so any definition that would
start a run can be captured, visualized, and validated. Loading an
unstructured (handwritten) definition carves its module-level helper code —
everything the structured regeneration would not re-emit — into the
contract's helpers section so nothing the graph references is lost on the
first structured save. Saving generates a structured executable definition
from the contract, proves it executes in a temp file, and only then replaces
the target; a save that would produce a broken definition fails without
touching the file on disk. Arbitrary handwritten Python outside the
structured contract is not presumed to be losslessly reversible into
editable GUI elements: unrecoverable callables surface as opaque references,
and saving over a file that is not in the structured format is refused
unless forced.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import io
import json
import os
import re
import sys
import textwrap
from pathlib import Path

from .model import (
    GENERATOR_ID,
    Edge,
    Gate,
    Graph,
    HumanNode,
    LLMNode,
    Node,
    ScriptNode,
    Semaphores,
)
from .errors import GraphValidationError
from .providers import known_providers
from .validation import validate

CONTRACT_VERSION = 1

_HELPERS_BEGIN = "# --- gw:helpers ---"
_HELPERS_END = "# --- gw:end-helpers ---"
_GRAPH_BEGIN = "# --- gw:graph ---"
_GRAPH_END = "# --- gw:end-graph ---"
_GUI_BEGIN = "# --- gw:gui ---"
_GUI_END = "# --- gw:end-gui ---"

_BARE_TYPES = {"str", "int", "float", "bool", "dict", "list", "tuple", "set", "bytes"}


# --------------------------------------------------------------------------- #
# Capture: execute a graph definition with run() intercepted
# --------------------------------------------------------------------------- #


class _Captured(BaseException):
    """Raised by the intercepted run(); BaseException so a definition's own
    ``except Exception`` around run() cannot swallow the capture."""

    def __init__(self, graph: Graph, namespace: dict):
        self.graph = graph
        self.namespace = namespace


def capture(path) -> tuple[Graph, dict]:
    """Execute the graph definition at ``path`` as ``__main__`` with the
    runtime entry points intercepted; returns (graph, namespace). A
    definition that never calls ``run()`` is scanned for a Graph binding."""
    import runpy

    import graph_workflow
    from . import engine

    path = Path(path).resolve()

    def _fake_run(graph, **kwargs):
        if not isinstance(graph, Graph):
            raise TypeError(f"run() called with {graph!r}, not a Graph")
        ns = kwargs.get("namespace")
        if ns is None:
            ns = sys._getframe(1).f_globals
        raise _Captured(graph, dict(ns))

    def _fake_resume(graph, trace_path, **kwargs):
        return _fake_run(graph, **kwargs)

    def _fake_runtime_run(self):
        raise _Captured(self.graph, dict(self.namespace))

    originals = (
        graph_workflow.run, engine.run,
        graph_workflow.resume_run, engine.resume_run,
        engine.Runtime.run,
    )
    graph_workflow.run = engine.run = _fake_run
    graph_workflow.resume_run = engine.resume_run = _fake_resume
    engine.Runtime.run = _fake_runtime_run
    try:
        # The definition executes as __main__, so its own prints fire; stdout
        # must stay pure JSON for the GUI, so they are diverted to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            module_ns = runpy.run_path(str(path), run_name="__main__")
    except _Captured as c:
        return c.graph, c.namespace
    finally:
        (graph_workflow.run, engine.run,
         graph_workflow.resume_run, engine.resume_run,
         engine.Runtime.run) = originals
    graphs = [v for v in module_ns.values() if isinstance(v, Graph)]
    if not graphs:
        raise RuntimeError(
            f"{path.name} neither called run() nor bound a Graph at module level"
        )
    return max(graphs, key=lambda g: len(g.nodes)), dict(module_ns)


# --------------------------------------------------------------------------- #
# Contract serialization (Graph -> JSON)
# --------------------------------------------------------------------------- #


def _type_str(t) -> str:
    if isinstance(t, str):
        return t
    return getattr(t, "__name__", repr(t))


def _callable_source(fn) -> str | None:
    try:
        src = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return None
    if src.lstrip().startswith("def "):
        try:
            compile(src, "<code>", "exec")
        except SyntaxError:
            return None
        return src.strip("\n") + "\n"
    # A lambda: getsource returns the whole enclosing statement — recover the
    # expression itself, but only when it is unambiguous.
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    lambdas = [n for n in ast.walk(tree) if isinstance(n, ast.Lambda)]
    if len(lambdas) != 1:
        return None
    seg = ast.get_source_segment(src, lambdas[0])
    if seg is None:
        return None
    try:
        compile(seg, "<code>", "eval")
    except SyntaxError:
        return None
    return seg.strip()


def _callable_ref(fn, namespace: dict) -> str | None:
    name = getattr(fn, "__name__", None)
    if name and name != "<lambda>" and namespace.get(name) is fn:
        return name
    return None


def _gate_json(g: Gate) -> dict:
    d: dict = {"expression": g.expression}
    if g.p_invocation is not None:
        d["p_invocation"] = g.p_invocation
    return d


def _prompt_fields(prompt, namespace: dict) -> dict:
    if isinstance(prompt, str) or prompt is None:
        return {"prompt": prompt, "prompt_ref": None}
    ref = _callable_ref(prompt, namespace)
    return {
        "prompt": None,
        "prompt_ref": ref or repr(prompt),
        "prompt_opaque": ref is None,
    }


def node_to_contract(node: Node, namespace: dict) -> dict:
    d: dict = {
        "id": node.id,
        "rank": node.rank,
        "type": node.type,
        "inputs": {k: _type_str(v) for k, v in node.inputs.items()},
        "outputs": {k: _type_str(v) for k, v in node.outputs.items()},
        "gate": node.gate,
        "output_expression": node.output_expression,
    }
    if isinstance(node, ScriptNode):
        code = _callable_source(node.fn)
        ref = _callable_ref(node.fn, namespace)
        d["code"] = code
        d["code_ref"] = ref
        if code is None and ref is None:
            d["code_opaque"] = repr(node.fn)
    elif isinstance(node, LLMNode):
        d.update(_prompt_fields(node.prompt, namespace))
        d.update(
            provider=node.provider,
            model=node.model,
            effort=node.effort,
            settings=node.settings,
            cwd=node.cwd,
            extra_args=list(node.extra_args),
            timeout_s=node.timeout_s,
            pause_message=node.pause_message,
            resume_message=node.resume_message,
        )
        if node.parse is not None:
            d["parse_ref"] = _callable_ref(node.parse, namespace) or repr(node.parse)
    elif isinstance(node, HumanNode):
        d.update(_prompt_fields(node.prompt, namespace))
        d.update(shell_cmd=node.shell_cmd, timeout_s=node.timeout_s)
    return d


def graph_to_contract(graph: Graph, namespace: dict) -> dict:
    return {
        "name": graph.name,
        "semaphores": graph.semaphores.to_json(),
        "pause_message": graph.pause_message,
        "resume_message": graph.resume_message,
        "nodes": [node_to_contract(n, namespace) for n in graph.nodes_by_rank()],
        "edges": [
            {
                "id": e.id,
                "inputs": list(e.inputs),
                "outputs": list(e.outputs),
                "universal_gate": _gate_json(e.universal_gate),
                "input_gates": {n: _gate_json(g) for n, g in e.input_gates.items()},
                "output_gates": {n: _gate_json(g) for n, g in e.output_gates.items()},
            }
            for e in graph.edges.values()
        ],
    }


def _extract_section(text: str, begin: str, end: str) -> str | None:
    try:
        i = text.index(begin) + len(begin)
        j = text.index(end, i)
    except ValueError:
        return None
    return textwrap.dedent(text[i:j]).strip("\n")


def _gui_meta(contract: dict) -> dict | None:
    """Monitor-only display metadata (node labels, the default-label
    template) destined for the definition's gw:gui comment section. The
    runtime never sees any of it: the section is pure comments."""
    g = contract.get("graph") or {}
    meta: dict = {}
    tmpl = g.get("label_template")
    if isinstance(tmpl, str) and tmpl.strip():
        meta["label_template"] = tmpl.strip()
    labels = {
        n["id"]: n["label"].strip()
        for n in g.get("nodes", [])
        if isinstance(n.get("label"), str) and n["label"].strip()
    }
    if labels:
        meta["labels"] = labels
    return meta or None


def _parse_gui_meta(text: str) -> dict:
    section = _extract_section(text, _GUI_BEGIN, _GUI_END)
    if not section:
        return {}
    payload = "\n".join(re.sub(r"^\s*#\s?", "", ln) for ln in section.splitlines())
    try:
        meta = json.loads(payload)
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}


_GW_CLASSES = {
    "Graph", "LLMNode", "ScriptNode", "HumanNode", "Edge", "Gate",
    "Semaphores", "Generator",
}


def _carve_helpers(text: str, graph: Graph, namespace: dict) -> str:
    """Best-effort helpers extraction from an unstructured definition:
    every top-level statement except the pieces the structured regeneration
    re-emits (module docstring, graph_workflow imports, graph-element
    construction, registration and run calls, and the ``__main__`` block).
    Seeding the contract's helpers section with this keeps the prompt
    callables, script functions, and expression helpers a handwritten file
    defines from being silently discarded on the first structured save."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ""
    lines = text.splitlines()
    graph_names = {k for k, v in namespace.items() if v is graph}

    def call_name(call: ast.Call) -> str | None:
        if isinstance(call.func, ast.Name):
            return call.func.id
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        return None

    def is_boilerplate(stmt: ast.stmt) -> bool:
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            return True  # module docstring
        if isinstance(stmt, ast.ImportFrom):
            return (stmt.module or "").split(".")[0] == "graph_workflow"
        if isinstance(stmt, ast.Import):
            return all(a.name.split(".")[0] == "graph_workflow" for a in stmt.names)
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and isinstance(
            getattr(stmt, "value", None), ast.Call
        ):
            if call_name(stmt.value) in _GW_CLASSES:
                return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            f = stmt.value.func
            if (
                isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id in graph_names
                and f.attr in {"add_node", "add_nodes", "add_edge", "add_edges"}
            ):
                return True
            if isinstance(f, ast.Name) and f.id in {"run", "resume_run"}:
                return True
        if isinstance(stmt, ast.If):
            t = stmt.test
            if (
                isinstance(t, ast.Compare)
                and isinstance(t.left, ast.Name)
                and t.left.id == "__name__"
            ):
                return True
        return False

    def segment(stmt: ast.stmt) -> str:
        start = stmt.lineno
        for dec in getattr(stmt, "decorator_list", None) or []:
            start = min(start, dec.lineno)
        return "\n".join(lines[start - 1 : stmt.end_lineno])

    kept = [segment(s).rstrip() for s in tree.body if not is_boilerplate(s)]
    return "\n\n".join(k for k in kept if k).strip("\n")


def load_contract(path) -> dict:
    path = Path(path).resolve()
    graph, namespace = capture(path)
    try:
        warnings = validate(graph, namespace)
        errors: list[str] = []
    except GraphValidationError as exc:
        errors = [str(p) for p in exc.problems]
        warnings = []
    text = path.read_text(encoding="utf-8")
    structured = _GRAPH_BEGIN in text
    if structured:
        helpers = _extract_section(text, _HELPERS_BEGIN, _HELPERS_END)
    else:
        helpers = _carve_helpers(text, graph, namespace)
    contract = {
        "ok": True,
        "version": CONTRACT_VERSION,
        "path": str(path),
        "structured": structured,
        "helpers": helpers or "",
        "graph": graph_to_contract(graph, namespace),
        "errors": errors,
        "warnings": list(warnings),
        "providers": known_providers(),
    }
    gui_meta = _parse_gui_meta(text)
    if gui_meta:
        gmodel = contract["graph"]
        tmpl = gui_meta.get("label_template")
        if isinstance(tmpl, str) and tmpl.strip():
            gmodel["label_template"] = tmpl.strip()
        labels = gui_meta.get("labels")
        if isinstance(labels, dict):
            for nd in gmodel["nodes"]:
                lab = labels.get(nd["id"])
                if isinstance(lab, str) and lab.strip():
                    nd["label"] = lab.strip()
    return contract


# --------------------------------------------------------------------------- #
# Code generation (JSON -> structured executable definition)
# --------------------------------------------------------------------------- #


def _lit(value) -> str:
    """A Python literal for a JSON-shaped value."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_lit(k)}: {_lit(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_lit(v) for v in value) + "]"
    return repr(value)


def _io_dict(mapping: dict) -> str:
    items = []
    for name, tname in (mapping or {}).items():
        t = tname if tname in _BARE_TYPES else repr(tname)
        items.append(f"{name!r}: {t}")
    return "{" + ", ".join(items) + "}"


def _gate_literal(g: dict | None) -> str | None:
    if not g:
        return None
    expr = (g.get("expression") or "True").strip()
    p = g.get("p_invocation")
    if p is None:
        return None if expr == "True" else repr(expr)
    return f"Gate({expr!r}, p_invocation={p!r})"


def _fn_name_for(code: str, node_id: str, lines: list[str]) -> str:
    """Emit ``code`` (a def or a lambda expression) and return the name that
    references it."""
    code = textwrap.dedent(code).strip("\n")
    compile(code, f"<{node_id} code>", "exec")  # refuse to write broken code
    m = re.match(r"def\s+([A-Za-z_]\w*)\s*\(", code)
    if m:
        lines.append(code)
        lines.append("")
        return m.group(1)
    name = f"fn_{node_id}"
    lines.append(f"{name} = {code}")
    lines.append("")
    return name


def _kwargs(pairs: list[tuple[str, str | None]]) -> str:
    return ", ".join(f"{k}={v}" for k, v in pairs if v is not None)


def _common_node_kwargs(n: dict) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    if n.get("inputs"):
        out.append(("inputs", _io_dict(n["inputs"])))
    if n.get("outputs"):
        out.append(("outputs", _io_dict(n["outputs"])))
    if n.get("gate"):
        out.append(("gate", repr(n["gate"])))
    if n.get("output_expression"):
        out.append(("output_expression", repr(n["output_expression"])))
    return out


def generate(contract: dict) -> str:
    g = contract["graph"]
    name = g.get("name") or "untitled"
    helpers = (contract.get("helpers") or "").strip("\n")
    body: list[str] = []

    sem = g.get("semaphores") or {}
    sem_pairs = [
        ("script", repr(int(sem.get("script", 4)))),
        ("llm", repr(int(sem.get("llm", 2)))),
        ("human", repr(int(sem.get("human", 1)))),
    ]
    if sem.get("providers"):
        sem_pairs.append(("providers", _lit(sem["providers"])))
    graph_pairs = [("semaphores", f"Semaphores({_kwargs(sem_pairs)})")]
    if g.get("pause_message"):
        graph_pairs.append(("pause_message", repr(g["pause_message"])))
    if g.get("resume_message"):
        graph_pairs.append(("resume_message", repr(g["resume_message"])))
    body.append(f"g = Graph({name!r}, {_kwargs(graph_pairs)})")
    body.append("")

    node_ids: list[str] = []
    for n in sorted(g.get("nodes", []), key=lambda n: n["rank"]):
        nid = n["id"]
        node_ids.append(nid)
        ntype = n["type"]
        pairs: list[tuple[str, str | None]] = []
        if ntype == "script":
            if n.get("code"):
                fn = _fn_name_for(n["code"], nid, body)
            elif n.get("code_ref"):
                fn = n["code_ref"]
            else:
                raise ValueError(f"script node {nid!r}: no code or code_ref to save")
            pairs.append(("fn", fn))
            pairs += _common_node_kwargs(n)
            body.append(f"{nid} = ScriptNode({nid!r}, rank={n['rank']}, {_kwargs(pairs)})")
        elif ntype == "llm":
            pairs += [
                ("provider", repr(n.get("provider") or "claude")),
                ("model", repr(n.get("model") or "")),
            ]
            if n.get("effort"):
                pairs.append(("effort", repr(n["effort"])))
            if n.get("prompt") is not None:
                pairs.append(("prompt", repr(n["prompt"])))
            elif n.get("prompt_ref") and not n.get("prompt_opaque"):
                pairs.append(("prompt", n["prompt_ref"]))
            else:
                raise ValueError(f"LLM node {nid!r}: no prompt to save")
            for key in ("settings", "cwd", "pause_message", "resume_message"):
                if n.get(key):
                    pairs.append((key, repr(n[key])))
            if n.get("extra_args"):
                pairs.append(("extra_args", _lit(n["extra_args"])))
            if n.get("timeout_s") != 1800.0 and "timeout_s" in n:
                pairs.append(("timeout_s", repr(n["timeout_s"])))
            if n.get("parse_ref"):
                pairs.append(("parse", n["parse_ref"]))
            pairs += _common_node_kwargs(n)
            body.append(f"{nid} = LLMNode({nid!r}, rank={n['rank']}, {_kwargs(pairs)})")
        elif ntype == "human":
            if n.get("prompt") is not None:
                pairs.append(("prompt", repr(n["prompt"])))
            elif n.get("prompt_ref") and not n.get("prompt_opaque"):
                pairs.append(("prompt", n["prompt_ref"]))
            else:
                raise ValueError(f"human node {nid!r}: no prompt to save")
            if n.get("shell_cmd"):
                pairs.append(("shell_cmd", repr(n["shell_cmd"])))
            if n.get("timeout_s") is not None:
                pairs.append(("timeout_s", repr(n["timeout_s"])))
            pairs += _common_node_kwargs(n)
            body.append(f"{nid} = HumanNode({nid!r}, rank={n['rank']}, {_kwargs(pairs)})")
        else:
            raise ValueError(f"node {nid!r}: unknown type {ntype!r}")
    if node_ids:
        body.append("")
        body.append(f"g.add_nodes({', '.join(node_ids)})")

    def endpoint(nid: str) -> str:
        return "g.generator" if nid == GENERATOR_ID else nid

    edge_lines: list[str] = []
    for e in g.get("edges", []):
        pairs = [
            ("inputs", "[" + ", ".join(endpoint(i) for i in e["inputs"]) + "]"),
            ("outputs", "[" + ", ".join(endpoint(o) for o in e["outputs"]) + "]"),
        ]
        for role in ("input_gates", "output_gates"):
            gates = {
                nid: _gate_literal(gd)
                for nid, gd in (e.get(role) or {}).items()
                if _gate_literal(gd) is not None
            }
            if gates:
                pairs.append((role, "{" + ", ".join(f"{k!r}: {v}" for k, v in gates.items()) + "}"))
        ug = _gate_literal(e.get("universal_gate"))
        if ug is not None:
            pairs.append(("universal_gate", ug))
        edge_lines.append(f"    Edge({e['id']!r}, {_kwargs(pairs)}),")
    if edge_lines:
        body.append("g.add_edges(")
        body.extend(edge_lines)
        body.append(")")

    doc = (
        f'"""{name} — graph-workflow graph definition.\n\n'
        "Generated by the Monitor GUI (structured format v1). The gw:graph\n"
        "section is regenerated on every save; the gw:helpers section is\n"
        'preserved verbatim and is yours to edit."""'
    )
    # Monitor-only display metadata rides as a comment section the runtime
    # never parses (node labels, the default-label template).
    meta = _gui_meta(contract)
    gui_section = (
        [_GUI_BEGIN, "# " + json.dumps(meta, ensure_ascii=False), _GUI_END, ""]
        if meta
        else []
    )
    parts = [
        doc,
        "",
        "from graph_workflow import (",
        "    Edge, Gate, Graph, HumanNode, LLMNode, ScriptNode, Semaphores,",
        "    isDefined, run,",
        ")",
        "",
        _HELPERS_BEGIN,
        helpers,
        _HELPERS_END,
        "",
        _GRAPH_BEGIN,
        *body,
        _GRAPH_END,
        "",
        *gui_section,
        'if __name__ == "__main__":',
        "    import os",
        "",
        "    run(",
        "        g,",
        '        seed=int(os.environ["GW_SEED"]) if os.environ.get("GW_SEED") else None,',
        '        run_dir=os.environ.get("GW_RUN_DIR") or None,',
        "    )",
        "",
    ]
    return "\n".join(parts)


def save_contract(path, contract: dict, force: bool = False) -> dict:
    path = Path(path).resolve()
    text = generate(contract)
    compile(text, str(path), "exec")
    if path.exists() and not force:
        existing = path.read_text(encoding="utf-8")
        if existing.strip() and _GRAPH_BEGIN not in existing:
            raise RuntimeError(
                f"{path.name} exists and is not in the structured format; "
                "saving over it would discard handwritten Python (use force)"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Prove the regenerated definition executes before touching the target:
    # compile() only catches syntax, not a graph section referencing names
    # the helpers section no longer defines. The candidate runs from a temp
    # file and atomically replaces the target only on success, so a failed
    # save can never leave a broken definition on disk.
    tmp = path.with_name(path.name + ".gw-tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    try:
        try:
            result = load_contract(tmp)
        except Exception as exc:
            raise RuntimeError(
                f"regenerated definition failed to execute "
                f"({type(exc).__name__}: {exc}); {path.name} was left untouched. "
                "Check that the helpers section defines every name the graph "
                "references."
            ) from exc
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    result["path"] = str(path)
    return result


def template(name: str = "untitled") -> dict:
    return {
        "ok": True,
        "version": CONTRACT_VERSION,
        "path": None,
        "structured": True,
        "helpers": "",
        "graph": {
            "name": name,
            "semaphores": Semaphores().to_json(),
            "pause_message": None,
            "resume_message": None,
            "nodes": [],
            "edges": [],
        },
        "errors": [],
        "warnings": [],
        "providers": known_providers(),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")

    def out(payload: dict) -> int:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.flush()
        return 0 if payload.get("ok") else 1

    def fail(exc: BaseException) -> int:
        import traceback

        return out(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=8),
            }
        )

    if not argv:
        return out({"ok": False, "error": "usage: gui load|validate|save|template ..."})
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "load":
            return out(load_contract(args[0]))
        if cmd == "validate":
            c = load_contract(args[0])
            return out(
                {
                    "ok": not c["errors"],
                    "errors": c["errors"],
                    "warnings": c["warnings"],
                }
            )
        if cmd == "save":
            force = "--force" in args
            target = [a for a in args if not a.startswith("--")][0]
            contract = json.loads(sys.stdin.buffer.read().decode("utf-8"))
            return out(save_contract(target, contract, force=force))
        if cmd == "template":
            return out(template(args[0] if args else "untitled"))
        return out({"ok": False, "error": f"unknown command {cmd!r}"})
    except BaseException as exc:  # noqa: BLE001 — everything must land as JSON
        return fail(exc)


if __name__ == "__main__":
    sys.exit(main())
