# graph-workflow reference

Exact semantics and API for composing graph definitions. When this document
and the source disagree, the source wins — the runtime is small and readable:
`graph_workflow/model.py` (declarations), `engine.py` (scheduling),
`expressions.py` (gates), `runners.py` (node execution), `validation.py`
(what gets rejected), `providers/` (the three CLIs). Verified against tag
1.4.5.

---

## The shape of a definition

A graph definition is an **executable Python file**: it declares topology as
data and starts a run of itself when executed. One graph per file. The
canonical skeleton:

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

The `__main__` guard is required in practice: tests import definition files,
and the Monitor GUI loads them with `run()` intercepted. Always plumb
`GW_SEED` and `GW_RUN_DIR` exactly as above so launchers can steer the file
without editing it.

## Execution model

1. At run start the Generator's output state flips TRUE — the run's first
   event. Every gating expression that references it re-evaluates; that
   seeds the initial ready set.
2. A node whose gating expression evaluates TRUE while it is idle becomes
   **ready** (armed). Arming **captures its inputs** — a deep-copied
   snapshot of upstream outputs. The snapshot does not refresh while the
   node waits.
3. The scheduler starts the **lowest-rank** ready node whose semaphores have
   free slots, then rescans. Rank is a scheduling priority, not topology.
4. When an invocation ends, the runtime evaluates the node's **output
   expression** over its declared outputs. That evaluation **is the
   commit**; the boolean result is the node's **output state**.
5. After a commit, exactly the gating expressions that mention the committed
   node re-evaluate, highest rank first. Ready nodes whose gates went FALSE
   are **demoted** back to idle. Stochastic gates on the committed node's
   out-edges redraw first.
6. The run is over when no node is ready, none is running, and none is
   paused mid-invocation. Reason `quiescent`. Other end reasons: `stopped`,
   `commit_limit`, `keyboard_interrupt`.

Node lifecycle: `idle -> ready -> running -> (commit) -> idle`, with
`ready -> idle` on demotion and `running -> paused -> running` when Pause
interrupts an LLM invocation (Resume completes the *same* invocation).

Everything is **level-triggered**: an edge term is a pure function of
current output states, not an event or a queue. Once an upstream node has
committed TRUE, the term stays TRUE at every later evaluation. Consequences:

- Gates re-evaluate **only** when a node they mention commits (or the
  Generator fires). A gate over wall-clock time or a mutated global will not
  be spontaneously rechecked.
- A gate that mentions its own node (`A.invocations`) re-evaluates at A's
  own commits — this is how loops re-arm and how they stop.
- Cycles need two guards, both stated in the looping node's gate: an
  invocation bound (`A.invocations < 3`) and an alternation guard
  (`B.invocations == A.invocations`, i.e. "my peer has answered my latest
  commit"). Without the alternation guard the node re-arms from its own
  commit's re-evaluation, because the back-edge term is still TRUE.

Concurrency note: gate draws and gating decisions depend only on the seed
and the commit history, but the wall-clock completion order of nodes running
*concurrently* can vary between runs. When byte-identical traces matter
(tests), serialize with `Semaphores(script=1, ...)`.

## Graph

```python
Graph(name, semaphores=None, pause_message=None, resume_message=None)
```

- `name` — free string; becomes part of the run id.
- `pause_message` / `resume_message` — graph-wide overrides of what a paused
  LLM session is told; per-node values override these. Defaults:
  `"Halt for now. Do not call any new tools until I say 'continue'"` /
  `"continue"`.
- `g.add_nodes(...)`, `g.add_edges(...)` (aliases `add_node`/`add_edge`) —
  variadic, chainable, reject duplicates.
- `g.generator` — the Generator, id `"GEN"`. Not a node: no logic, no data
  outputs, nothing may be upstream of it. Use it in edge `inputs`.

## Semaphores

```python
Semaphores(script=4, llm=2, human=1, providers=None)
```

Global concurrency caps by node type, all positive ints. `providers` (e.g.
`{"claude": 1, "codex": 1}`) subdivides the llm pool: an LLM node starts
only when the `llm` semaphore AND its provider's semaphore both have a free
slot. A provider absent from the dict is bounded only by `llm`. Semaphores
queue ready nodes; they never cancel them.

## Nodes — common declaration

```python
Node(id, rank, inputs=None, outputs=None, gate=None, output_expression=None)
```

- `id` — a valid non-keyword Python identifier, unique among nodes, never
  `"GEN"`, and never equal to any edge id (nodes and edges share one
  namespace inside gating expressions). Bind the node object to a module
  variable of the same name — inside expressions that name resolves to the
  node's runtime handle, which is what you want.
- `rank` — unique int per node. Lowest-rank ready node starts first;
  re-evaluation after a commit runs highest rank first. Number in flow
  order.
- `inputs` / `outputs` — `{name: type}` dicts. Names must be identifiers.
  Types are declarative (shown to humans and the GUI; `str`-or-class both
  fine); the runtime does not enforce them. Output names may not be any of
  the reserved metric names: `id`, `rank`, `state`, `output_state`,
  `invocations`, `commits`, `context_size`, `duration_s`, `usage`,
  `session_id`, `last_commit_seq`.
- `gate` — the node's gating expression over its in-edges, a Python string.
  `None` means the default: the AND of all in-edge terms. A node with no
  in-edges can never become ready under the default.
- `output_expression` — a boolean Python string over the declared outputs,
  evaluated at invocation end (that evaluation is the commit; the boolean is
  the output state). `None` means the default: `isDefined(o)` ANDed over
  every declared output (vacuously TRUE with no declared outputs).

### ScriptNode

```python
ScriptNode(id, rank, fn, **common)
```

`fn(inputs)` runs on a worker thread. The `inputs` argument supports
attribute access (`i.x` — declared names only, `AttributeError` otherwise),
mapping access (`i["x"]`, `i.get("x", default)`, iteration), and declared
but unsupplied names read as `UNDEFINED` (falsy). Return:

- a dict over declared output names (undeclared keys warn and are dropped),
- the bare value, when exactly one output is declared,
- `None` for no outputs.

An exception inside `fn` ends the invocation with an error recorded and no
outputs — under the default output expression the node commits FALSE. The
run continues; failure is a value, not a crash. Scripts may do anything:
files, network, subprocesses, even model calls.

### LLMNode

```python
LLMNode(id, rank, provider, model, prompt,
        effort=None, settings=None, cwd=None, extra_args=None,
        timeout_s=1800.0, parse=None,
        pause_message=None, resume_message=None, **common)
```

One node invocation = one fresh CLI session turn with that provider; the
session persists provider-side and is re-entered only by Pause/Resume.

- `prompt` — either a `string.Template` string (`$name` substitutes the
  captured input of that name; non-strings are JSON-encoded; `$$` is a
  literal `$`; a name that was never captured is left as literal `$name`
  text) or a callable `prompt(inputs) -> str`. Use the callable form when
  any input is optional.
- Outputs: if `response` is declared it receives the raw reply text. Every
  other declared output is extracted from the last JSON object in the reply
  (fenced ```` ```json ```` blocks first, last one wins; then the last
  balanced `{...}` in the raw text). So the prompt must end by demanding
  exactly one JSON object carrying those names. Extracted keys that are not
  declared warn and are dropped; declared keys missing from the reply stay
  undefined, and the default output expression then commits FALSE.
- `parse` — `parse(text, raw) -> dict` replaces the JSON extraction
  entirely (`raw` is the provider's parsed result event/stream).
- `cwd` — the agent's working directory; its tools operate there. Default:
  the launching process's cwd. Point it somewhere intentional (a workspace,
  or the run dir via `os.environ.get("GW_RUN_DIR")`).
- `effort` — reasoning effort string passed to the CLI (tests use `"low"`).
- `timeout_s` — per-invocation wall clock; timeout ends the invocation with
  an error (FALSE commit), default 1800.
- Provider API-key env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `XAI_API_KEY`) are stripped from the CLI environment — the CLIs must be
  logged in on the machine; nothing falls through to direct API calls.

Provider table (models are the ones exercised in this repo's tests; prefer
what the installed CLI accepts today):

| provider | CLI | models seen | live steering | session id |
|---|---|---|---|---|
| `claude` | `claude -p`, stream-json in/out | `haiku`, `sonnet` | yes — Pause message reaches the live session mid-turn | assigned by runtime (deterministic UUID) |
| `codex` | `codex app-server` JSON-RPC | `gpt-5.6-luna`, `gpt-5.6-terra` | yes — `turn/steer` | assigned by provider (thread id) |
| `grok` | `grok --prompt-file` | `grok-4.5` | no — Pause interrupts the turn, halt delivered via session resume | assigned by runtime |

All three run with permissions bypassed (`--dangerously-skip-permissions` /
`danger-full-access` / `--yolo`): treat `cwd` as the agent's blast radius.
`GW_CODEX_EXEC=1` falls back to legacy `codex exec` (no steering).

### HumanNode

```python
HumanNode(id, rank, prompt, shell_cmd=None, timeout_s=None, **common)
```

The runtime writes `human/<id>-<inv>.request.json` in the run dir, launches
the human-input shell (Tauri app at `human-shell/bin/`), and blocks until
the human submits the response file with the declared outputs. `prompt` as
in LLMNode. `timeout_s=None` waits forever. Response values that arrive as
strings for non-`str` declared types are `json.loads`-coerced when possible
(so a `bool` output should be answered `true`/`false`, not `"yes"`).
Unattended runs: `GW_HUMAN_AUTO=<json file>` maps node id (or `"*"`) to
canned outputs and bypasses the shell entirely; `GW_HUMAN_SHELL_AUTO` drives
the real shell headlessly.

## Edges and gates

```python
Edge(id, inputs, outputs, input_gates=None, output_gates=None, universal_gate=None)
```

First-class N:M edges: `inputs`/`outputs` are node objects (or id strings);
`g.generator` may appear only in `inputs`. Gates are `Gate` objects, bare
expression strings, or `None` (identity). `input_gates`/`output_gates` map
node id -> gate for that endpoint.

The edge contributes exactly **one term** to each output node's gating
expression:

```
term(edge, o) = universal_gate
                AND output_gate(o)
                AND over every input i: input_gate(i) AND output_state(i)
```

- With every gate defaulted the edge is pure feedforward, and an N:M edge
  behaves identically to N×M separate 1:1 edges.
- An input whose output state is unset (never committed) or FALSE makes the
  term FALSE. A FALSE gate anywhere makes the term FALSE, and a FALSE-term
  edge **transfers no data** at capture time.
- Universal and output gates evaluate **before** input output-states are
  checked — including at run start, before anything upstream has committed.
  Any gate expression that reads a node output must therefore guard with
  `isDefined(...)`: comparing `UNDEFINED` raises, and an errored gate
  evaluates FALSE with a warning in the trace.

```python
Gate(expression="True", p_invocation=None)
```

Inside an edge-gate expression two extra names exist: `sampled_value` — the
gate's own seeded draw in [0,1) — and `p_invocation` (if declared; must be
in [0,1]). The stochastic idiom:

```python
output_gates={"X": Gate("sampled_value <= p_invocation", p_invocation=0.5)}
```

Draw mechanics: every gate owns an RNG stream seeded from
`"{run_seed}:{gate_id}"` (gate ids: `edge::univ`, `edge::in::node`,
`edge::out::node`). Draw #0 happens at run start; a gate **redraws at every
commit of its edge's input nodes**. Evaluation never advances a stream. An
edge's universal gate has one stream shared across the whole edge; per-node
input/output gates draw independently. Same seed + same graph = same draws,
exactly; non-default gates log each redraw as a `draw` trace event.

## Expressions

Gating expressions, edge gates, and output expressions are strings of
ordinary Python, `eval`-ed at need. The namespace, later entries winning:

1. the definition module's own globals (imports and helpers included),
2. node handles for every node id + `GEN` (so node ids shadow same-named
   module variables — harmless under the bind-same-name idiom),
3. `isDefined`, `UNDEFINED`,
4. context extras: in a **node gate**, each in-edge's term bound by edge id;
   in an **edge gate**, `sampled_value` and `p_invocation`; in an **output
   expression**, the node's declared outputs bound by name.

Edge-term names are available **only** in node gates. There is no gate
function library; write Python, call your own module-level helpers.

A node handle exposes declared outputs plus runtime metrics as attributes:

| attribute | meaning |
|---|---|
| `A.<output>` | latest committed value, `UNDEFINED` before first commit |
| `A.output_state` | boolean from last commit, `UNDEFINED` before it |
| `A.state` | `"idle"` / `"ready"` / `"running"` (paused reads as running) |
| `A.invocations` / `A.commits` | count of starts / of commits (a mid-flight invocation has incremented `invocations` but not yet `commits`) |
| `A.last_commit_seq` | trace seq of latest commit, -1 before |
| `A.context_size`, `A.duration_s`, `A.usage`, `A.session_id` | LLM metrics (0 / 0.0 / {} / None default) |

`GEN.output_state` is FALSE before the run starts, TRUE after. Any exception
inside an expression (NameError, TypeError, AttributeError...) is caught,
logged as a `warning` trace event, and the expression counts as **False**.

## The data plane

Inputs pair with outputs **by name**. At arming, the runtime scans the
node's in-edges whose term was TRUE at that evaluation; for each declared
input name, it takes the value from the upstream declared output of the same
name — **latest commit wins** when several suppliers define the same name.
Values are deep-copied at capture. Validation rejects a declared input that
no upstream output on any in-edge could ever supply. Unconsumed outputs are
harmless. The captured snapshot is frozen until the node commits and arms
again.

## Entry points and control

```python
run(graph, seed=None, namespace=None, run_dir=None,
    validate_graph=True, console=True, max_commits=None) -> RunResult
```

- `seed=None` reads `GW_SEED`, else draws a random 63-bit seed.
- `run_dir=None` reads `GW_RUN_DIR`, else creates
  `<definition dir>/runs/<name>-<timestamp>-s<seed%100000>/`.
- `max_commits` — total-commit hard stop (`reason: commit_limit`); the
  safety valve for cycles.
- Executing a definition auto-launches the Monitor GUI when
  `monitor/bin/graph-workflow-monitor.exe` exists and stderr is an
  interactive console; `GW_MONITOR=0` suppresses, `=1` forces, a path runs
  that exe.

`RunResult`: `run_id`, `run_dir`, `trace_path`, `seed`, `reason`, `states`
(dict of per-node state), plus `result.output_state(nid)` and
`result.outputs(nid)`.

```python
resume_run(graph, trace_path) -> RunResult
```

Resumes a crashed or paused-and-dead run from its trace. The definition and
seed must be unchanged — replayed draws are verified against recorded ones
and a mismatch raises `TraceReplayError`. Outputs come back JSON-restored
(non-JSON values as repr strings). Invocations that were running at the
crash re-run with their captured inputs.

A live run obeys `control.json` written into the run directory:
`{"command": "pause"}` (or `resume` / `stop`), polled 4×/s by mtime; a file
already present at startup is ignored as stale. In-process, `Runtime` has
`pause()` / `resume()` / `stop()` methods. Pause lets scripts and human
invocations finish, and tells each running LLM session to halt (steered live
into the session where the provider supports it, else interrupt + halt via
session resume). Resume re-enters each paused session with the resume
message and completes the same invocation; declared outputs missing from the
continue-reply are also mined from what the model said when pausing.

## The run directory

| file | writer | content |
|---|---|---|
| `trace.jsonl` | runtime, append+flush every event | the authoritative record; one JSON event per line |
| `status.json` | runtime, atomic rewrite every event | live snapshot for humans/GUI (outputs previewed to 800 chars) |
| `control.json` | **you** / the GUI | pause/resume/stop commands |
| `prompts/<node>-<inv>.txt` | runtime | every rendered LLM prompt (`-resume`/`-pause` tags for those turns) |
| `sessions/<node>-<inv>.jsonl` | runtime | live tee of the provider session stream |
| `human/<node>-<inv>.request.json` / `.response.json` | runtime / human shell | the human-node exchange |

Trace event vocabulary (complete): `run_start`, `generator_fire`, `reeval`
(fields: node, value, terms-by-edge-id, cause), `ready` (node, captured
inputs), `demoted`, `start` (node, invocation, resumed), `commit` (node,
invocation, outputs, output_state, metrics, error), `draw` (gate, index,
value), `warning`, `pause_requested`, `node_interrupted`,
`node_pause_acked`, `paused`, `resume_requested`, `resumed`,
`resumed_from_disk`, `run_end` (reason). Every event carries `seq`
(monotonic) and `ts`.

## Environment variables

| var | effect |
|---|---|
| `GW_SEED` | seed used when `run(seed=None)` |
| `GW_RUN_DIR` | run dir used when `run(run_dir=None)` |
| `GW_MONITOR` | `0` never launch GUI, `1` force, path = that exe, unset = auto when interactive |
| `GW_HUMAN_AUTO` | JSON file mapping node id (or `"*"`) -> canned human outputs; bypasses the shell |
| `GW_HUMAN_SHELL` | path override for the human-input shell exe |
| `GW_HUMAN_SHELL_AUTO` | auto-response mode passed to the real shell (headless) |
| `GW_CODEX_EXEC` | `1` = legacy `codex exec` path instead of app-server |

## Validation

`run()` validates by default; standalone check:
`python -m graph_workflow.gui validate <file>` prints
`{"ok": true, "errors": [], "warnings": []}` on a clean definition. Loading
**executes the file** (with `run()` intercepted), so module-level side
effects run.

Rejected (errors): duplicate ranks; a name used as both node id and edge id;
output names colliding with reserved metrics; unknown edge endpoints;
anything upstream of the Generator; a primary node (in-edges only from the
Generator) declaring data inputs; a declared input no upstream output could
supply; expression syntax errors; `p_invocation` outside [0,1]; unknown
provider / missing model / missing prompt.

Warnings (returned, and logged into the trace at run start): a node with no
in-edges; an expression referencing a name that is not a node, edge, helper,
or builtin; a semaphore for an unknown provider; a module variable shadowed
by a node/edge handle.

**Not checked — these only surface as behavior:** contradictory gates (node
never arms), unbounded cycles (use bounds + `max_commits`), missing
alternation guards (double-fires), missing `isDefined` guards (warning spam,
terms stuck FALSE), `$name` typos in prompts (literal text survives), an LLM
reply that ignores the JSON contract (outputs undefined, FALSE commit).

## The Monitor GUI and the structured format

Executing a definition auto-attaches the Monitor (see `GW_MONITOR`). The GUI
edits definitions through `python -m graph_workflow.gui load|validate|save|
template`. A GUI-saved file has three marked sections: `# --- gw:helpers ---`
(your hand-written Python, preserved verbatim), `# --- gw:graph ---`
(regenerated on every save), and `# --- gw:gui ---` (display metadata as
comments — labels, canvas positions; the runtime never reads it). Hand
edits inside the graph section vanish at the next GUI save; edit helpers,
or change topology in the GUI. Hand-written files load fine (helper code is carved into the
contract automatically), but the GUI refuses to save over a non-structured
file without force. GUI-generated ids look like `Scr1`, `LCl1`, `LCo1`,
`LGr1`, `Hum1`, `e1` — same species as hand-written ids, just generated.

For lossless GUI round-trips keep script fns and prompt callables as named
module-level defs; lambdas inline in node kwargs survive execution but can
turn opaque in the contract.
