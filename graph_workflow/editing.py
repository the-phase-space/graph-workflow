"""Monitor edits that require Python expression semantics."""

from __future__ import annotations

import ast
import copy
import io
import symtable
import tokenize


def _global_names(node: ast.AST) -> set[str]:
    """Names looked up in the evaluator, excluding lambda/comprehension locals."""
    table = symtable.symtable(ast.unparse(node), "<gate>", "eval")

    def names(scope):
        found = {s.get_name() for s in scope.get_symbols() if s.is_global() and s.is_referenced()}
        for child in scope.get_children():
            found.update(names(child))
        return found

    return names(table)


def prune_expression(expression: str | None, removed: set[str], surviving: set[str],
                     where: str, *, default: str | None = None) -> str | None:
    """Drop Boolean predicates dependent on removed bindings, then collapse
    and/or/not groups. An emptied expression returns to its native default.
    A predicate that also uses a surviving runtime binding needs an author's
    decision: abort the edit instead of silently dropping that constraint.
    Helper constants (such as a comparison threshold) do not themselves keep
    a predicate alive. Assignments in a removed predicate require an edit,
    since another clause may depend on the binding they create.
    """
    if expression is None or not removed:
        return expression
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        # An unrelated, unfinished expression should not prevent topology
        # edits. Tokenize only to locate a name; never rewrite invalid Python.
        names = set()
        try:
            for token in tokenize.generate_tokens(io.StringIO(expression).readline):
                if token.type == tokenize.NAME:
                    names.add(token.string)
        except (tokenize.TokenError, IndentationError):
            pass
        if names & removed:
            raise ValueError(f"{where}: fix its Python syntax before deleting {sorted(names & removed)}") from exc
        return expression

    if not (_global_names(tree.body) & removed):
        return expression

    def prune(node):
        refs = _global_names(node)
        if not refs & removed:
            return node
        if isinstance(node, ast.BoolOp):
            values = [kept for value in node.values if (kept := prune(value)) is not None]
            if not values:
                return None
            return values[0] if len(values) == 1 else ast.BoolOp(op=node.op, values=values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            operand = prune(node.operand)
            return ast.UnaryOp(op=node.op, operand=operand) if operand is not None else None
        assigned = {part.target.id for part in ast.walk(node) if isinstance(part, ast.NamedExpr)}
        if assigned:
            raise ValueError(
                f"{where}: removing {ast.unparse(node)!r} would discard assignments "
                f"to {sorted(assigned)}. Edit this predicate before deleting."
            )
        if refs & surviving:
            raise ValueError(
                f"{where}: cannot automatically remove {sorted(refs & removed)} from "
                f"{ast.unparse(node)!r}; it also uses {sorted(refs & surviving)}. "
                "Edit this predicate before deleting."
            )
        return None

    result = prune(tree.body)
    return ast.unparse(result) if result is not None else default


def remove_references(graph: dict, removed_nodes: set[str], removed_edges: list[dict]) -> None:
    """Clean explicit expressions in a graph whose topology was already edited."""
    node_ids = {node["id"] for node in graph["nodes"]}
    runtime_nodes = node_ids | {"GEN"}
    for node in graph["nodes"]:
        nid = node["id"]
        old_edge_terms = {e["id"] for e in removed_edges if nid in e["outputs"]}
        edge_terms = {e["id"] for e in graph["edges"] if nid in e["outputs"]}
        node["gate"] = prune_expression(
            node.get("gate"), (removed_nodes - edge_terms) | old_edge_terms,
            runtime_nodes | edge_terms, f"node {nid!r} gate",
        )
        outputs = set(node.get("outputs") or {})
        node["output_expression"] = prune_expression(
            node.get("output_expression"), removed_nodes - outputs,
            runtime_nodes | outputs, f"node {nid!r} output expression",
        )
        if node.get("persist_from") in removed_nodes:
            node["persist_from"] = None

    extras = {"sampled_value", "p_invocation"}
    for edge in graph["edges"]:
        gates = [("universal", edge.get("universal_gate"))]
        for role in ("input_gates", "output_gates"):
            gates.extend((f"{role}[{nid}]", gate) for nid, gate in (edge.get(role) or {}).items())
        for role, gate in gates:
            if gate is not None:
                gate["expression"] = prune_expression(
                    gate.get("expression", "True"), removed_nodes - extras,
                    runtime_nodes | extras, f"edge {edge['id']!r} {role} gate", default="True",
                )


def delete_element(graph: dict, kind: str, element_id: str) -> dict:
    """Delete topology and its explicit gate references as one undoable edit.
    The caller's graph stays untouched, including when cleanup is ambiguous.
    """
    if kind not in {"node", "edge"}:
        raise ValueError(f"unknown element kind {kind!r}")
    if not any(item["id"] == element_id for item in graph[kind + "s"]):
        raise ValueError(f"{kind} {element_id!r} does not exist")
    edited = copy.deepcopy(graph)
    removed_nodes = {element_id} if kind == "node" else set()
    if removed_nodes:
        edited["nodes"] = [node for node in edited["nodes"] if node["id"] != element_id]
    edges = []
    removed_edges = []
    for edge in edited["edges"]:
        # Preserve the original outputs to know where a removed edge was a
        # bare Boolean binding; it has no such meaning in other node gates.
        original = copy.deepcopy(edge)
        for role in ("inputs", "outputs"):
            edge[role] = [nid for nid in edge[role] if nid not in removed_nodes]
        for role in ("input_gates", "output_gates"):
            edge[role] = {nid: gate for nid, gate in (edge.get(role) or {}).items()
                          if nid not in removed_nodes}
        if (kind == "edge" and edge["id"] == element_id) or not edge["inputs"] or not edge["outputs"]:
            removed_edges.append(original)
        else:
            edges.append(edge)
    edited["edges"] = edges
    remove_references(edited, removed_nodes, removed_edges)
    return edited
