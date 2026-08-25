# graph-workflow FAQ

Indexed by symptom. Each entry gives the mechanism first, then the fix. The
diagnostic instrument for all of them is the run's `trace.jsonl`: every
gating decision is a `reeval` event carrying the per-edge terms and the
cause; every arming is a `ready` event carrying the captured inputs; every
commit carries outputs, output state, and any error.

---

## Arming and firing

### A node never becomes ready

Work through these in order — each is a distinct mechanism:

1. **No in-edge.** The default gate is the AND of in-edge terms; with zero
   edges there is nothing to AND. Validation warns:
   `node 'X' has no in-edges`.
2. **An upstream node committed FALSE.** A term requires every input's
   output state to be TRUE. Find the upstream `commit` event and check
   `output_state` and `error` — a script exception, LLM timeout, or missing
   declared output all commit FALSE by design.
3. **A gate condition is simply false** — check the node's `reeval` events:
   `"terms"` shows each in-edge term, `"value"` the gate result, `"cause"`
   which commit triggered the evaluation.
4. **The gate never re-evaluated at all** (no `reeval` events after some
   point). Gates re-evaluate only when a node they mention commits. A gate
   over anything else — time, files, a mutated global — is never rechecked.
   Rewrite the condition in terms of node outputs and metrics.
5. **An expression error.** An errored gate counts as False and logs:
   `expression error in node X gate: TypeError: ... (expr '...') — treated
   as False`. See the `isDefined` entry below.

A semaphore is never the answer to "never ready" — semaphores queue ready
nodes, they don't cancel them.

Quick filter for one node's story:

```python
import json
evs = [json.loads(l) for l in open("trace.jsonl", encoding="utf-8")]
print(*[e for e in evs if e.get("node") == "X"], sep="\n")
```

### A node armed, then a `demoted` event sent it back to idle

Between arming and starting, some other commit re-evaluated its gate to
FALSE (stochastic gates redraw at every commit of their edge's input nodes,
so a queued node behind a coin-flip gate can lose its slot on the next
flip). Demotion is normal scheduling, not an error. If the node must run
once armed, make its gate depend only on conditions that cannot regress.

### A loop node re-fires immediately / runs away

Edge terms are level-triggered: after the peer commits TRUE once, the
back-edge term stays TRUE forever, and the looping node's gate re-evaluates
at its **own** commits too (it mentions itself via `invocations`). Without
an alternation guard it re-arms off its own commit. A loop gate needs both
guards, together:

```python
gate=("(e_seed and A.invocations == 0)"
      " or (e_back and B.invocations == A.invocations and A.invocations < 3)")
```

`B.invocations == A.invocations` means "my peer has answered my latest
commit"; `A.invocations < 3` is the budget. Belt and braces: pass
`max_commits=` to `run()` so a wrong loop stops with reason `commit_limit`
instead of running forever.

### The run ended `quiescent` but some nodes never ran

`quiescent` means exactly: nothing ready, nothing running, nothing paused.
It is the *clean* ending even when branches were never taken — a FALSE
branch gate, an unapproved human gate, or a loop that exhausted its budget
before satisfying a downstream gate all leave nodes at
`output_state=UNDEFINED, invocations=0`. If a node must always eventually
run, give one of its in-edges an escape hatch, e.g.
`universal_gate="isDefined(B.score) and (B.score >= 7 or A.invocations >= 3)"`
— and keep that `3` equal to the loop bound.

### The run never ends

In order of likelihood: a human node with `timeout_s=None` is waiting for a
person (check `human/*.request.json` and status.json); an LLM invocation is
still inside its `timeout_s` (default 1800 s); the graph is genuinely
perpetual (see the loop entry). To intervene from outside, write
`{"command": "stop"}` to `control.json` in the run directory.

---

## Data

### An input read as UNDEFINED (or the fn crashed on `i.x`)

Validation guarantees a *possible* supplier, not a certain one. At arming,
data crosses only in-edges whose term was TRUE **at that evaluation**, and
only names the supplier actually committed. Two usual causes: the value
rides an edge that wasn't TRUE when the node armed (it armed via a
different edge), or an LLM supplier committed without that output (see the
LLM entry). For genuinely optional inputs, read defensively:
`i.get("notes", "")` or `isDefined(i.notes)`.

### A rendered prompt contains literal `$name`

String prompts substitute only names that were **captured**; anything else
is left as literal text, silently — including typos, which validation
cannot see inside prompt strings. Check `prompts/<node>-<inv>.txt` in the
run dir for what was actually sent. Fix the name, declare the input, or —
for optional inputs — switch to a callable prompt and branch on
`isDefined(...)`. A literal dollar sign is `$$`.

### Two upstream nodes output the same name — which value arrives?

The one with the **latest commit** (highest trace seq) among suppliers on
TRUE-term in-edges at arming. This is a feature for cycles (the freshest
round wins) and a trap for diamonds — rename the outputs if both must
arrive.

### A queued node ran with stale inputs

Inputs are snapshotted at arming (`ready` event), not at start. A node that
waited on a semaphore keeps its snapshot even if upstream re-committed
meanwhile. If freshness matters, make the gate strict enough that arming
implies freshness (alternation guards do this for loops).

### An LLM node committed FALSE with `error: null`

The invocation succeeded but a declared output was missing from the reply,
so the default output expression failed. Read `outputs` on the commit event
and the reply in `sessions/`. Almost always the prompt's JSON contract:
end the prompt with a literal demand naming every declared output —

```
End your reply with exactly one JSON object, no prose after it,
no code fences: {"score": <integer 0-10>, "notes": "<one sentence>"}
```

The extractor takes the **last** JSON object in the reply (fenced blocks
first), so a model that "thinks aloud" in JSON earlier is fine. Keys that
don't match declared names warn:
`node X: extracted output 'foo' is not declared; ignored` — that warning
plus a missing declared key usually means the prompt and `outputs={...}`
disagree on spelling.

---

## Gates and expressions

### Warnings: `expression error in ... treated as False`

The expression touched `UNDEFINED` — usually a comparison like
`L2.score < 7` evaluated before L2 ever committed. Universal and output
gates evaluate before input output-states are checked, including at run
start, so this fires early and often. Guard every read of a node output:

```python
universal_gate="isDefined(L2.score) and L2.score < 7"
```

The run still behaves (errored gates are False), but each evaluation logs a
warning; a clean trace has none.

### Universal vs input vs output gates — which do I want?

All of them AND into the same single term, so on a 1-input, 1-output edge
they are interchangeable; placement is about scope on N:M edges:

- `universal_gate` — one condition (and one shared random draw) for the
  entire edge.
- `output_gates={"X": ...}` — per-destination: X's term can differ from Y's
  on the same edge. This is where per-branch probabilities go.
- `input_gates={"A": ...}` — a condition *about* input A, still ANDed into
  the whole term. A FALSE input gate blocks the term entirely; it does
  **not** deliver the other inputs without A.

There is no per-edge OR — a node needing "any of" semantics puts separate
edges on separate terms and writes `gate="e_a or e_b"`.

### Can a gate read the clock / a file / a global counter?

It can (gates are ordinary Python over the module namespace), but it will
only be *evaluated* when a mentioned node commits, so the answer is
effectively no: the gate would go stale between commits. Encode the
condition in node outputs instead — have a script node commit the fact.

### `A.state` compares equal to "running" while A is paused

By design: a pause-interrupted node is mid-invocation, and expressions see
the three-state lifecycle only (`idle`/`ready`/`running`). The finer
distinction lives in status.json.

### A module helper stopped resolving inside expressions

Node and edge handles are bound **over** module globals. Naming a helper or
constant the same as a node/edge id shadows it inside every expression.
Validation warns: `graph namespace name 'X' is shadowed by a node/edge
handle inside gating expressions`. Rename the helper (the node keeps the
name).

---

## Randomness and reproducibility

### When do stochastic gates draw?

Each gate owns one RNG stream seeded by `(run seed, gate id)`. Draw #0 at
run start; a redraw at **every commit of the edge's input nodes** — never
at evaluation. Between redraws the value is constant, so evaluating twice
gives the same answer. An edge's universal gate draws once for the whole
edge; per-node gates draw independently. Every redraw of a non-default gate
lands in the trace as a `draw` event — even for gates whose expression
never reads `sampled_value`.

### How do I reproduce a run exactly?

Same definition file + same seed (`GW_SEED` or `run(seed=...)`) reproduces
every draw and every gating decision. Wall-clock *interleaving* of
concurrently running nodes can still differ; when a byte-identical trace
matters (tests asserting commit order), serialize with
`Semaphores(script=1, ...)`.

---

## Node specifics

### Validation error: `primary node ... declares data inputs`

A primary node (in-edges only from the Generator) receives nothing — the
Generator carries no data. Declare `inputs={}` and put the seed data in the
node's own fn (or module constants). If the node genuinely needs inputs, it
isn't primary: feed it from a rank-0 script node.

### Validation error: `declared input 'x' cannot be supplied by any upstream output on any in-edge`

The data plane pairs by name. Either the upstream node doesn't declare `x`
in `outputs={...}`, or no edge connects that upstream to this node. Fix the
name or add the edge — this is the error the "choose IO names first"
discipline prevents.

### A human's "no" came through truthy

Human outputs declared as non-`str` are `json.loads`-coerced only when
that parse succeeds; the string `"no"` stays a truthy string. Declare
booleans as `bool` and phrase the prompt to demand `true`/`false`;
`GW_HUMAN_AUTO` files should carry real JSON booleans. Gate on the value
itself (`"e_go and H1.approved"`), never on `bool(...)` of free text.

### A script node needs the run directory

There is no runtime API for it. Launchers (Monitor, test harnesses, the
template's own run block) export `GW_RUN_DIR`; read it with
`os.environ.get("GW_RUN_DIR")` and fall back to a module-level path of your
own. Unlaunched bare runs create `runs/<run-id>/` beside the definition.

### `provider 'claude': executable 'claude' not found on PATH` (or auth errors)

LLM nodes shell out to the installed CLIs — `claude`, `codex`, `grok` —
which must be present and logged in. API-key env vars are deliberately
stripped from node environments, so a key in the environment cannot
substitute for CLI auth.

### Validation error: `declared output ... collides with a runtime metric name`

`id`, `rank`, `state`, `output_state`, `invocations`, `commits`,
`context_size`, `duration_s`, `usage`, `session_id`, `last_commit_seq` are
metrics every handle exposes; pick another output name.

---

## Operations

### Pause didn't stop a running script node

Pause never kills work: scripts and human invocations run to completion;
each running **LLM** session is told to halt (steered into the live session
for claude/codex, interrupt-then-notify for grok) and holds its invocation
open until Resume completes it. The run reaches the `paused` state once
nothing is left running.

### `python -m graph_workflow.gui validate` ran my file's side effects

Loading executes the definition (with `run()` intercepted) — that is what
makes any executable definition capturable. Keep module level free of
side effects beyond declarations; put them in node fns or under the
`__main__` guard.

### `output_state=UNDEFINED` in the final result

The node never committed — it never armed, or the run ended first.
Distinguish via `invocations` (0 = never started) and the trace.
