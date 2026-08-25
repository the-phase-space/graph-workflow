# Composing graph-workflow definitions

This folder is the working guide for anyone writing a graph definition —
an executable Python file that declares a directed graph of script, LLM,
and human nodes and starts a run of itself. Read this page, then copy
`template.py` and prune it. Everything here was verified by executing it
against the runtime at tag 1.4.5; when the guide and the source disagree,
the source wins (`graph_workflow/model.py`, `engine.py`, `expressions.py`,
`runners.py`, `validation.py`).

## Discard these assumptions

This runtime resembles familiar DAG/pipeline systems just enough to
mislead. Points where it differs from what that resemblance suggests:

- **Cycles are legal and ordinary.** A back-edge plus a gate is the loop
  construct. Nodes re-fire; `invocations` counts.
- **Edges gate; they do not merely carry data.** An edge contributes a
  boolean *term* to each destination's gating expression. Data transfer is
  a side effect of a TRUE term at arming.
- **Data pairs by name, not by port.** A declared input is filled by an
  upstream declared output with the same name. Two suppliers of one name:
  latest commit wins.
- **Everything is level-triggered.** Terms are pure functions of current
  output states — not events, not queues. They are re-evaluated only when
  a node mentioned by them commits. Loops therefore need explicit
  alternation guards and bounds.
- **Rank is a scheduling priority, not topology.** Unique int per node;
  lowest-rank ready node starts first. Order comes from gates alone.
- **Failure is a value.** Exceptions, timeouts, and missing LLM outputs
  commit FALSE with an error recorded; the run continues and ends
  `quiescent`. Nothing raises across the graph.
- **Gating expressions are ordinary Python strings** evaluated in the
  definition module's own namespace plus node handles (`L2.score`,
  `A.invocations`) — there is no separate expression language to learn,
  and no gate function library to look for.
- **LLM nodes are headless CLI sessions** (`claude`, `codex`, `grok` must
  be installed and logged in; API keys are stripped on purpose), with
  permissions bypassed — the node's `cwd` is the agent's blast radius.

## The minimal definition

```python
import os
from graph_workflow import Edge, Graph, ScriptNode, Semaphores, run

g = Graph("my-graph", semaphores=Semaphores(script=4, llm=2, human=1))

A = ScriptNode("A", rank=0, fn=lambda i: {"x": 21}, outputs={"x": int})
B = ScriptNode("B", rank=1, fn=lambda i: {"y": i.x * 2},
               inputs={"x": int}, outputs={"y": int})
g.add_nodes(A, B)
g.add_edges(
    Edge("gen_A", inputs=[g.generator], outputs=[A]),
    Edge("e_AB", inputs=[A], outputs=[B]),
)

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ["GW_SEED"]) if os.environ.get("GW_SEED") else None,
        run_dir=os.environ.get("GW_RUN_DIR") or None,
    )
    print("REASON:", result.reason)
```

The Generator (`g.generator`, id `GEN`) flips TRUE at run start and arms
the nodes fed only by it; commits cascade from there; the run ends when
nothing is ready and nothing is running (`quiescent`).

## The rules

1. One graph per file; `run(g, ...)` under the `__main__` guard; the file
   itself is the entry point. Always plumb `GW_SEED`/`GW_RUN_DIR` as above.
2. Data pairs by name: every declared input needs an upstream output of the
   same name on some in-edge (validated). Latest commit wins. Inputs are
   snapshotted when the node arms, not when it starts.
3. Every node: unique identifier id (distinct from edge ids), unique int
   rank.
4. Default node gate = AND of all in-edge terms; default edge = pure
   feedforward; a node with no in-edges never arms.
5. Level-triggered, re-evaluated only at commits of mentioned nodes.
   Cycles need an invocation bound and an alternation guard
   (`peer.invocations == mine.invocations`).
6. Commit = output-expression evaluation; the boolean is the output state
   every downstream term reads. Failures commit FALSE; they don't crash.
7. `isDefined()`-guard every gate expression that reads a node output.
8. LLM structured outputs: declare them, and end the prompt demanding
   "exactly one JSON object" carrying exactly those names.
9. Ensure some path always reaches quiescence: loop bounds, branch escape
   hatches, `max_commits=` as backstop.
10. Verify every definition before handing it over:

```powershell
python -m graph_workflow.gui validate mygraph.py     # {"ok": true, ...}
$env:GW_SEED = "7"; $env:GW_RUN_DIR = "$env:LOCALAPPDATA\Temp\gw-check"
$env:GW_MONITOR = "0"; python mygraph.py             # REASON: quiescent
```

then read `trace.jsonl` in the run dir: last event `run_end`, expected
commits with `output_state: true`, no `warning` events, no literal `$name`
in `prompts/*.txt`. Keep scratch run dirs out of cloud-synced folders.

## This folder

| file | use it when |
|---|---|
| `template.py` | starting any new definition — every construct, annotated, executable |
| `reference.md` | you need exact signatures, semantics, defaults, tables |
| `howto.md` | you have a goal: loops, branches, probabilities, structured LLM output, human gates, pause/resume, recovery, testing |
| `faq.md` | you have a symptom: node won't arm, loop runs away, `$name` in a prompt, FALSE commits, warning spam |

Proven working examples beyond this folder: `tests/graphs/` (each t-file's
docstring states the one construct it demonstrates, t99 shows what
validation rejects, and the pytest files beside them assert the exact
semantics) and `examples/demo_pipeline.py`.
