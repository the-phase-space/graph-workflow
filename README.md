# graph-workflow

A Python runtime library for composing and executing directed graphs of
**script**, **LLM**, and **human** nodes, with first-class N-to-M edges,
Python-native gating expressions, seeded stochastic gates, a disk-persisted
trace, and Pause/Resume over live LLM sessions.

The normative specification is `Development/graph-workflow-schema.md`. This
README is only a usage sketch.

## Model in one paragraph

A **graph definition** is an executable Python file: it declares topology as
data via this API and invokes the runtime — executing the file starts a run.
**Edges** are first-class: N input nodes, M output nodes, one gate per
connected node plus one universal gate (all defaulting to `True`); each edge
contributes exactly one boolean term to each output node's **gating
expression** (default: AND of in-edge terms). A node becomes `ready` when its
gating expression evaluates TRUE, `running` when the type-level (and, for LLM
nodes, provider-level) semaphores permit — lowest rank first — and commits
when its invocation ends: the **output expression** (default: every declared
output `isDefined`) evaluates, and that boolean becomes the node's **output
state**, which its out-edges consume. Commits trigger re-evaluation of every
gating expression referencing the committer, in reverse-rank order. The
**Generator**'s flip to TRUE at run start seeds the initial ready set. Cycles
are just back-edges; loop bounds are ordinary gate conditions
(`A.invocations < 3`). The run is over when nothing is ready and nothing is
running.

## Quick start

```python
# my_graph.py — executable graph definition
from graph_workflow import Edge, Gate, Graph, LLMNode, ScriptNode, Semaphores, run

g = Graph("example", semaphores=Semaphores(script=4, llm=2, human=1,
                                           providers={"claude": 1}))

S = ScriptNode("S", rank=0, fn=lambda i: {"topic": "tides"},
               outputs={"topic": str})
L = LLMNode("L", rank=1, provider="claude", model="sonnet", effort="low",
            prompt='Write one sentence about $topic. Reply as {"sentence": "..."}',
            inputs={"topic": str},
            outputs={"response": str, "sentence": str})
g.add_nodes(S, L)
g.add_edges(
    Edge("gen_S", inputs=[g.generator], outputs=[S]),
    Edge("e_SL", inputs=[S], outputs=[L]),
)

if __name__ == "__main__":
    result = run(g, seed=42)          # python my_graph.py starts the run
    print(result.reason, result.outputs("L"))
```

- Gating expressions are strings of ordinary Python evaluated in the graph
  file's own namespace plus node handles (`A.some_output`, `A.invocations`,
  `A.context_size`, `A.output_state`) and, inside edge gates, the gate's own
  seeded draw: `Gate("sampled_value <= p_invocation", p_invocation=0.3)`.
- LLM prompts are `string.Template` strings (`$input_name`) or callables.
  Declared outputs beyond `response` are extracted from the last JSON object
  in the reply (or a per-node `parse=` callable).
- Providers: `claude`, `codex`, `grok` — headless CLI sessions with API-key
  environment variables stripped.

## Runs, trace, control

Each run writes `runs/<run-id>/` beside the graph file (override with
`run_dir=`): `trace.jsonl` (every state change; the substrate for fault
tolerance), `status.json` (live snapshot for the monitor GUI), `prompts/`,
`human/`. Control a live run programmatically (`Runtime.pause/resume/stop`)
or externally by writing `{"command": "pause"}` to `control.json` in the run
directory. `Pause` delivers the halt message to every running LLM session —
steered live into the session where the provider exposes a channel (claude
via stream-json stdin, codex via the app-server `turn/steer` method: the
message reaches the agent after its current tool call, no interrupt), with
an interrupt + session-bound-resume fallback where none exists (headless
grok) — and the session terminates once the model ends its following turn. `Resume` re-enters each session with
`continue`, and that reply completes the open invocation (both messages
overridable per graph and per node). `resume_run(graph, trace_path)` resumes
a crashed or paused run from disk.

## Human-input shell

`human-shell/` is a Tauri 2 app. Build:

```powershell
$env:CARGO_TARGET_DIR = "$env:LOCALAPPDATA\Temp\Graph-Workflow-build"
cd human-shell/src-tauri; cargo tauri build --no-bundle
Copy-Item "$env:CARGO_TARGET_DIR\release\human-shell.exe" ..\bin\
```

The runtime finds it at `human-shell/bin/human-shell.exe` (override:
`GW_HUMAN_SHELL` or per-node `shell_cmd=`). Test hooks: `GW_HUMAN_AUTO`
(runtime bypass mapping file) and `GW_HUMAN_SHELL_AUTO` (the real exe
answers headlessly).

## Tests

```powershell
python -m pytest tests/test_battery.py tests/test_control.py tests/test_human.py   # fast, no LLM calls
python -m pytest tests/test_llm.py                                                 # real provider CLIs
```

Versioning: lightweight git tags are the sole source of truth.
