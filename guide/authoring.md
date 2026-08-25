# Composing graph-workflow definitions

Addressed to the model writing a definition. Everything here is stated once;
derive consequences from the mechanisms rather than expecting enumerated
cases. When this file and the source disagree, the source wins — the runtime
is small and readable: `graph_workflow/model.py` (declarations), `engine.py`
(scheduling), `expressions.py` (gates), `runners.py` (node execution),
`validation.py` (rejections), `providers/` (the CLIs). Executable proof of
every construct: `tests/graphs/` (each t-file's docstring names the construct
it demonstrates; `t99` collects what validation rejects; the pytest files
beside them assert exact semantics) and `examples/demo_pipeline.py`. Verified
by execution at tag 1.4.5.

## Where your priors mislead

This runtime resembles DAG/pipeline systems just enough to import wrong
assumptions:

- Cycles are legal and ordinary. A back-edge plus a gate is the loop
  construct; nodes re-fire, and `invocations` counts.
- Edges gate; they do not carry data. An edge contributes one boolean term to
  each destination's gating expression; data transfer is a side effect of a
  TRUE term at arming, paired by name.
- Everything is level-triggered. Terms are pure functions of current output
  states — not events, not queues — re-evaluated only when a node they
  mention commits.
- Rank is a scheduling priority, not topology.
- Failure is a value. Exceptions, timeouts, and missing LLM outputs commit
  FALSE with an error recorded; nothing raises across the graph; the run
  continues to quiescence.
- Gating expressions are ordinary Python strings; there is no expression DSL
  and no gate function library.
- LLM nodes are headless CLI sessions (`claude`, `codex`, `grok`) with
  permissions bypassed.

## Shape

A definition is one executable Python file: topology declared as data, a run
of itself started under the `__main__` guard.

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

The guard is required — tests import definitions, and the Monitor GUI loads
them with `run()` intercepted — so module level must stay free of side
effects beyond declarations. Plumb `GW_SEED`/`GW_RUN_DIR` exactly as above;
that is how launchers steer a definition without editing it.

## Execution

1. At run start the Generator (`g.generator`, id `GEN` — not a node: no
   logic, no data, nothing may be upstream of it) flips its output state
   TRUE. Gates referencing it re-evaluate; that seeds the ready set.
2. A node whose gate evaluates TRUE while idle becomes ready and captures its
   inputs: for each declared input name, a deep-copied value from the
   same-named output of the latest-committed supplier, across in-edges whose
   term was TRUE at that evaluation. The snapshot stays frozen while the node
   waits. Validation guarantees only that a supplier is *possible*, not that
   one has committed.
3. The scheduler starts the lowest-rank ready node whose semaphores have free
   slots. Semaphores queue ready nodes; they never cancel them.
4. When an invocation ends, the node's output expression is evaluated over
   its declared outputs. That evaluation is the commit; its boolean is the
   node's output state — the value every downstream term reads.
5. After a commit, exactly the gating expressions mentioning the committed
   node re-evaluate (stochastic gates on its out-edges redraw first). Ready
   nodes whose gates went FALSE are demoted to idle.
6. The run ends when nothing is ready, running, or paused mid-invocation:
   `reason: quiescent` — the clean ending even when branches never fired.
   Other reasons: `stopped`, `commit_limit`, `keyboard_interrupt`.

Two consequences of level-triggering are worth spelling out. A gate over
anything but node state — the clock, a file, a mutated global — is legal
Python but is never re-checked between commits; encode such conditions as
node outputs. And a cycle's gate needs an arm-once first term plus, on the
re-arm term, both an alternation guard and a bound:

```python
gate=("(e_seed and D.invocations == 0)"
      " or (e_back and C.invocations == D.invocations"
      " and D.invocations < MAX_ROUNDS)")
```

Without the alternation guard ("my peer has answered my latest commit") the
still-TRUE back-edge term re-arms D off its own commit. `max_commits=` in
`run()` is the backstop.

Gating decisions and draws are functions of seed and commit history alone,
but wall-clock completion order of concurrently running nodes varies;
`Semaphores(script=1, ...)` when a test asserts ordering.

## Declarations

```python
Graph(name, semaphores=None, pause_message=None, resume_message=None)
Semaphores(script=4, llm=2, human=1, providers=None)
```

`providers` (e.g. `{"claude": 1, "codex": 1}`) subdivides the llm pool — an
LLM node needs a free slot in both, and llm slots are concurrent CLI
sessions, i.e. real spend. `pause_message`/`resume_message` override what a
paused LLM session is told (defaults: `DEFAULT_PAUSE_MESSAGE` /
`DEFAULT_RESUME_MESSAGE`; per-node values override the graph's).
`add_nodes`/`add_edges` are variadic and reject duplicates.

Common to all nodes:

```python
Node(id, rank, inputs=None, outputs=None, gate=None, output_expression=None)
```

- `id` — a non-keyword Python identifier, never `GEN`, unique across nodes
  and edges together (they share the expression namespace). Bind each node
  object to a module variable of the same name; expressions resolve that name
  to the node's runtime handle.
- `rank` — unique int; number in flow order. Lowest ready rank starts first.
- `inputs` / `outputs` — `{name: type}`; types are declarative only, not
  enforced. Choose output names globally before drawing any edge — names are
  the data plane, and when two suppliers share a name, latest commit wins.
  Output names may not collide with the handle metrics listed under
  Expressions.
- `gate` — defaults to the AND of all in-edge terms; a node with no in-edges
  never arms.
- `output_expression` — defaults to `isDefined(...)` ANDed over every
  declared output.

### ScriptNode

```python
ScriptNode(id, rank, fn, **common)
```

`fn(inputs)` on a worker thread; anything goes — files, network,
subprocesses, model calls. `inputs` offers attribute access (`i.x`, declared
names only), mapping access (`i["x"]`, `i.get("x", default)`), and declared
but unsupplied names read as `UNDEFINED` (falsy). Return a dict over declared
output names, the bare value when exactly one output is declared, or `None`
for none. Undeclared keys warn and are dropped; an exception ends the
invocation with the error recorded and, under the default output expression,
a FALSE commit.

### LLMNode

```python
LLMNode(id, rank, provider, model, prompt,
        effort=None, settings=None, cwd=None, extra_args=None,
        timeout_s=1800.0, parse=None,
        pause_message=None, resume_message=None, **common)
```

One invocation = one fresh headless CLI session turn; the session persists
provider-side and is re-entered only by Pause/Resume. Provider API-key env
vars are stripped on purpose: the CLIs must be installed and logged in, and
they run with permissions bypassed — `cwd` (default: the launching process's)
is the agent's working directory and its blast radius.

`prompt` is a `string.Template` string — `$name` substitutes the captured
input, non-strings JSON-encoded, `$$` a literal `$`, and a name never
captured survives as literal `$name` text, silently — or a callable
`prompt(inputs) -> str`; use the callable and branch on `isDefined(...)`
whenever an input is optional. If `response` is declared it receives the raw
reply text; every other declared output is extracted from the last JSON
object in the reply (fenced blocks first), so the prompt must end by
demanding it — "End your reply with exactly one JSON object, no prose after
it, no code fences:" followed by a sketch carrying exactly the declared
names. A declared key missing from the reply stays undefined, and the default
output expression then commits FALSE. `parse(text, raw) -> dict` replaces the
extraction entirely. `effort` maps to the CLI's reasoning-effort control;
`settings` to `claude --settings`; `extra_args` appends verbatim to the CLI
argv; `timeout_s` ends the invocation as an error (FALSE commit).

| provider | CLI | live steering on Pause |
|---|---|---|
| `claude` | `claude -p`, stream-json | yes, mid-turn |
| `codex` | `codex app-server` JSON-RPC | yes (`GW_CODEX_EXEC=1` = legacy `codex exec`, no steering) |
| `grok` | `grok --prompt-file` | no — interrupt, halt delivered on session resume |

Model strings are whatever the installed CLI accepts (`opus` and `fable` are
exercised below).

### HumanNode

```python
HumanNode(id, rank, prompt, shell_cmd=None, timeout_s=None, **common)
```

Writes `human/<id>-<inv>.request.json` in the run dir, launches the
human-input shell (`shell_cmd` overrides the exe), and blocks until the
response file supplies the declared outputs; `timeout_s=None` waits forever.
String answers for non-`str` declared types are `json.loads`-coerced only
when that parses — declare booleans as `bool` and phrase the prompt to demand
`true`/`false`, since `"no"` stays a truthy string. The node commits TRUE for
yes and no alike (both outputs defined); the decision belongs in a downstream
gate on the value. `GW_HUMAN_AUTO` answers human nodes without the shell.

### Edge

```python
Edge(id, inputs, outputs, input_gates=None, output_gates=None, universal_gate=None)
Gate(expression="True", p_invocation=None)
```

First-class N:M; endpoints are node objects or id strings; `g.generator` may
appear only in `inputs`. Gates are `Gate` objects, bare expression strings,
or `None` (identity); `input_gates`/`output_gates` map node id to gate. The
edge contributes exactly one term to each output node's gate:

    term(edge, o) = universal_gate AND output_gate(o)
                    AND, over every input i: input_gate(i) AND output_state(i)

All gates default to True, so a default edge is pure feedforward and an N:M
edge behaves as N×M separate 1:1 edges. An input that never committed, or
committed FALSE, makes the term FALSE, and a FALSE-term edge transfers no
data at capture. Placement is scope: `universal_gate` covers the whole edge
(and owns one shared random draw); `output_gates` are per-destination
(independent draws — per-branch probabilities go here); `input_gates` are
ANDed in like the rest — a FALSE input gate blocks the whole term, it does
not deliver the other inputs. There is no per-edge OR: give the node separate
edges and write `gate="e_a or e_b"`.

Universal and output gates evaluate before input output-states are checked,
including at run start, so guard every read of a node output with
`isDefined(...)`.

Edge-gate expressions additionally see `sampled_value` — the gate's own
seeded draw in [0,1) — and `p_invocation` if declared (must be in [0,1]); the
stochastic idiom is `Gate("sampled_value <= p_invocation", p_invocation=0.35)`.
Each gate owns an RNG stream seeded by `(run seed, gate id)`: draw #0 at run
start, a redraw at every commit of the edge's input nodes, never at
evaluation. Same seed + same definition = same draws, exactly; each redraw of
a non-default gate is a `draw` trace event.

## Expressions

Node gates, edge gates, and output expressions are Python strings eval'd on
demand in one namespace, later entries shadowing: the definition module's
globals (module helpers and constants are therefore usable by name), node
handles for every node id plus `GEN`, `isDefined`/`UNDEFINED`, then context
extras — in a node gate, each in-edge's term bound by edge id (available
nowhere else); in an edge gate, `sampled_value`/`p_invocation`; in an output
expression, the declared outputs by name. Any exception inside an expression
is caught, logged as a `warning` trace event, and the expression counts as
False — the run behaves, but a clean trace has no warnings, which is what the
`isDefined` guards are for.

A handle exposes declared outputs (latest committed value, `UNDEFINED` before
the first commit) plus metrics — the reserved names outputs may not use:
`id`, `rank`, `state` (`"idle"`/`"ready"`/`"running"`; paused reads as
running), `output_state`, `invocations`/`commits` (starts vs commits — a
mid-flight invocation has incremented only the former), `context_size`,
`duration_s`, `usage`, `session_id`, `last_commit_seq`.

## Running

```python
run(graph, seed=None, namespace=None, run_dir=None,
    validate_graph=True, console=True, max_commits=None) -> RunResult
resume_run(graph, trace_path) -> RunResult
```

`seed=None` reads `GW_SEED`, else draws randomly. `run_dir=None` reads
`GW_RUN_DIR`, else creates `runs/<name>-<timestamp>-s<seed%100000>/` beside
the definition. `namespace` defaults to the caller's module globals — which
is how the expression namespace comes to be the definition module; override
only when embedding. `RunResult`: `run_id`, `run_dir`, `trace_path`, `seed`,
`reason`, `states`, `output_state(nid)`, `outputs(nid)`.

A live run polls `control.json` in the run dir (~4×/s; `{"command": "pause"}`
/ `resume` / `stop`; a file already present at startup is stale and ignored —
in-process, `Runtime` has `pause()`/`resume()`/`stop()`). Pause lets scripts
and humans finish and halts each running LLM session politely, holding its
invocation open; Resume completes that same invocation; Stop ends the run
(`reason: stopped`). `resume_run` replays a crashed or abandoned run from its
trace: the definition and seed must be unchanged (replayed draws are checked;
mismatch raises `TraceReplayError`), restored outputs come back in their JSON
trace forms, and invocations in flight at the crash re-run with their
captured inputs.

The run directory: `trace.jsonl` (authoritative, append per event),
`status.json` (live snapshot), `control.json` (yours), `prompts/<node>-<inv>.txt`
(every rendered LLM prompt), `sessions/<node>-<inv>.jsonl` (provider stream
tee), `human/` (request/response files). Trace events, completely:
`run_start`, `generator_fire`, `reeval` (node, value, per-edge terms, cause),
`ready` (captured inputs), `demoted`, `start`, `commit` (outputs,
output_state, error), `draw`, `warning`, `pause_requested`,
`node_interrupted`, `node_pause_acked`, `paused`, `resume_requested`,
`resumed`, `resumed_from_disk`, `run_end` (reason); every event carries `seq`
and `ts`. The trace is the diagnostic instrument: `reeval` explains arming,
`ready` explains data, `commit` explains everything downstream.

| env var | effect |
|---|---|
| `GW_SEED` / `GW_RUN_DIR` | defaults for `run(seed=None, run_dir=None)` |
| `GW_MONITOR` | `0` never launch the Monitor GUI, `1` force, a path = that exe, unset = auto when interactive |
| `GW_HUMAN_AUTO` | JSON file `{"<node id>" or "*": {output: value}}`; answers human nodes, no shell |
| `GW_HUMAN_SHELL` / `GW_HUMAN_SHELL_AUTO` | shell exe override / drive the real shell headlessly |
| `GW_CODEX_EXEC` | `1` = legacy `codex exec` instead of app-server |

## Verification

`python -m graph_workflow.gui validate <file>` executes the file with `run()`
intercepted and prints `{"ok": true, "errors": [], "warnings": []}` when
clean. Errors are structural: duplicate ranks, node/edge id collisions,
output names on the reserved-metrics list, unknown endpoints, anything
upstream of the Generator, a Generator-fed-only node declaring inputs, an
input no upstream output could supply, expression syntax errors,
`p_invocation` out of range, unknown provider or missing model/prompt.
Warnings are curatorial: no in-edges, unresolvable names in expressions,
semaphores for unknown providers, shadowed module names. What validation
cannot see — gate logic, `$name` typos inside prompts, unbounded cycles, a
model ignoring the JSON contract — surfaces only in a run. So every
definition gets both before handover:

```powershell
python -m graph_workflow.gui validate mygraph.py
$env:GW_SEED = "7"; $env:GW_RUN_DIR = "$env:LOCALAPPDATA\Temp\gw-check"
$env:GW_MONITOR = "0"; python mygraph.py       # REASON: quiescent
```

Then read the trace: last event `run_end` with reason `quiescent`, the
expected commits with `output_state: true` and `error: null`, no `warning`
events, no literal `$name` in `prompts/*.txt`. Keep scratch run dirs out of
cloud-synced folders. The same shape makes a pytest: run the file as a
subprocess with pinned seed and tmp run dir, assert on the trace
(`tests/conftest.py` is the pattern; set `GW_HUMAN_AUTO` for human nodes).

## Policy

- Definitions carry no narrative comments. A single-line docstring naming the
  graph's shape is the ceiling; a coupling that would otherwise need a
  comment becomes structure instead — a shared module constant, referenced by
  name from every gate string that depends on it.
- Topology is static; there is no dynamic node spawning. Collections: iterate
  inside one script node, unroll a known N into parallel nodes (`t21`), or
  ride the payload around a bounded cycle (`t05`).
- Every quiescence path is designed: loop bounds, branch escape hatches whose
  constants match the bounds they answer, `max_commits` as backstop.
- Keep script fns and prompt callables as named module-level defs —
  expressions can call them, and the Monitor GUI round-trips them where
  inline lambdas in node kwargs turn opaque. In a GUI-saved file, hand-edit
  only the `# --- gw:helpers ---` section; `# --- gw:graph ---` is
  regenerated wholesale on every GUI save, and `# --- gw:gui ---` is display
  metadata the runtime ignores.

## A full definition

Every remaining construct, once: bounded LLM loop with alternation guard,
convergent edge, stochastic gate, custom output expression, branch with
escape hatch, human approval with the decision in a downstream gate.

```python
"""Bounded draft/critique loop, stochastic audit, conditional publish, human approval."""

import os

from graph_workflow import (
    Edge, Gate, Graph, HumanNode, LLMNode, ScriptNode, Semaphores, isDefined, run,
)

MAX_DRAFTS = 3
PASS_SCORE = 7


def draft_prompt(i):
    lines = [f"Write a 4-6 sentence product pitch for {i.topic}, aimed at {i.audience}."]
    if isDefined(i.notes):
        lines.append(f"This is a revision. Address this reviewer feedback: {i.notes}")
    lines.append(
        "End your reply with exactly one JSON object, no prose after it, "
        'no code fences: {"draft": "<the pitch>"}'
    )
    return "\n".join(lines)


def assemble(i):
    return {"final_md": f"{i.draft}\n\n---\nscore {i.score}/10 - {i.notes}"}


g = Graph("pitch-loop", semaphores=Semaphores(script=4, llm=2, human=1))

S1 = ScriptNode(
    "S1", rank=0,
    fn=lambda i: {"topic": "a pocket spectrometer", "audience": "field geologists"},
    outputs={"topic": str, "audience": str},
)

L1 = LLMNode(
    "L1", rank=1,
    provider="claude", model="opus", effort="medium",
    prompt=draft_prompt,
    inputs={"topic": str, "audience": str, "notes": str},
    outputs={"response": str, "draft": str},
    cwd=os.environ.get("GW_RUN_DIR"),
    timeout_s=600,
    gate=(
        "(e_topic and L1.invocations == 0)"
        " or (e_back and L2.invocations == L1.invocations"
        " and L1.invocations < MAX_DRAFTS"
        " and isDefined(L2.score) and L2.score < PASS_SCORE)"
    ),
)

L2 = LLMNode(
    "L2", rank=2,
    provider="claude", model="fable", effort="high",
    prompt=(
        "You are a strict pitch reviewer. The product: $topic. The draft:\n\n"
        "$draft\n\nScore it 0-10 for specificity and punch, and give one "
        "actionable sentence of feedback. "
        "End your reply with exactly one JSON object, no prose after it, "
        'no code fences: {"score": <integer 0-10>, "notes": "<one sentence>"}'
    ),
    inputs={"topic": str, "draft": str},
    outputs={"response": str, "score": int, "notes": str},
    cwd=os.environ.get("GW_RUN_DIR"),
    timeout_s=600,
)

S2 = ScriptNode(
    "S2", rank=3,
    fn=lambda i: i.score < 5,
    inputs={"score": int},
    outputs={"flag": bool},
    output_expression="isDefined(flag) and not flag",
)

S3 = ScriptNode(
    "S3", rank=4,
    fn=assemble,
    inputs={"draft": str, "score": int, "notes": str},
    outputs={"final_md": str},
)

H1 = HumanNode(
    "H1", rank=5,
    prompt=(
        "A pitch is ready to publish.\n\n$final_md\n\n"
        "Approve publication? Set `approved` true or false; `remarks` optional."
    ),
    inputs={"final_md": str},
    outputs={"approved": bool, "remarks": str},
    timeout_s=600,
)

S4 = ScriptNode(
    "S4", rank=6,
    fn=lambda i: {"receipt": f"published with remarks: {i.get('remarks', '')}"},
    inputs={"remarks": str},
    outputs={"receipt": str},
    gate="e_go and H1.approved",
)

g.add_nodes(S1, L1, L2, S2, S3, H1, S4)
g.add_edges(
    Edge("gen_S1", inputs=[g.generator], outputs=[S1]),
    Edge("e_topic", inputs=[S1], outputs=[L1]),
    Edge("e_review", inputs=[S1, L1], outputs=[L2]),
    Edge("e_back", inputs=[L2], outputs=[L1]),
    Edge("e_audit", inputs=[L2], outputs=[S2],
         output_gates={"S2": Gate("sampled_value <= p_invocation",
                                  p_invocation=0.35)}),
    Edge("e_final", inputs=[L1], outputs=[S3]),
    Edge("e_pub", inputs=[L2], outputs=[S3],
         universal_gate="isDefined(L2.score) and"
                        " (L2.score >= PASS_SCORE or L1.invocations >= MAX_DRAFTS)"),
    Edge("e_ship", inputs=[S3], outputs=[H1]),
    Edge("e_go", inputs=[H1], outputs=[S4]),
)

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ["GW_SEED"]) if os.environ.get("GW_SEED") else None,
        run_dir=os.environ.get("GW_RUN_DIR") or None,
        max_commits=50,
    )
    print("REASON:", result.reason)
    for nid in ("S1", "L1", "L2", "S2", "S3", "H1", "S4"):
        st = result.states[nid]
        print(nid, st.output_state, st.invocations)
```

At any seed it quiesces: the loop exits by score or by exhausting
`MAX_DRAFTS` (the escape hatch on `e_pub` keeps S3 reachable either way), S2
fires per its seeded draws, S4 only on approval. Verified at seed 7 with
`GW_HUMAN_AUTO` = `{"H1": {"approved": true, "remarks": "ship it"}}`: 34
trace events, all six commits TRUE, S2 idle (its draw, 0.559, exceeds 0.35),
zero warnings, `run_end` reason `quiescent`.
