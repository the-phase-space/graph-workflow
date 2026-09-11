"""Core model: Graph, nodes, edges, gates, the Generator, and semaphores.

Everything here is static declaration — topology as data. All mutable runtime
state lives in the engine's per-run structures (`engine.NodeState`), never on
these objects, so a Graph can be run any number of times.
"""

from __future__ import annotations

import keyword
from typing import Any, Callable

from .errors import GraphDefinitionError

# --------------------------------------------------------------------------- #
# UNDEFINED sentinel
# --------------------------------------------------------------------------- #


class _Undefined:
    """Singleton for 'no value yet'. Falsy, so unset output states and
    undefined outputs never satisfy a gate on their own."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self):
        return False

    def __repr__(self):
        return "UNDEFINED"


UNDEFINED = _Undefined()


def isDefined(value) -> bool:
    """The default term of every output expression: has this output been set?"""
    return value is not UNDEFINED


#: Attribute names every node handle exposes as runtime metrics. Declared
#: output names may not collide with these (validation enforces it).
RESERVED_METRICS = frozenset(
    {
        "id",
        "rank",
        "state",
        "output_state",
        "invocations",
        "commits",
        "context_size",
        "duration_s",
        "usage",
        "session_id",
        "last_commit_seq",
    }
)

GENERATOR_ID = "GEN"


def _require_identifier(value: str, what: str) -> str:
    if not isinstance(value, str) or not value.isidentifier() or keyword.iskeyword(value):
        raise GraphDefinitionError(
            f"{what} must be a valid non-keyword Python identifier, got {value!r}"
        )
    return value


def _type_name(t: Any) -> str:
    if isinstance(t, str):
        return t
    return getattr(t, "__name__", repr(t))


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #


class Gate:
    """A gating expression attached to an edge.

    ``expression`` is a string of ordinary Python, evaluated by the runtime in
    the graph definition's namespace augmented with node handles, the gate's
    own seeded draw (``sampled_value``), and — when declared here —
    ``p_invocation``. A gate left unspecified defaults to the constant True,
    the identity gate.
    """

    __slots__ = ("expression", "p_invocation", "gate_id")

    def __init__(self, expression: str = "True", p_invocation: float | None = None):
        if not isinstance(expression, str):
            raise GraphDefinitionError(f"gate expression must be a string, got {expression!r}")
        self.expression = expression
        self.p_invocation = p_invocation
        self.gate_id: str | None = None  # assigned when the edge is attached to a graph

    @classmethod
    def coerce(cls, value) -> "Gate":
        if value is None:
            return cls()
        if isinstance(value, Gate):
            return value
        if isinstance(value, str):
            return cls(value)
        raise GraphDefinitionError(f"cannot interpret {value!r} as a gate")

    @property
    def is_default(self) -> bool:
        return self.expression.strip() == "True" and self.p_invocation is None

    def to_json(self):
        d = {"expression": self.expression, "id": self.gate_id}
        if self.p_invocation is not None:
            d["p_invocation"] = self.p_invocation
        return d

    def __repr__(self):
        return f"Gate({self.expression!r})"


# --------------------------------------------------------------------------- #
# Edge
# --------------------------------------------------------------------------- #


class Edge:
    """A first-class edge: N input nodes, M output nodes, one gate per
    connected node, and one universal gate over all input nodes.

    The edge contributes exactly one term to each output node's gating
    expression. The term delivered to output node ``o`` is the conjunction

        universal_gate AND output_gate(o) AND
        AND over inputs i of (input_gate(i) AND output_state(i))

    so with every gate at the default the edge is pure feedforward: an N-to-M
    edge behaves identically to N×M separate 1:1 edges (under the default
    node gating expression, the AND of in-edge terms), and the minimal edge
    passes its source's output state through unchanged. An input whose output
    state is unset makes the term FALSE.

    Edges are static: nothing on an Edge changes at runtime.
    """

    def __init__(
        self,
        id: str,
        inputs,
        outputs,
        input_gates: dict[str, Any] | None = None,
        output_gates: dict[str, Any] | None = None,
        universal_gate: Any = None,
    ):
        self.id = _require_identifier(id, "edge id")
        self.inputs: list[str] = [self._node_id(n) for n in _as_list(inputs, "edge inputs")]
        self.outputs: list[str] = [self._node_id(n) for n in _as_list(outputs, "edge outputs")]
        if not self.inputs:
            raise GraphDefinitionError(f"edge {id!r} has no input nodes")
        if not self.outputs:
            raise GraphDefinitionError(f"edge {id!r} has no output nodes")
        if len(set(self.inputs)) != len(self.inputs):
            raise GraphDefinitionError(f"edge {id!r} lists a duplicate input node")
        if len(set(self.outputs)) != len(self.outputs):
            raise GraphDefinitionError(f"edge {id!r} lists a duplicate output node")

        def own(value) -> Gate:
            # Each gate belongs to exactly one edge position — its ID seeds
            # its independent RNG stream — so a Gate instance reused across
            # edges/roles is cloned rather than shared.
            g = Gate.coerce(value)
            if g.gate_id is not None:
                g = Gate(g.expression, g.p_invocation)
            return g

        self.input_gates: dict[str, Gate] = {}
        for nid, g in (input_gates or {}).items():
            nid = self._node_id(nid)
            if nid not in self.inputs:
                raise GraphDefinitionError(f"edge {id!r}: input gate for non-input node {nid!r}")
            self.input_gates[nid] = own(g)
        self.output_gates: dict[str, Gate] = {}
        for nid, g in (output_gates or {}).items():
            nid = self._node_id(nid)
            if nid not in self.outputs:
                raise GraphDefinitionError(f"edge {id!r}: output gate for non-output node {nid!r}")
            self.output_gates[nid] = own(g)
        # Gates are never absent: fill the unspecified ones with the identity gate.
        for nid in self.inputs:
            self.input_gates.setdefault(nid, Gate())
        for nid in self.outputs:
            self.output_gates.setdefault(nid, Gate())
        self.universal_gate: Gate = own(universal_gate)

        # Stable gate IDs — these seed each gate's RNG stream.
        self.universal_gate.gate_id = f"{self.id}::univ"
        for nid, g in self.input_gates.items():
            g.gate_id = f"{self.id}::in::{nid}"
        for nid, g in self.output_gates.items():
            g.gate_id = f"{self.id}::out::{nid}"

    @staticmethod
    def _node_id(n) -> str:
        if isinstance(n, str):
            return n
        if isinstance(n, (Node, Generator)):
            return n.id
        raise GraphDefinitionError(f"cannot interpret {n!r} as a node reference")

    def all_gates(self) -> list[Gate]:
        return [self.universal_gate, *self.input_gates.values(), *self.output_gates.values()]

    def to_json(self):
        return {
            "id": self.id,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "universal_gate": self.universal_gate.to_json(),
            "input_gates": {n: g.to_json() for n, g in self.input_gates.items()},
            "output_gates": {n: g.to_json() for n, g in self.output_gates.items()},
        }

    def __repr__(self):
        return f"Edge({self.id!r}, {self.inputs} -> {self.outputs})"


def _as_list(value, what) -> list:
    if value is None:
        raise GraphDefinitionError(f"{what} may not be None")
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #


class Node:
    """Base declaration shared by all three functional node types.

    ``gate`` — the node's gating expression over its in-edges. In-edge terms
    are bound by edge id. ``None`` means the default: the conjunction (AND) of
    all in-edge terms.

    ``output_expression`` — a boolean expression over the node's declared
    outputs, evaluated when an invocation ends; that evaluation is the commit
    and its boolean result is the node's output state. ``None`` means the
    default: the AND of ``isDefined(output)`` over every declared output
    (vacuously True for a node with no declared outputs).
    """

    type: str = "abstract"

    def __init__(
        self,
        id: str,
        rank: int,
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        gate: str | None = None,
        output_expression: str | None = None,
    ):
        self.id = _require_identifier(id, "node id")
        if self.id == GENERATOR_ID:
            raise GraphDefinitionError(f"node id {GENERATOR_ID!r} is reserved for the Generator")
        if not isinstance(rank, int) or isinstance(rank, bool):
            raise GraphDefinitionError(f"node {id!r}: rank must be an int, got {rank!r}")
        self.rank = rank
        self.inputs: dict[str, Any] = dict(inputs or {})
        self.outputs: dict[str, Any] = dict(outputs or {})
        for name in (*self.inputs, *self.outputs):
            _require_identifier(name, f"node {id!r} input/output name")
        self.gate = gate
        self.output_expression = output_expression

    def to_json(self):
        return {
            "id": self.id,
            "rank": self.rank,
            "type": self.type,
            "inputs": {k: _type_name(v) for k, v in self.inputs.items()},
            "outputs": {k: _type_name(v) for k, v in self.outputs.items()},
            "gate": self.gate,
            "output_expression": self.output_expression,
        }

    def __repr__(self):
        return f"{type(self).__name__}({self.id!r}, rank={self.rank})"


class ScriptNode(Node):
    """Arbitrary Python. ``fn`` is called as ``fn(inputs)`` where ``inputs``
    offers attribute and mapping access to the node's captured inputs. It may
    return a dict mapping declared output names to values, or — with exactly
    one declared output — the bare value. A script may do anything, including
    invoking models."""

    type = "script"

    def __init__(self, id, rank, fn: Callable, **kwargs):
        super().__init__(id, rank, **kwargs)
        if not callable(fn):
            raise GraphDefinitionError(f"script node {id!r}: fn must be callable")
        self.fn = fn


class LLMNode(Node):
    """A structured CLI model invocation. One LLM node = one provider = one
    CLI invocation per node invocation.

    Grok uses the CLI's default model; ``model`` is ignored and omitted from
    serialized node metadata for that provider.

    ``prompt`` is a ``string.Template``-style string (``$name`` placeholders
    substitute captured inputs) or a callable ``prompt(inputs) -> str``.

    Declared outputs beyond ``response`` (the raw reply text) are extracted
    from the last JSON object found in the reply, unless ``parse`` — a
    callable ``parse(text, result) -> dict`` — is given.
    """

    type = "llm"

    def __init__(
        self,
        id,
        rank,
        provider: str,
        model: str | None,
        prompt,
        effort: str | None = None,
        settings: str | None = None,
        cwd: str | None = None,
        extra_args: list[str] | None = None,
        timeout_s: float | None = 1800.0,
        parse: Callable | None = None,
        pause_message: str | None = None,
        resume_message: str | None = None,
        **kwargs,
    ):
        super().__init__(id, rank, **kwargs)
        self.provider = str(provider).lower()
        self.model = model
        self.prompt = prompt
        self.effort = effort
        self.settings = settings
        self.cwd = cwd
        self.extra_args = list(extra_args or [])
        self.timeout_s = timeout_s
        self.parse = parse
        self.pause_message = pause_message
        self.resume_message = resume_message

    def to_json(self):
        d = super().to_json()
        d.update(
            provider=self.provider,
            effort=self.effort,
            prompt=self.prompt if isinstance(self.prompt, str) else repr(self.prompt),
        )
        if self.provider != "grok":
            d["model"] = self.model
        return d


class HumanNode(Node):
    """A shell invocation that requires human input: the runtime launches the
    human-input shell (a Tauri app) with a request file and blocks until the
    human submits the response file supplying the node's declared outputs.

    ``prompt`` as in LLMNode. ``shell_cmd`` overrides the shell executable.
    """

    type = "human"

    def __init__(
        self,
        id,
        rank,
        prompt,
        shell_cmd: str | None = None,
        timeout_s: float | None = None,
        **kwargs,
    ):
        super().__init__(id, rank, **kwargs)
        self.prompt = prompt
        self.shell_cmd = shell_cmd
        self.timeout_s = timeout_s

    def to_json(self):
        d = super().to_json()
        d["prompt"] = self.prompt if isinstance(self.prompt, str) else repr(self.prompt)
        return d


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #


class Generator:
    """Exactly one per graph; its own element class, not a node. A single
    property — an output state that starts FALSE — and no logic, so nothing
    can be upstream of it. At run start its output state flips to TRUE; that
    flip is the run's first event and seeds the initial ready set."""

    type = "generator"

    def __init__(self):
        self.id = GENERATOR_ID
        self.outputs: dict[str, Any] = {}  # the Generator bears no data outputs

    def to_json(self):
        return {"id": self.id, "type": self.type}

    def __repr__(self):
        return f"Generator({self.id!r})"


# --------------------------------------------------------------------------- #
# Semaphores
# --------------------------------------------------------------------------- #


class Semaphores:
    """Global type-specific concurrency limits, declared prominently near the
    top of a graph definition. Per-provider limits subdivide the LLM
    semaphore: an LLM node starts running only when both its type's semaphore
    and its provider's (if declared) have a free slot. A provider absent from
    ``providers`` is bounded only by the LLM semaphore."""

    def __init__(
        self,
        script: int = 4,
        llm: int = 2,
        human: int = 1,
        providers: dict[str, int] | None = None,
    ):
        for name, v in (("script", script), ("llm", llm), ("human", human)):
            if not isinstance(v, int) or v < 1:
                raise GraphDefinitionError(f"semaphore {name!r} must be a positive int")
        self.script = script
        self.llm = llm
        self.human = human
        self.providers = {str(k).lower(): int(v) for k, v in (providers or {}).items()}
        for k, v in self.providers.items():
            if v < 1:
                raise GraphDefinitionError(f"provider semaphore {k!r} must be a positive int")

    def to_json(self):
        return {
            "script": self.script,
            "llm": self.llm,
            "human": self.human,
            "providers": dict(self.providers),
        }


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #


class Graph:
    """The declared topology: nodes, first-class edges, the Generator, and
    the run's semaphores. Static and immutable for the duration of a run."""

    def __init__(
        self,
        name: str,
        semaphores: Semaphores | None = None,
        pause_message: str | None = None,
        resume_message: str | None = None,
    ):
        self.name = str(name)
        self.semaphores = semaphores or Semaphores()
        self.generator = Generator()
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, Edge] = {}
        # Graph-wide overrides for the runtime default Pause/Resume messages;
        # per-LLM-node values override these in turn.
        self.pause_message = pause_message
        self.resume_message = resume_message

    # -- construction ------------------------------------------------------ #

    def add_node(self, *nodes: Node) -> "Graph":
        for node in nodes:
            if not isinstance(node, Node):
                raise GraphDefinitionError(f"{node!r} is not a Node")
            if node.id in self.nodes or node.id == GENERATOR_ID:
                raise GraphDefinitionError(f"duplicate node id {node.id!r}")
            self.nodes[node.id] = node
        return self

    add_nodes = add_node

    def add_edge(self, *edges: Edge) -> "Graph":
        for edge in edges:
            if not isinstance(edge, Edge):
                raise GraphDefinitionError(f"{edge!r} is not an Edge")
            if edge.id in self.edges:
                raise GraphDefinitionError(f"duplicate edge id {edge.id!r}")
            self.edges[edge.id] = edge
        return self

    add_edges = add_edge

    # -- topology queries -------------------------------------------------- #

    def in_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges.values() if node_id in e.outputs]

    def out_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges.values() if node_id in e.inputs]

    def element(self, element_id: str):
        if element_id == GENERATOR_ID:
            return self.generator
        return self.nodes[element_id]

    def nodes_by_rank(self) -> list[Node]:
        return sorted(self.nodes.values(), key=lambda n: n.rank)

    def primary_nodes(self) -> list[Node]:
        """Nodes whose in-edges come only from the Generator."""
        out = []
        for node in self.nodes.values():
            ins = self.in_edges(node.id)
            if ins and all(set(e.inputs) == {GENERATOR_ID} for e in ins):
                out.append(node)
        return out

    # -- interchange ------------------------------------------------------- #

    def to_json(self):
        return {
            "name": self.name,
            "semaphores": self.semaphores.to_json(),
            "generator": self.generator.to_json(),
            "nodes": [n.to_json() for n in self.nodes_by_rank()],
            "edges": [e.to_json() for e in self.edges.values()],
            "pause_message": self.pause_message,
            "resume_message": self.resume_message,
        }

    def __repr__(self):
        return f"Graph({self.name!r}, {len(self.nodes)} nodes, {len(self.edges)} edges)"
