"""Expression evaluation: gating expressions, output expressions, seeded draws.

Gating expressions are strings of ordinary Python, evaluated at need via
``eval``. The evaluation namespace is the graph definition's own namespace —
imports and helper functions included — augmented with the state bindings only
the runtime can supply: node handles (exposing declared outputs and runtime
metrics as attributes), in-edge terms bound by edge id (node gating
expressions only), the gate's own seeded draw (``sampled_value``, edge gates
only), and ``isDefined``/``UNDEFINED``. The runtime defines no gate-function
library.

Every stochastic draw is taken from the drawing gate's own RNG stream,
derived from the run seed plus the gate's ID, so outcomes are deterministic
over the seed regardless of the wall-clock ordering of concurrent draws.
Draws are redrawn at every commit of the gate's input nodes; evaluation never
advances a stream.

Expression defaults are computed natively (no eval): a ``None`` node gate is
the AND of all in-edge terms, a ``None`` output expression is the AND of
``isDefined(output)`` over the declared outputs, and the identity gate
``True`` short-circuits.
"""

from __future__ import annotations

import ast
import random
from typing import Any

from .model import (
    GENERATOR_ID,
    UNDEFINED,
    Edge,
    Gate,
    Generator,
    Graph,
    Node,
    isDefined,
)

# --------------------------------------------------------------------------- #
# Handles
# --------------------------------------------------------------------------- #


class NodeHandle:
    """Read-only view of one node bound into expression namespaces: declared
    outputs and runtime metrics as attributes (``A.X``, ``A.context_size``)."""

    __slots__ = ("_decl", "_st")

    def __init__(self, decl: Node, st):
        object.__setattr__(self, "_decl", decl)
        object.__setattr__(self, "_st", st)

    def __getattr__(self, name):
        decl = object.__getattribute__(self, "_decl")
        st = object.__getattribute__(self, "_st")
        if name in decl.outputs:
            return st.outputs.get(name, UNDEFINED)
        if name == "id":
            return decl.id
        if name == "rank":
            return decl.rank
        if name == "state":
            # The lifecycle has exactly three states; a pause-interrupted
            # node is mid-invocation, so expressions see it as running (the
            # monitor's status surface keeps the finer distinction).
            return "running" if st.state == "paused" else st.state
        if name == "output_state":
            return st.output_state
        if name == "invocations":
            return st.invocations
        if name == "commits":
            return st.commits
        if name == "last_commit_seq":
            return st.last_commit_seq
        if name in ("context_size", "duration_s", "usage", "session_id"):
            return st.metrics.get(name, _METRIC_DEFAULTS[name])
        raise AttributeError(
            f"node {decl.id!r} has no declared output or runtime metric {name!r}"
        )

    def __setattr__(self, name, value):
        raise AttributeError("node handles are read-only")

    def __repr__(self):
        return f"<handle {object.__getattribute__(self, '_decl').id}>"


_METRIC_DEFAULTS = {"context_size": 0, "duration_s": 0.0, "usage": {}, "session_id": None}


class GeneratorHandle:
    """The Generator exposes a single property: its output state."""

    __slots__ = ("_get",)

    def __init__(self, get_state):
        object.__setattr__(self, "_get", get_state)

    def __getattr__(self, name):
        if name == "output_state":
            return object.__getattribute__(self, "_get")()
        if name == "id":
            return GENERATOR_ID
        raise AttributeError(f"the Generator has no property {name!r}")

    def __setattr__(self, name, value):
        raise AttributeError("the Generator handle is read-only")


# --------------------------------------------------------------------------- #
# Seeded draws
# --------------------------------------------------------------------------- #


class DrawState:
    """One RNG stream per gate, seeded from ``run_seed`` plus the gate ID.

    Per-node gates draw independently; an edge's universal gate has a single
    stream whose current value is shared across the whole edge. Draw #0 is
    taken for every gate at run start; thereafter a gate redraws at every
    commit of its edge's input nodes.
    """

    def __init__(self, run_seed: int):
        self.run_seed = run_seed
        self._rngs: dict[str, random.Random] = {}
        self.index: dict[str, int] = {}
        self.value: dict[str, float] = {}

    def register(self, gate: Gate):
        gid = gate.gate_id
        if gid in self._rngs:
            return
        # str seeding hashes via SHA-512 in CPython — stable across runs and platforms.
        self._rngs[gid] = random.Random(f"{self.run_seed}:{gid}")
        self.index[gid] = 0
        self.value[gid] = self._rngs[gid].random()

    def redraw(self, gate: Gate) -> tuple[int, float]:
        gid = gate.gate_id
        self.index[gid] += 1
        self.value[gid] = self._rngs[gid].random()
        return self.index[gid], self.value[gid]

    def fast_forward(self, gate_id: str, index: int):
        """Advance a stream to ``index`` (used by trace replay)."""
        while self.index[gate_id] < index:
            self.index[gate_id] += 1
            self.value[gate_id] = self._rngs[gate_id].random()


# --------------------------------------------------------------------------- #
# Evaluator
# --------------------------------------------------------------------------- #


class ExpressionError(Exception):
    """Wraps an exception raised inside an authored expression."""

    def __init__(self, where: str, expr: str, cause: BaseException):
        self.where, self.expr, self.cause = where, expr, cause
        super().__init__(f"{where}: {type(cause).__name__}: {cause} in {expr!r}")


class Evaluator:
    """Evaluates gates, edge terms, node gating expressions, and output
    expressions against live run state. Pure over (namespace, node state,
    current draws): evaluation stores no truth values and advances no
    streams."""

    def __init__(self, graph: Graph, namespace: dict, states: dict, draws: DrawState,
                 gen_state, warn):
        self.graph = graph
        self.namespace = namespace  # the graph definition's own namespace (live)
        self.states = states
        self.draws = draws
        self.warn = warn  # callable(str) -> None; expression errors are warnings
        self._code: dict[tuple[str, str], Any] = {}
        self.handles: dict[str, Any] = {
            nid: NodeHandle(decl, states[nid]) for nid, decl in graph.nodes.items()
        }
        self.handles[GENERATOR_ID] = GeneratorHandle(gen_state)

    # -- plumbing ---------------------------------------------------------- #

    def compile(self, expr: str, where: str):
        key = (expr, "eval")
        code = self._code.get(key)
        if code is None:
            code = compile(expr, f"<{where}>", "eval")
            self._code[key] = code
        return code

    def _eval(self, expr: str, where: str, extras: dict) -> Any:
        ns: dict[str, Any] = {}
        ns.update(self.namespace)
        ns.update(self.handles)
        ns.setdefault("isDefined", isDefined)
        ns["UNDEFINED"] = UNDEFINED
        ns.update(extras)
        return eval(self.compile(expr, where), ns)  # noqa: S307 — the expression language is Python by design

    def _eval_bool(self, expr: str, where: str, extras: dict) -> bool:
        try:
            return bool(self._eval(expr, where, extras))
        except Exception as exc:  # authored expressions may do anything
            self.warn(
                f"expression error in {where}: {type(exc).__name__}: {exc} "
                f"(expr {expr!r}) — treated as False"
            )
            return False

    # -- gates and terms --------------------------------------------------- #

    def eval_gate(self, gate: Gate) -> bool:
        if gate.is_default:
            return True
        extras: dict[str, Any] = {"sampled_value": self.draws.value[gate.gate_id]}
        if gate.p_invocation is not None:
            extras["p_invocation"] = gate.p_invocation
        return self._eval_bool(gate.expression, f"gate {gate.gate_id}", extras)

    def _element_output_state(self, element_id: str):
        if element_id == GENERATOR_ID:
            return self.handles[GENERATOR_ID].output_state
        return self.states[element_id].output_state

    def edge_term(self, edge: Edge, target_id: str) -> bool:
        """The one term this edge contributes to ``target_id``'s gating
        expression: universal gate AND the target's output gate AND, for every
        input node, its input gate AND its output state. A FALSE gate
        transfers nothing and defines the term as FALSE; an unset input
        output state makes the term FALSE."""
        if not self.eval_gate(edge.universal_gate):
            return False
        if not self.eval_gate(edge.output_gates[target_id]):
            return False
        for nid in edge.inputs:
            if not self.eval_gate(edge.input_gates[nid]):
                return False
            state = self._element_output_state(nid)
            if state is UNDEFINED or not state:
                return False
        return True

    def node_gate(self, node: Node) -> tuple[bool, dict[str, bool]]:
        """Evaluate a node's gating expression over its in-edges. Returns
        (result, terms-by-edge-id). Default gate: AND of all in-edge terms
        (a node with no in-edges is never ready)."""
        terms = {e.id: self.edge_term(e, node.id) for e in self.graph.in_edges(node.id)}
        if node.gate is None:
            return (bool(terms) and all(terms.values()), terms)
        result = self._eval_bool(node.gate, f"node {node.id} gate", dict(terms))
        return (result, terms)

    def output_state_value(self, node: Node, outputs: dict) -> bool:
        """Evaluate the node's output expression over its declared outputs —
        this evaluation is the commit; the boolean is the output state."""
        bound = {name: outputs.get(name, UNDEFINED) for name in node.outputs}
        if node.output_expression is None:
            return all(isDefined(v) for v in bound.values())
        return self._eval_bool(
            node.output_expression, f"node {node.id} output expression", bound
        )


# --------------------------------------------------------------------------- #
# Static analysis
# --------------------------------------------------------------------------- #


def referenced_names(expr: str) -> set[str]:
    """Bare names referenced by an expression (for dependency mapping).
    Unparseable expressions return the empty set — the syntax error surfaces
    via validation/evaluation instead."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return set()
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def dependency_map(graph: Graph) -> dict[str, set[str]]:
    """element id -> set of node ids whose gating expressions reference it.

    A node's gating expression references: every input node of its in-edges,
    every name mentioned in those edges' gate expressions, and every name
    mentioned in the node's own gating expression that is a node id. When an
    element commits, exactly the gating expressions of ``dependency_map[id]``
    are re-evaluated.
    """
    ids = set(graph.nodes) | {GENERATOR_ID}
    deps: dict[str, set[str]] = {eid: set() for eid in ids}
    for node in graph.nodes.values():
        referenced: set[str] = set()
        for edge in graph.in_edges(node.id):
            referenced.update(edge.inputs)
            for gate in [edge.universal_gate, *edge.input_gates.values()]:
                referenced.update(referenced_names(gate.expression) & ids)
            referenced.update(referenced_names(edge.output_gates[node.id].expression) & ids)
        if node.gate is not None:
            referenced.update(referenced_names(node.gate) & ids)
        for ref in referenced:
            deps[ref].add(node.id)
    return deps
