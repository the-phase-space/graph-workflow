"""Structural graph validation.

The runtime checks that the Python in a graph definition is valid and runs it
as-is: expressions must compile, references must resolve, the Generator and
primary-node rules must hold, and every declared input must have a possible
upstream supplier (the input/output validator). No semantic idiot-proofing —
contradictory gating expressions and perpetual graphs execute as written.

Errors raise ``GraphValidationError``; warnings are returned and land in the
trace.
"""

from __future__ import annotations

import ast
import builtins

from .errors import GraphValidationError
from .model import GENERATOR_ID, RESERVED_METRICS, Graph, LLMNode, HumanNode
from .providers import known_providers


def _free_names(expr: str) -> set[str]:
    """Names an expression reads that it does not itself bind (comprehension
    targets and walrus bindings are excluded). Unparseable expressions return
    the empty set — the syntax error surfaces separately."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return set()
    loads = {
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    bound = {
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load)
    }
    return loads - bound


def validate(graph: Graph, namespace: dict | None = None) -> list[str]:
    errors: list[str] = []
    warnings: list[str] = []
    ns_provided = namespace is not None
    namespace = namespace or {}

    node_ids = set(graph.nodes)

    # -- identity and rank -------------------------------------------------- #
    ranks: dict[int, str] = {}
    for node in graph.nodes.values():
        if node.rank in ranks:
            errors.append(
                f"nodes {ranks[node.rank]!r} and {node.id!r} share rank {node.rank}; "
                "every node has a unique rank"
            )
        else:
            ranks[node.rank] = node.id

    id_overlap = node_ids & set(graph.edges)
    for shared in sorted(id_overlap):
        errors.append(f"{shared!r} is used as both a node id and an edge id")

    for node in graph.nodes.values():
        for name in node.outputs:
            if name in RESERVED_METRICS:
                errors.append(
                    f"node {node.id!r}: declared output {name!r} collides with a "
                    "runtime metric name"
                )

    # -- edge endpoint references ------------------------------------------- #
    for edge in graph.edges.values():
        for nid in edge.inputs:
            if nid != GENERATOR_ID and nid not in node_ids:
                errors.append(f"edge {edge.id!r}: unknown input node {nid!r}")
        for nid in edge.outputs:
            if nid == GENERATOR_ID:
                errors.append(
                    f"edge {edge.id!r}: the Generator has no logic; nothing can be "
                    "upstream of it"
                )
            elif nid not in node_ids:
                errors.append(f"edge {edge.id!r}: unknown output node {nid!r}")

    # -- generator / primary rule ------------------------------------------- #
    for node in graph.nodes.values():
        in_edges = graph.in_edges(node.id)
        if not in_edges:
            warnings.append(
                f"node {node.id!r} has no in-edges: its gating expression is over "
                "nothing and the node can never become ready"
                if node.gate is None
                else f"node {node.id!r} has no in-edges; only an expression over "
                "node state can ever ready it"
            )
        elif all(set(e.inputs) == {GENERATOR_ID} for e in in_edges):
            # Primary node: declares that single input and nothing else.
            if node.inputs:
                errors.append(
                    f"primary node {node.id!r} (in-edges only from the Generator) "
                    f"declares data inputs {sorted(node.inputs)}; primary nodes "
                    "declare that single input and nothing else"
                )

    # -- input/output satisfiability ---------------------------------------- #
    for node in graph.nodes.values():
        suppliers: set[str] = set()
        for edge in graph.in_edges(node.id):
            for nid in edge.inputs:
                if nid in graph.nodes:
                    suppliers.update(graph.nodes[nid].outputs)
        for name in node.inputs:
            if name not in suppliers:
                errors.append(
                    f"node {node.id!r}: declared input {name!r} cannot be supplied "
                    "by any upstream output on any in-edge"
                )

    # -- expression syntax --------------------------------------------------- #
    def check_expr(expr: str | None, where: str):
        if expr is None:
            return
        try:
            compile(expr, f"<{where}>", "eval")
        except SyntaxError as exc:
            errors.append(f"{where}: invalid Python: {exc.msg} in {expr!r}")

    for node in graph.nodes.values():
        check_expr(node.gate, f"node {node.id} gating expression")
        check_expr(node.output_expression, f"node {node.id} output expression")
    for edge in graph.edges.values():
        for gate in edge.all_gates():
            check_expr(gate.expression, f"gate {gate.gate_id}")
            if gate.p_invocation is not None and not (0.0 <= gate.p_invocation <= 1.0):
                errors.append(
                    f"gate {gate.gate_id}: p_invocation must be in [0, 1], "
                    f"got {gate.p_invocation}"
                )

    # -- expression name resolution ------------------------------------------ #
    # Expressions evaluate in the definition module's own namespace plus the
    # runtime bindings; a name resolvable in neither evaluates to a NameError
    # (treated as False) at run time. Only meaningful when the caller supplied
    # the definition namespace.
    if ns_provided:
        known = (
            set(namespace)
            | node_ids
            | set(graph.edges)
            | {GENERATOR_ID, "isDefined", "UNDEFINED"}
            | set(vars(builtins))
        )

        def check_names(expr: str | None, where: str, extra: tuple | dict = ()):
            if not expr:
                return
            for name in sorted(_free_names(expr) - known - set(extra)):
                warnings.append(
                    f"{where} references {name!r}, which is not a node, edge, "
                    "helper, or builtin — it will fail at evaluation time"
                )

        for node in graph.nodes.values():
            check_names(node.gate, f"node {node.id} gating expression")
            check_names(
                node.output_expression,
                f"node {node.id} output expression",
                extra=node.outputs,
            )
        for edge in graph.edges.values():
            for gate in edge.all_gates():
                check_names(
                    gate.expression,
                    f"gate {gate.gate_id}",
                    extra=("sampled_value", "p_invocation"),
                )

    # -- typed node requirements -------------------------------------------- #
    providers = set(known_providers())
    for node in graph.nodes.values():
        if isinstance(node, LLMNode):
            if node.provider not in providers:
                errors.append(
                    f"LLM node {node.id!r}: unknown provider {node.provider!r} "
                    f"(known: {sorted(providers)})"
                )
            # Grok always uses its CLI's default model.
            if node.provider != "grok" and not node.model:
                errors.append(f"LLM node {node.id!r}: model is required")
            if node.prompt is None:
                errors.append(f"LLM node {node.id!r}: prompt is required")
        if isinstance(node, HumanNode) and node.prompt is None:
            errors.append(f"human node {node.id!r}: prompt is required")

    for provider in graph.semaphores.providers:
        if provider not in providers:
            warnings.append(
                f"semaphore declared for unknown provider {provider!r}"
            )

    # -- namespace shadowing (curatorial) ------------------------------------ #
    # Binding the node object to a module variable of the same name
    # (``A = ScriptNode("A", ...)``) is the expected idiom — inside gating
    # expressions that name resolves to the runtime handle instead, which is
    # what an author means. Warn only when the shadowed name is some
    # unrelated value.
    for shared in sorted((node_ids | set(graph.edges) | {GENERATOR_ID}) & set(namespace)):
        value = namespace[shared]
        if shared in graph.nodes and value is graph.nodes[shared]:
            continue
        if shared in graph.edges and value is graph.edges[shared]:
            continue
        if shared == GENERATOR_ID and value is graph.generator:
            continue
        warnings.append(
            f"graph namespace name {shared!r} is shadowed by a node/edge handle "
            "inside gating expressions"
        )

    if errors:
        raise GraphValidationError(errors)
    return warnings
