"""Monitor saves preserve callable behavior, not function debug strings."""

import json
from pathlib import Path
import tempfile
import textwrap
import unittest

from graph_workflow import HumanNode, LLMNode, ScriptNode, gui


PRELUDE = '''\
from graph_workflow import Edge, Graph, HumanNode, LLMNode, ScriptNode, run
from functools import partial

def capture(output_name):
    def parse_reply(text, raw):
        return {output_name: text.strip()}
    return parse_reply

def named_parser(output_name, text, raw):
    return {output_name: text.strip()}

g = Graph("callable-save")
'''


class CallableSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gw-gui-callables-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / "original.py"
        self.saved = self.root / "saved.py"

    def load(self, body):
        self.original.write_text(PRELUDE + textwrap.dedent(body), encoding="utf-8")
        # Exercise the actual JSON boundary as well as Python serialization.
        return json.loads(json.dumps(gui.load_contract(self.original)))

    def round_trip(self, contract):
        saved = gui.save_contract(self.saved, contract)
        graph, _ = gui.capture(self.saved)
        return saved, graph

    def test_deleting_unrelated_node_preserves_distinct_closures_across_saves(self):
        contract = self.load('''
            discard = LLMNode("DISCARD", 0, provider="claude", model="test", prompt="discard")
            a = LLMNode("FABLE", 1, provider="claude", model="test", prompt="review",
                        parse=capture("fable_pick"), outputs={"fable_pick": str})
            b = LLMNode("ASTRA", 2, provider="codex", model="test", prompt="review",
                        parse=capture("astra_pick"), outputs={"astra_pick": str})
            g.add_nodes(discard, a, b)
            g.add_edge(Edge("start", g.generator, [discard, a, b]))
            if __name__ == "__main__":
                run(g)
        ''')
        original = self.original.read_bytes()
        contract["graph"]["nodes"] = [
            n for n in contract["graph"]["nodes"] if n["id"] != "DISCARD"
        ]
        contract["graph"]["edges"][0]["outputs"].remove("DISCARD")
        for _ in range(2):
            contract, graph = self.round_trip(contract)
            self.assertEqual(set(graph.nodes), {"FABLE", "ASTRA"})
            self.assertEqual(graph.nodes["FABLE"].parse(" fable ", None), {"fable_pick": "fable"})
            self.assertEqual(graph.nodes["ASTRA"].parse(" astra ", None), {"astra_pick": "astra"})
            self.assertEqual(contract["errors"], [])
            self.assertEqual(contract["warnings"], [])
        self.assertEqual(self.original.read_bytes(), original)

    def test_alias_lambda_and_partial_parsers(self):
        contract = self.load('''
            parser_alias = capture("alias")
            a = LLMNode("A", 0, "claude", "test", "prompt", parse=parser_alias)
            b = LLMNode("B", 1, "claude", "test", "prompt",
                        parse=lambda text, raw: {"lambda": text.strip()})
            c = LLMNode("C", 2, "claude", "test", "prompt",
                        parse=partial(named_parser, "partial"))
            g.add_nodes(a, b, c)
        ''')
        for _ in range(2):
            contract, graph = self.round_trip(contract)
            for nid, key in (("A", "alias"), ("B", "lambda"), ("C", "partial")):
                self.assertEqual(graph.nodes[nid].parse(" value ", None), {key: "value"})

    def test_module_alias_recovers_a_closure_without_constructor_source(self):
        contract = self.load('''
            parser_alias = capture("alias")
            def build():
                return LLMNode("A", 0, "claude", "test", "prompt", parse=parser_alias)
            a = build()
            g.add_node(a)
        ''')
        _, graph = self.round_trip(contract)
        self.assertEqual(graph.nodes["A"].parse(" value ", None), {"alias": "value"})

    def test_script_and_prompt_factories_keep_enclosing_bindings(self):
        contract = self.load('''
            def script_factory(key):
                def fn(inputs):
                    return {key: inputs["value"]}
                return fn

            def prompt_factory(prefix):
                def prompt(inputs):
                    return prefix + inputs["value"]
                return prompt

            a = ScriptNode("A", 0, script_factory("script"))
            b = LLMNode("B", 1, "claude", "test", prompt_factory("llm:"))
            c = HumanNode("C", 2, prompt_factory("human:"))
            g.add_nodes(a, b, c)
        ''')
        for _ in range(2):
            contract, graph = self.round_trip(contract)
            self.assertEqual(graph.nodes["A"].fn({"value": "x"}), {"script": "x"})
            self.assertEqual(graph.nodes["B"].prompt({"value": "x"}), "llm:x")
            self.assertEqual(graph.nodes["C"].prompt({"value": "x"}), "human:x")

    def test_unrecoverable_parser_is_reported_and_target_is_untouched(self):
        contract = self.load('''
            def build():
                return LLMNode("A", 0, "claude", "test", "prompt", parse=capture("hidden"))
            a = build()
            g.add_node(a)
        ''')
        self.assertIsNone(contract["graph"]["nodes"][0]["parse_ref"])
        self.assertTrue(any("node 'A': cannot save parse callable" in w for w in contract["warnings"]))
        self.saved.write_text("# original target\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "node 'A': cannot save parse callable"):
            gui.save_contract(self.saved, contract, force=True)
        self.assertEqual(self.saved.read_text(encoding="utf-8"), "# original target\n")
        self.assertFalse(self.saved.with_name("saved.py.gw-tmp").exists())

    def test_legacy_debug_repr_is_never_emitted_as_code(self):
        contract = self.load('''
            a = LLMNode("A", 0, "claude", "test", "prompt", parse=capture("reply"))
            g.add_node(a)
        ''')
        contract["graph"]["nodes"][0]["parse_ref"] = "<function capture.<locals>.parse_reply at 0x1234>"
        with self.assertRaisesRegex(ValueError, "node 'A': cannot save parse callable"):
            gui.save_contract(self.saved, contract)
        self.assertFalse(self.saved.exists())

    def test_changing_bindings_and_callbacks_are_not_guessed(self):
        for tail in ('key = "changed"', 'a.parse = capture("changed")'):
            with self.subTest(tail=tail):
                contract = self.load(textwrap.dedent('''
                    key = "original"
                    a = LLMNode("A", 0, "claude", "test", "prompt", parse=capture(key))
                    g.add_node(a)
                ''') + tail + "\n")
                with self.assertRaisesRegex(ValueError, "node 'A': cannot save parse callable"):
                    gui.generate(contract)

    def test_loop_scope_is_not_replaced_with_its_final_binding(self):
        contract = self.load('''
            for rank, key in enumerate(("first", "second")):
                g.add_node(LLMNode(key, rank, "claude", "test", "prompt", parse=capture(key)))
        ''')
        with self.assertRaisesRegex(ValueError, "node 'first': cannot save parse callable"):
            gui.generate(contract)

    def test_constructor_hooks_are_restored_on_capture_failure(self):
        original_inits = {cls: cls.__init__ for cls in (ScriptNode, LLMNode, HumanNode)}
        with self.assertRaisesRegex(RuntimeError, "broken definition"):
            self.load('raise RuntimeError("broken definition")\n')
        self.assertEqual({cls: cls.__init__ for cls in original_inits}, original_inits)


if __name__ == "__main__":
    unittest.main()
