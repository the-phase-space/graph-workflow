# graph-workflow how-to

Recipes for composing and operating graph definitions. `template.py` in this
folder carries the fully annotated version of most constructs; `reference.md`
has the exact semantics; `faq.md` decodes symptoms.

---

## Compose a new definition, start to finish

Work in this order — it front-loads the decisions that everything else
depends on. Worked example request: *"ask claude and codex the same
question and keep the answer only if they agree."*

**1. List the nodes: id, type, one-line job, rank in flow order.**

| id | type | job | rank |
|---|---|---|---|
| Q | script | supply the question | 0 |
| L1 | llm (claude) | answer it | 1 |
| L2 | llm (codex) | answer it | 2 |
| V | script | compare; succeed only on agreement | 3 |

**2. Choose the output names — before any edge.** Names are the data plane:
a declared input is filled by an upstream declared output of the same name.
Q emits `question`. L1 and L2 must emit **different** names (`a1`, `a2`) —
if both emitted `answer`, V would receive only the later commit.

**3. Draw the edges as arrows.** `GEN -> Q`, `Q -> {L1, L2}` (one divergent
edge), `{L1, L2} -> V` (one convergent edge — TRUE only when both have
committed TRUE).

**4. Decide the gates.** Defaults cover the flow here. The only policy is
"succeed only on agreement," which is V's output expression — its commit
boolean is what anything downstream would gate on.

**5. Write the file.**

```python
import os

from graph_workflow import Edge, Graph, LLMNode, ScriptNode, Semaphores, run

g = Graph(
    "cross-check",
    semaphores=Semaphores(script=2, llm=2, human=1,
                          providers={"claude": 1, "codex": 1}),
)

Q = ScriptNode(
    "Q", rank=0,
    fn=lambda i: {"question": "Name the SI unit of luminous intensity."},
    outputs={"question": str},
)

L1 = LLMNode(
    "L1", rank=1, provider="claude", model="haiku", effort="low",
    prompt=(
        "Q: $question\nAnswer in one word. End your reply with exactly one "
        'JSON object, no prose after it, no code fences: {"a1": "<the word>"}'
    ),
    inputs={"question": str}, outputs={"response": str, "a1": str},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=300,
)

L2 = LLMNode(
    "L2", rank=2, provider="codex", model="gpt-5.6-luna", effort="low",
    prompt=(
        "Q: $question\nAnswer in one word. End your reply with exactly one "
        'JSON object, no prose after it, no code fences: {"a2": "<the word>"}'
    ),
    inputs={"question": str}, outputs={"response": str, "a2": str},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=300,
)


def compare(i):
    return {"agreed": str(i.a1).strip().lower() == str(i.a2).strip().lower(),
            "answers": [i.a1, i.a2]}


V = ScriptNode(
    "V", rank=3, fn=compare,
    inputs={"a1": str, "a2": str},
    outputs={"agreed": bool, "answers": list},
    output_expression="isDefined(agreed) and agreed",
)

g.add_nodes(Q, L1, L2, V)
g.add_edges(
    Edge("gen_Q", inputs=[g.generator], outputs=[Q]),
    Edge("e_fan", inputs=[Q], outputs=[L1, L2]),
    Edge("e_join", inputs=[L1, L2], outputs=[V]),
)

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ["GW_SEED"]) if os.environ.get("GW_SEED") else None,
        run_dir=os.environ.get("GW_RUN_DIR") or None,
    )
    print("REASON:", result.reason)
    print("V:", result.output_state("V"), result.outputs("V"))
```

**6. Verify** — next recipe. Never hand over an unvalidated definition.

## Validate, run, and read the results

```powershell
# Structural check: executes the file with run() intercepted, starts no run.
python -m graph_workflow.gui validate mygraph.py
# clean: {"ok": true, "errors": [], "warnings": []}
```

Errors block the run; warnings usually mean a misspelled name in an
expression — fix them too. Validation cannot see gate *logic* or prompt
`$name` typos, so follow with a real seeded run:

```powershell
$env:GW_SEED = "7"                                    # reproducible
$env:GW_RUN_DIR = "$env:LOCALAPPDATA\Temp\gw-check"   # scratch runs: keep
$env:GW_MONITOR = "0"                                 #   out of synced folders
python mygraph.py
```

(bash: `GW_SEED=7 GW_RUN_DIR=/tmp/gw-check GW_MONITOR=0 python mygraph.py`.)

Then interrogate the run directory:

```python
import json
evs = [json.loads(l) for l in open("trace.jsonl", encoding="utf-8")]
commits = [e for e in evs if e["event"] == "commit"]
for c in commits:
    print(c["node"], c["invocation"], c["output_state"], c.get("error"))
reevals = [e for e in evs if e["event"] == "reeval" and e["node"] == "V"]
```

Confirm: final event `run_end` with reason `quiescent`; every node that
should have run has commits with `output_state: true` and `error: null`;
LLM prompts rendered fully (`prompts/*.txt` — no literal `$name`); no
`warning` events.

## Build a bounded refinement loop

The full pattern lives in `template.py` (L1/L2 and edge `e_back`). Skeleton
— a drafter D and critic C, at most N rounds, stop early when satisfied:

```python
D = LLMNode(
    "D", ...,
    gate=(
        "(e_seed and D.invocations == 0)"
        " or (e_back and C.invocations == D.invocations"
        " and D.invocations < 3"
        " and isDefined(C.score) and C.score < 7)"
    ),
)
# C uses the default gate (re-arms on every D commit);
# e_back is a default-gated Edge from C to D — the policy lives in D's gate.
```

Every clause is required: the first term arms round one; the equality is
the alternation guard (without it D re-arms off its own commit, because
edge terms are level-triggered); the bound ends the loop; the `isDefined`
guard keeps early evaluations from erroring. Add `max_commits=` in `run()`
as the backstop, and give the loop's consumers an escape hatch (next
recipe) so exhaustion still produces output.

## Branch on a value

Route by putting the condition on the edges, not in the nodes. Both gates
read C's committed output through its handle, `isDefined`-guarded:

```python
g.add_edges(
    Edge("e_pub", inputs=[C], outputs=[PUBLISH],
         universal_gate="isDefined(C.score) and (C.score >= 7 or D.invocations >= 3)"),
    Edge("e_re", inputs=[C], outputs=[REWORK],
         universal_gate="isDefined(C.score) and C.score < 7 and D.invocations < 3"),
)
```

Make the conditions partition cleanly (note the shared constants — the `3`s
and `7`s must agree with the loop gate), and decide explicitly which
branches fire on the *final* state versus every state: these are level
conditions, re-checked at each commit of the mentioned nodes.

## Fire a node with probability p

```python
Edge("e_maybe", inputs=[A], outputs=[X],
     output_gates={"X": Gate("sampled_value <= p_invocation", p_invocation=0.35)})
```

The gate redraws at every commit of A, so X gets one seeded coin flip per
A-commit; same seed, same flips, always. Put the probability on a per-node
`output_gates` entry (independent flip per destination); a `universal_gate`
flip is one shared draw for the whole edge. Combine freely with other
conditions: `Gate("isDefined(A.ok) and A.ok and sampled_value <= p_invocation",
p_invocation=0.5)`.

## Get structured data out of an LLM node

Declare the fields as outputs, and end the prompt by demanding them as one
JSON object — this exact incantation is proven across this repo's tests:

```python
outputs={"response": str, "score": int, "notes": str},
prompt=(
    "...task...\n"
    "End your reply with exactly one JSON object, no prose after it, "
    'no code fences: {"score": <integer 0-10>, "notes": "<one sentence>"}'
),
```

The runtime takes the last JSON object in the reply; `response` (if
declared) always carries the full text. Downstream, trust but verify: a
cheap script node that checks the fields and commits FALSE otherwise (see
V in the worked example) keeps bad extractions from propagating. For
non-JSON reply formats, supply `parse=my_parser` —
`my_parser(text, raw) -> dict` over declared names — instead of fighting
the prompt.

## Handle optional inputs

A `$name` whose input was never captured stays literal text, so string
prompts are only for always-present inputs. Otherwise use a callable and
branch:

```python
def prompt_fn(i):
    parts = [f"Summarize: {i.text}"]
    if isDefined(i.feedback):
        parts.append(f"Incorporate this feedback: {i.feedback}")
    return "\n".join(parts)
```

Script fns: `i.get("feedback", "")`.

## Put a human in the loop

```python
H = HumanNode(
    "H", rank=5,
    prompt="About to publish:\n\n$final_md\n\nSet `approved` true or false.",
    inputs={"final_md": str}, outputs={"approved": bool, "remarks": str},
    timeout_s=600,          # None = wait forever
)
SHIP = ScriptNode("SHIP", ..., gate="e_go and H.approved")
```

H commits TRUE for yes *and* no (both outputs defined); the decision
belongs in the downstream gate. For unattended runs write a canned-response
file and set `GW_HUMAN_AUTO`:

```powershell
Set-Content gw-human.json '{"H": {"approved": true, "remarks": "ok"}}'
$env:GW_HUMAN_AUTO = (Resolve-Path gw-human.json)
```

(`"*"` as the key answers every human node; `GW_HUMAN_SHELL_AUTO` instead
exercises the real shell headlessly.)

## Pause, resume, or stop a live run

From any process, write to `control.json` in the run directory:

```powershell
Set-Content "$runDir\control.json" '{"command": "pause"}'   # or resume / stop
```

Pause lets scripts and humans finish, halts each running LLM session
politely (the session survives; its invocation stays open), and settles
into `"state": "paused"` in status.json. Resume completes those same
invocations with the resume message. Stop kills everything and ends the run
(`reason: stopped`). Customize the messages via `pause_message` /
`resume_message` on the Graph or per LLM node. The Monitor GUI's buttons
write the same file.

## Recover a crashed or abandoned run

```python
from mygraph import g            # the unchanged definition module
from graph_workflow import resume_run

result = resume_run(g, r"...\runs\<run-id>\trace.jsonl")
```

Replay restores all committed state, re-runs invocations that were in
flight at the crash, and continues. Constraints: the definition and seed
must be unchanged (verified against the recorded draws —
`TraceReplayError` otherwise), and restored outputs are their JSON trace
forms, so non-JSON values come back as repr strings.

## Test a definition

Run the file as a subprocess with a pinned seed and scratch run dir, then
assert on the trace — the pattern `tests/conftest.py` implements:

```python
import json, os, subprocess, sys

def test_mygraph(tmp_path):
    env = dict(os.environ, GW_SEED="7", GW_RUN_DIR=str(tmp_path),
               GW_MONITOR="0")
    proc = subprocess.run([sys.executable, "mygraph.py"], env=env,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    evs = [json.loads(l) for l in
           (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    commits = [e for e in evs if e["event"] == "commit" and e["node"] == "V"]
    assert commits and commits[-1]["output_state"] is True
    assert evs[-1]["event"] == "run_end" and evs[-1]["reason"] == "quiescent"
```

Set `GW_HUMAN_AUTO` for human nodes. Assertions about *ordering* between
concurrent branches need `Semaphores(script=1, ...)`; keep run dirs in temp
locations, never in cloud-synced folders.

## Process a collection of items

Topology is static — declared once, before the run. Three honest options:

1. **Loop inside one script node** (simplest; the collection never crosses
   an edge): fetch, iterate, return aggregates.
2. **Unroll a known N** into N parallel nodes fed by one divergent edge,
   converging on a collector — each worker's declared inputs select its
   slice by name (`w1`/`w2`/`w3`; see `tests/graphs/t21_synth_tri.py`).
3. **Let the data ride a bounded cycle**: a counter or shrinking payload
   travels around the loop, one item per revolution
   (`tests/graphs/t05_cyclic.py`).

There is no dynamic node spawning; if the work list is unbounded, a script
node owns it.

## Edit a Monitor-authored (structured) definition

A GUI-saved file has marked sections. Respect the boundary:

- `# --- gw:helpers ---` … `# --- gw:end-helpers ---`: hand-edited Python,
  preserved verbatim across saves. Your functions go here.
- `# --- gw:graph ---` … `# --- gw:end-graph ---`: regenerated wholesale on
  every GUI save — hand edits here survive until the next save, then
  vanish. Change topology in the GUI, or convert the file to hand-written
  style and stop GUI-saving it.
- `# --- gw:gui ---`: comment-only display metadata; ignore it.

After any hand edit: `python -m graph_workflow.gui validate <file>`. The
GUI can *load* any executable definition, structured or not; it only
refuses to *save over* unstructured files without force.

## Size the semaphores

- `script`: cheap threads — the default 4 is fine unless fns are heavy.
- `llm`: concurrent CLI sessions = money and rate limits; 2 is a sane
  default, raise deliberately.
- `providers={...}`: cap each provider at what its account tolerates; an
  LLM node needs a slot in *both* pools to start.
- `human`: 1, almost always — one shell window at a time.

Semaphores only queue ready nodes; the rank order decides who gets a freed
slot (lowest first). Serialize with `script=1` when tests assert ordering.
