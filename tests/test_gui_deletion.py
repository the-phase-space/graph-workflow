"""Deleting graph elements removes their gate dependencies as one edit."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from graph_workflow import Edge, Graph, ScriptNode, gui
from graph_workflow.editing import delete_element, prune_expression


def empty(inputs):
    return {}


def convergent_graph():
    g = Graph("deletion")
    a = ScriptNode("A", 0, empty)
    b = ScriptNode("B", 1, empty)
    c = ScriptNode("C", 2, empty, gate="A.commits >= 1 and B.commits >= 1")
    g.add_nodes(a, b, c)
    g.add_edges(Edge("start", g.generator, [a, b]), Edge("gather", [a, b], c))
    return gui.graph_to_contract(g, {"empty": empty})


class ExpressionPruningTests(unittest.TestCase):
    def prune(self, expression, **kwargs):
        return prune_expression(expression, {"A"}, {"B", "C", "GEN"}, "node C gate", **kwargs)

    def test_boolean_structure_and_remaining_gate_behavior(self):
        cases = {
            "A.commits >= 1 and B.commits >= 1": "B.commits >= 1",
            "B.commits >= 1 or A.commits >= 1": "B.commits >= 1",
            "not (A.output_state or B.output_state)": "not B.output_state",
            "(A.output_state and B.output_state) or C.output_state": "B.output_state or C.output_state",
            "not A.output_state and isDefined(A.value) and B.commits > 0": "B.commits > 0",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                rewritten = self.prune(original)
                self.assertEqual(rewritten, expected)
                for state in (False, True):
                    namespace = {"B": SimpleNamespace(commits=int(state), output_state=state),
                                 "C": SimpleNamespace(output_state=False)}
                    self.assertEqual(eval(rewritten, namespace), eval(expected, namespace))

    def test_all_removed_returns_native_default(self):
        for expression in ("A.commits > 0", "not A.output_state", "A or not A"):
            self.assertIsNone(self.prune(expression))
            self.assertEqual(self.prune(expression, default="True"), "True")

    def test_unaffected_text_and_non_references_are_untouched(self):
        for expression in ('  B.commits  >=  1  ', '"A" == "A"', "helper.A", "AA.commits > 0",
                           "all(A.ready for A in records)", "(lambda A: A.ready)(record)"):
            self.assertEqual(self.prune(expression), expression)

    def test_mixed_predicates_require_an_explicit_edit(self):
        for expression in ("A.commits >= B.commits", "A.commits + B.commits >= 2",
                           "any(n.output_state for n in [A, B])", "B if A.output_state else C"):
            with self.subTest(expression=expression):
                with self.assertRaisesRegex(ValueError, "also uses.*Edit this predicate"):
                    self.prune(expression)

    def test_unrelated_invalid_expression_is_preserved(self):
        self.assertEqual(self.prune("B.commits >"), "B.commits >")
        with self.assertRaisesRegex(ValueError, "fix its Python syntax"):
            self.prune("A.commits >")

    def test_assignments_used_by_later_clauses_are_not_discarded(self):
        with self.assertRaisesRegex(ValueError, "would discard assignments.*x"):
            self.prune("(x := A.commits) > 0 and x > 1")
        # A binding in a surviving predicate remains intact.
        rewritten = self.prune("A.commits > 0 and (x := B.commits) > 0 and x > 1")
        self.assertTrue(eval(rewritten, {"B": SimpleNamespace(commits=2)}))

    def test_helper_threshold_does_not_keep_deleted_nodes_predicate(self):
        self.assertIsNone(self.prune("A.commits > limit"))


class DeletionTests(unittest.TestCase):
    def test_unknown_kind_and_id_leave_graph_untouched(self):
        graph = convergent_graph()
        before = copy.deepcopy(graph)
        for kind, identity in (("gate", "A"), ("node", "missing"), ("edge", "missing")):
            with self.subTest(kind=kind, identity=identity):
                with self.assertRaises(ValueError):
                    delete_element(graph, kind, identity)
                self.assertEqual(graph, before)

    def test_node_deletion_preserves_other_endpoints_and_prunes_gates(self):
        graph = convergent_graph()
        gather = graph["edges"][1]
        gather["universal_gate"] = {"expression": "A.output_state and B.output_state"}
        gather["input_gates"]["B"] = {"expression": "A.commits > 0 and sampled_value < 0.5",
                                        "p_invocation": 0.5}
        gather["output_gates"]["C"] = {"expression": "not A.output_state"}
        before = copy.deepcopy(graph)
        edited = delete_element(graph, "node", "A")
        self.assertEqual(graph, before)
        self.assertEqual([n["id"] for n in edited["nodes"]], ["B", "C"])
        self.assertEqual(edited["nodes"][1]["gate"], "B.commits >= 1")
        gather = edited["edges"][1]
        self.assertEqual(gather["inputs"], ["B"])
        self.assertNotIn("A", gather["input_gates"])
        self.assertEqual(gather["universal_gate"]["expression"], "B.output_state")
        self.assertEqual(gather["input_gates"]["B"],
                         {"expression": "sampled_value < 0.5", "p_invocation": 0.5})
        self.assertEqual(gather["output_gates"]["C"]["expression"], "True")

    def test_removed_edge_terms_are_cleaned_only_in_their_target_nodes(self):
        graph = convergent_graph()
        graph["nodes"][1]["gate"] = "gather"  # a helper name here, not an incoming edge
        graph["nodes"][2]["gate"] = "gather and C.invocations < 1"
        edited = delete_element(graph, "edge", "gather")
        self.assertEqual(edited["nodes"][1]["gate"], "gather")
        self.assertEqual(edited["nodes"][2]["gate"], "C.invocations < 1")
        self.assertEqual([e["id"] for e in edited["edges"]], ["start"])

    def test_edges_emptied_by_node_deletion_also_lose_their_gate_terms(self):
        graph = convergent_graph()
        graph["edges"][1]["inputs"] = ["A"]
        graph["edges"][1]["input_gates"].pop("B")
        graph["nodes"][2]["gate"] = "gather and A.commits >= 1"
        edited = delete_element(graph, "node", "A")
        self.assertIsNone(edited["nodes"][1]["gate"])
        self.assertEqual([e["id"] for e in edited["edges"]], ["start"])

    def test_emptied_edge_updates_every_remaining_target(self):
        graph = convergent_graph()
        gather = graph["edges"][1]
        gather["inputs"] = ["A"]
        gather["input_gates"].pop("B")
        gather["outputs"] = ["B", "C"]
        gather["output_gates"]["B"] = {"expression": "True"}
        graph["nodes"][1]["gate"] = "gather and B.invocations < 1"
        graph["nodes"][2]["gate"] = "gather and A.commits > 0"
        edited = delete_element(graph, "node", "A")
        self.assertEqual(edited["nodes"][0]["gate"], "B.invocations < 1")
        self.assertIsNone(edited["nodes"][1]["gate"])
        self.assertEqual([e["id"] for e in edited["edges"]], ["start"])

    def test_output_bindings_shadow_deleted_node_names(self):
        graph = convergent_graph()
        graph["nodes"][1]["output_expression"] = "A.commits > 0 and B.commits > 0"
        graph["nodes"][2]["outputs"] = {"A": "str"}
        graph["nodes"][2]["output_expression"] = "isDefined(A)"
        edited = delete_element(graph, "node", "A")
        self.assertEqual(edited["nodes"][0]["output_expression"], "B.commits > 0")
        self.assertEqual(edited["nodes"][1]["output_expression"], "isDefined(A)")

    def test_ambiguous_edit_does_not_partially_delete_or_change_gates(self):
        graph = convergent_graph()
        graph["nodes"][1]["gate"] = "A.output_state"
        graph["nodes"][2]["gate"] = "A.commits >= B.commits"
        before = copy.deepcopy(graph)
        with self.assertRaisesRegex(ValueError, "node 'C' gate"):
            delete_element(graph, "node", "A")
        self.assertEqual(graph, before)

    def test_deleted_persistent_context_source_is_cleared(self):
        graph = convergent_graph()
        graph["nodes"][1]["persist_from"] = "A"
        edited = delete_element(graph, "node", "A")
        self.assertIsNone(edited["nodes"][0]["persist_from"])

    def test_edit_survives_save_reload_and_validates_without_stale_names(self):
        contract = gui.template("deletion")
        contract["graph"] = delete_element(convergent_graph(), "node", "A")
        with tempfile.TemporaryDirectory(prefix="gw-deletion-") as scratch:
            saved = gui.save_contract(Path(scratch) / "definition.py", contract)
            self.assertEqual(saved["errors"], [])
            self.assertEqual(saved["warnings"], [])
            self.assertEqual(saved["graph"]["nodes"][1]["gate"], "B.commits >= 1")

    def test_unsupplied_inputs_are_left_for_author_review(self):
        graph = convergent_graph()
        graph["nodes"][0]["outputs"] = {"data": "str"}
        graph["nodes"][2]["inputs"] = {"data": "str"}
        contract = gui.template("deletion")
        contract["graph"] = delete_element(graph, "node", "A")
        with tempfile.TemporaryDirectory(prefix="gw-deletion-") as scratch:
            saved = gui.save_contract(Path(scratch) / "definition.py", contract)
            self.assertEqual(saved["graph"]["nodes"][1]["inputs"], {"data": "str"})
            self.assertTrue(any("input 'data' cannot be supplied" in e for e in saved["errors"]))

    def test_cli_missing_arguments_give_usage(self):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "graph_workflow.gui", "delete"],
            input=b"{}", capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage: gui delete node|edge", json.loads(result.stdout)["error"])

    def test_cli_boundary_returns_edited_graph_and_named_failures(self):
        graph = convergent_graph()
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                if ambiguous:
                    graph["nodes"][2]["gate"] = "A.commits >= B.commits"
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "graph_workflow.gui", "delete", "node", "A"],
                    input=json.dumps(graph).encode("utf-8"), capture_output=True, timeout=15,
                )
                payload = json.loads(result.stdout)
                self.assertEqual(result.returncode, int(ambiguous))
                self.assertEqual(payload["ok"], not ambiguous)
                if ambiguous:
                    self.assertIn("node 'C' gate", payload["error"])
                else:
                    self.assertEqual(payload["graph"]["nodes"][1]["gate"], "B.commits >= 1")


if __name__ == "__main__":
    unittest.main()
