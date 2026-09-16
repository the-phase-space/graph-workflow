"""Persistent contexts, with provider processes replaced at the invocation boundary."""

import json
import os
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from graph_workflow import Edge, Graph, LLMNode, ScriptNode, isDefined
from graph_workflow import gui, runners
from graph_workflow.engine import Runtime
from graph_workflow.errors import GraphValidationError
from graph_workflow.providers import InvocationResult, get_adapter, known_providers
from graph_workflow.trace import read_events
from graph_workflow.validation import validate


def llm(nid, rank, **kwargs):
    return LLMNode(nid, rank, provider=kwargs.pop("provider", "claude"),
                   model="test-model", prompt=kwargs.pop("prompt", f"prompt-{nid}"),
                   outputs=kwargs.pop("outputs", {"response": str}), **kwargs)


def pair():
    graph = Graph("persist-pair")
    a = llm("A", 0)
    b = llm("B", 1, persist_from="A", prompt="review $response",
            inputs={"response": str})
    graph.add_nodes(a, b)
    graph.add_edges(Edge("start", graph.generator, a), Edge("forward", a, b))
    return graph


def loop(self_persist=True):
    graph = Graph("persist-loop")
    a = llm("A", 0, persist_from="A" if self_persist else None,
            gate="(start or back) and A.invocations == B.commits and A.invocations < 3",
            prompt=lambda i: f"discuss {i.response}" if i.response else "begin",
            inputs={"response": str})
    b = llm("B", 1, provider="codex" if self_persist else "claude",
            persist_from="B" if self_persist else "A", prompt="review $response",
            gate="forward and B.invocations < A.commits", inputs={"response": str})
    graph.add_nodes(a, b)
    graph.add_edges(Edge("start", graph.generator, a), Edge("forward", a, b),
                    Edge("back", b, a))
    return graph


class PersistenceValidationTests(unittest.TestCase):
    def test_direct_and_transitive_paths_ignore_gates_and_providers(self):
        graph = pair()
        graph.nodes["B"].provider = "codexb"
        middle = ScriptNode("M", 2, lambda i: {})
        other = ScriptNode("S", 3, lambda i: {})
        graph.add_nodes(middle, other)
        graph.edges.clear()
        graph.nodes["B"].inputs.clear()
        graph.add_edges(Edge("first", "A", ["M", "S"], universal_gate="False"),
                        Edge("second", ["M", "S"], "B"))
        validate(graph)

    def test_unknown_reverse_and_disconnected_sources_fail(self):
        for source, edges in (("missing", [("A", "B")]),
                              ("A", [("B", "A")]), ("A", [])):
            with self.subTest(source=source, edges=edges):
                graph = pair()
                graph.nodes["B"].persist_from = source
                graph.nodes["B"].inputs.clear()
                graph.edges.clear()
                for index, (a, b) in enumerate(edges):
                    graph.add_edge(Edge(f"edge{index}", a, b))
                with self.assertRaisesRegex(GraphValidationError, "persist_from"):
                    validate(graph)

    def test_self_reference_requires_a_nonempty_cycle(self):
        graph = pair()
        graph.nodes["A"].persist_from = "A"
        with self.assertRaisesRegex(GraphValidationError, "directed path"):
            validate(graph)
        graph.add_edge(Edge("back", "B", "A"))
        validate(graph)
        validate(loop())


class PersistenceRunTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ["LOCALAPPDATA"]) / "Temp" if os.name == "nt" else None
        self.temp = tempfile.TemporaryDirectory(prefix="gw-persistence-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.addCleanup(patch.stopall)
        self.invoker = patch("graph_workflow.runners._invoke_llm",
                             side_effect=self.reply).start()

    def reply(self, node, prompt, **kwargs):
        self.calls.append((node.id, prompt, kwargs["resume_session"], kwargs["tag"]))
        sid = kwargs["resume_session"] or f"{node.provider}-{node.id}-{kwargs['invocation']}"
        result = InvocationResult(text=f"reply-{node.id}-{kwargs['invocation']}",
                                  session_id=sid)
        return result, SimpleNamespace(steered=False, killed=False)

    def runtime(self, graph, name="run", **kwargs):
        return Runtime(graph, run_dir=self.root / name, seed=17, namespace={},
                       console=False, max_commits=20, monitor=False, **kwargs)

    def test_transitive_and_chained_contexts_use_each_nodes_prompt(self):
        graph = pair()
        graph.nodes["A"].provider = graph.nodes["B"].provider = "codex"
        graph.nodes["B"].inputs = {"instruction": str}
        graph.nodes["B"].prompt = "do $instruction"
        graph.edges.pop("forward")
        graph.add_nodes(ScriptNode("M", 2, lambda i: {"instruction": "review"},
                                  outputs={"instruction": str}),
                        llm("C", 3, persist_from="B", provider="codex"),
                        llm("D", 4, provider="codex"))
        graph.add_edges(Edge("am", "A", "M"), Edge("mb", "M", "B"),
                        Edge("bc", "B", "C"), Edge("cd", "C", "D"))
        result = self.runtime(graph).run()
        self.assertEqual(result.reason, "quiescent")
        self.assertEqual(self.calls, [
            ("A", "prompt-A", None, ""),
            ("B", "do review", "codex-A-1", ""),
            ("C", "prompt-C", "codex-A-1", ""),
            ("D", "prompt-D", None, ""),
        ])
        self.assertEqual(result.states["C"].session_id, "codex-A-1")

    def test_two_loop_agents_keep_separate_contexts(self):
        result = self.runtime(loop()).run()
        self.assertEqual(result.reason, "quiescent")
        self.assertEqual(self.calls, [
            ("A", "begin", None, ""),
            ("B", "review reply-A-1", None, ""),
            ("A", "discuss reply-B-1", "claude-A-1", ""),
            ("B", "review reply-A-2", "codex-B-1", ""),
            ("A", "discuss reply-B-2", "claude-A-1", ""),
            ("B", "review reply-A-3", "codex-B-1", ""),
        ])
        for nid in ("A", "B"):
            self.assertEqual(result.states[nid].invocations, 3)
            self.assertEqual(result.states[nid].commits, 3)

    def test_each_invocation_selects_sources_latest_session(self):
        self.runtime(loop(self_persist=False)).run()
        self.assertEqual([call[2] for call in self.calls],
                         [None, "claude-A-1", None, "claude-A-2", None, "claude-A-3"])

    def test_missing_source_session_errors_without_a_fresh_fallback(self):
        graph = pair()
        graph.nodes["A"] = ScriptNode("A", 0, lambda i: {"response": "no session"},
                                     outputs={"response": str})
        result = self.runtime(graph).run()
        self.invoker.assert_not_called()
        self.assertFalse(result.output_state("B"))
        commits = [e for e in read_events(result.trace_path)
                   if e["event"] == "commit" and e["node"] == "B"]
        self.assertIn("no session id available", commits[0]["error"])
        self.assertIsNone(commits[0]["metrics"]["session_id"])

    def test_pause_resume_keeps_bound_session_and_finishes_same_invocation(self):
        graph = pair()
        graph.nodes["B"].outputs["decision"] = bool
        graph.nodes["B"].resume_message = "finish the review"
        runtime = self.runtime(graph)
        paused = threading.Event()

        def invoke(node, prompt, **kwargs):
            result, handle = self.reply(node, prompt, **kwargs)
            if node.id == "B" and kwargs["tag"] == "":
                def steer(message):
                    handle.steered = True
                    paused.set()
                    return True
                handle.steer = steer
                kwargs["register_proc"](handle)
                runtime.pause()
                if not paused.wait(5):
                    raise AssertionError("pause never reached the running node")
                kwargs["register_proc"](None)
                result.text = '{"decision": true}'
            return result, handle

        original_emit = runtime._emit

        def emit(event, **payload):
            seq = original_emit(event, **payload)
            if event == "paused":
                runtime.states["A"].session_id = "later-source-session"
                runtime.resume()
            return seq

        self.invoker.side_effect = invoke
        with patch.object(runtime, "_emit", side_effect=emit):
            result = runtime.run()
        self.assertEqual(self.calls[-1],
                         ("B", "finish the review", "claude-A-1", "-resume"))
        self.assertEqual(result.states["B"].invocations, 1)
        self.assertEqual(result.states["B"].commits, 1)
        self.assertTrue(result.outputs("B")["decision"])

    def test_replay_restores_committed_context_and_inflight_binding(self):
        graph = pair()
        original = self.runtime(graph).run()
        events = read_events(original.trace_path)
        for cut_after in ("commit", "start"):
            with self.subTest(cut_after=cut_after):
                target = "A" if cut_after == "commit" else "B"
                cut = next(i for i, e in enumerate(events)
                           if e["event"] == cut_after and e.get("node") == target)
                prefix = events[:cut + 1]
                directory = self.root / cut_after
                directory.mkdir()
                (directory / "trace.jsonl").write_text(
                    "".join(json.dumps(e) + "\n" for e in prefix), encoding="utf-8")
                recovered = self.runtime(graph, cut_after, _resume_events=prefix,
                                         _run_id=original.run_id)
                if cut_after == "start":
                    # A later source turn must not change B's recorded binding.
                    recovered.states["A"].session_id = "newer-source-session"
                self.calls.clear()
                result = recovered.run()
                self.assertEqual(self.calls[0][2], "claude-A-1")
                self.assertEqual(result.states["B"].session_id, "claude-A-1")

    def test_gui_save_load_preserves_declaration_and_trace_metadata(self):
        graph = pair()
        contract = gui.template("persist-pair")
        contract["graph"] = gui.graph_to_contract(graph, {})
        saved = gui.save_contract(self.root / "definition.py", contract)
        self.assertTrue(saved["ok"], saved.get("errors"))
        b = next(n for n in saved["graph"]["nodes"] if n["id"] == "B")
        self.assertEqual(b["persist_from"], "A")
        self.assertEqual(graph.to_json()["nodes"][1]["persist_from"], "A")

    def test_authoring_examples_run(self):
        guide = Path(__file__).resolve().parents[1] / "guide" / "authoring.md"
        examples = [block for block in re.findall(r"```python\n(.*?)```",
                                                  guide.read_text(encoding="utf-8"), re.S)
                    if "persist_from=" in block and "g = Graph(" in block]
        self.assertEqual(len(examples), 2)
        for index, example in enumerate(examples):
            with self.subTest(example=index):
                namespace = dict(Graph=Graph, LLMNode=LLMNode, Edge=Edge, isDefined=isDefined)
                exec(example, namespace)
                self.calls.clear()
                result = self.runtime(namespace["g"], f"example-{index}").run()
                self.assertEqual(result.reason, "quiescent")
                self.assertTrue(all(s.output_state for s in result.states.values()))
                self.assertEqual(len(self.calls), 2 if index == 0 else 6)


class PersistenceProviderTests(unittest.TestCase):
    def test_provider_routes_and_session_id_retention(self):
        scratch = Path(os.environ["LOCALAPPDATA"]) / "Temp" if os.name == "nt" else None
        with tempfile.TemporaryDirectory(prefix="gw-persist-providers-", dir=scratch) as run_dir:
            for provider in known_providers():
                modes = (False, True) if provider.startswith("codex") else (False,)
                for legacy in modes:
                    for sid in (None, "retained-session-id"):
                        with self.subTest(provider=provider, legacy=legacy, sid=sid):
                            adapter = get_adapter(provider)
                            node = llm("B", 1, provider=provider, prompt="review $response")
                            handle = Mock(steered=False, killed=False)
                            handle.finish.return_value = ("", "", 0, False)
                            with (patch.dict(os.environ, {"GW_CODEX_EXEC": "1" if legacy else ""}),
                                  patch.object(adapter, "resolve_exe", return_value=provider),
                                  patch.object(adapter, "build_env", return_value={}),
                                  patch.object(adapter, "parse", return_value=InvocationResult(text="reply")),
                                  patch.object(adapter, "start_app_server", create=True,
                                               return_value=handle) as app,
                                  patch("graph_workflow.runners.LiveInvocation", return_value=handle) as cli):
                                status, outputs, metrics, error, _ = runners.run_llm(
                                    node, runners.Inputs({"response": str}, {"response": "draft"}),
                                    run_id="test", run_dir=run_dir, invocation=1,
                                    register_proc=None, interrupted=lambda: False, warn=lambda m: None,
                                    resume_session=sid)
                            self.assertEqual((status, outputs, error), ("ok", {"response": "reply"}, None))
                            if provider.startswith("codex") and not legacy:
                                self.assertEqual(app.call_args.args[1:3], (sid, "review draft"))
                                cli.assert_not_called()
                            else:
                                argv = cli.call_args.args[0].argv
                                if sid:
                                    self.assertIn(sid, argv)
                                    self.assertIn("resume" if legacy else "--resume", argv)
                                    self.assertNotIn("--session-id", argv)
                                else:
                                    self.assertNotIn("--resume", argv)
                                    self.assertNotIn("resume", argv)
                            expected = sid or (None if provider.startswith("codex") else
                                               runners.session_uuid("test", "B", 1))
                            self.assertEqual(metrics["session_id"], expected)
                            self.assertEqual((Path(run_dir) / "prompts" / "B-001.txt").read_text(
                                encoding="utf-8"), "review draft")


if __name__ == "__main__":
    unittest.main()
