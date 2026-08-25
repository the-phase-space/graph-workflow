r"""guide-template — the annotated reference graph definition.

Copy this file to start a new graph. Every construct the runtime offers
appears here exactly once, with the reasoning attached at the point where it
matters. Prune by deleting whole labeled blocks; each block's comment says
what else must go with it.

The graph is a bounded draft/critique loop with a conditional publish branch:

    GEN ── gen_S1 ──> S1 (seed: topic + audience)
    S1  ── e_topic ─> L1 (claude, drafts)          <── e_back ── L2
    S1,L1 ─ e_review ─────────> L2 (codex, scores 0-10)
                                 ├─ e_audit (p=0.35) ──> S2 (audit, stochastic)
    L1  ── e_final ─> S3 (assemble) <── e_pub ──────┘   (score >= 7, or 3 drafts spent)
    S3  ── e_ship ──> H1 (human approval) ── e_go ──> S4 (ship)

L1 redrafts while L2 scores below 7, at most 3 drafts. S2 fires on a
seed-determined subset of L2's commits. S3 publishes on a good score or when
the drafts are exhausted. S4 runs only if the human approves.

Check it (executes the file with run() intercepted; starts no run):

    python -m graph_workflow.gui validate guide/template.py

Run it (needs the `claude` and `codex` CLIs logged in; H1 opens the
human-input shell unless GW_HUMAN_AUTO points at a canned-response file):

    $env:GW_SEED = "7"; $env:GW_RUN_DIR = "$env:LOCALAPPDATA\Temp\gw-template-run"
    $env:GW_MONITOR = "0"   # headless; unset to let the Monitor GUI attach
    python guide/template.py

Expected: `REASON: quiescent`, with S1/L1/L2/S3 committed TRUE and H1/S4
reflecting the approval. S2 fires only when its gate's seeded draw falls
under 0.35: at seed 7 it stays idle (the two draws are 0.559 and 0.943 —
find them as `draw` events in the trace); rerun with GW_SEED=3 and it fires
on every critique. Read runs from trace.jsonl in the run directory — one
JSON event per line.
"""

import os

from graph_workflow import (
    Edge,
    Gate,
    Graph,
    HumanNode,
    LLMNode,
    ScriptNode,
    Semaphores,
    isDefined,
    run,
)

# --------------------------------------------------------------------------- #
# Helpers.
#
# Keep script fns, prompt callables, and expression helpers as NAMED,
# MODULE-LEVEL defs. Gating expressions evaluate in this module's namespace,
# so anything defined here is callable from a gate string; the Monitor GUI
# can also round-trip named module-level functions, where inline lambdas
# buried in kwargs become opaque.
# --------------------------------------------------------------------------- #


def draft_prompt(i):
    """A prompt may be a callable ``prompt(inputs) -> str`` instead of a
    string. Use the callable form whenever an input is OPTIONAL: on the first
    round `notes` has never been supplied, and a ``$notes`` placeholder in a
    string prompt would render as the literal text "$notes". Declared but
    unsupplied inputs read as UNDEFINED (falsy); test with isDefined()."""
    lines = [
        f"Write a 4-6 sentence product pitch for {i.topic}, aimed at {i.audience}.",
    ]
    if isDefined(i.notes):
        lines.append(f"This is a revision. Address this reviewer feedback: {i.notes}")
    lines.append(
        "End your reply with exactly one JSON object, no prose after it, "
        'no code fences: {"draft": "<the pitch>"}'
    )
    return "\n".join(lines)


def assemble(i):
    """Script fns receive one argument: the node's captured inputs, readable
    as attributes (i.draft) or items (i["draft"]); i.get(name, default) for
    optional ones. Return a dict over the declared output names."""
    final_md = (
        f"{i.draft}\n\n---\ncritic score {i.score}/10 - {i.notes}"
    )
    return {"final_md": final_md}


def ship(i):
    """Side effects live in script fns — write files, call APIs, anything.
    This one just reports. A fn with exactly one declared output may return
    the bare value instead of a dict."""
    return {"shipped": True, "receipt": f"published with remarks: {i.get('remarks', '')}"}


# --------------------------------------------------------------------------- #
# Graph and semaphores.
#
# Semaphores cap CONCURRENCY per node type; they queue ready nodes, never
# block them forever. `providers` subdivides the llm pool: an LLM node starts
# only when its type semaphore AND its provider's both have a free slot.
# Defaults: script=4, llm=2, human=1, providers={}.
# --------------------------------------------------------------------------- #

g = Graph(
    "guide-template",
    semaphores=Semaphores(script=4, llm=2, human=1,
                          providers={"claude": 1, "codex": 1}),
    # Optional graph-wide overrides for what a Paused LLM session is told;
    # per-node pause_message/resume_message override these in turn.
    # pause_message="Halt for now. Do not call any new tools until I say 'continue'",
    # resume_message="continue",
)

# --------------------------------------------------------------------------- #
# Nodes.
#
# Ids: valid Python identifiers, unique, never "GEN", and never equal to an
# edge id — node and edge ids share one namespace inside gating expressions.
# Bind each node object to a module variable of the SAME name: expressions
# like "L2.score" resolve that name to the node's runtime handle.
#
# Ranks: unique ints. Rank is a SCHEDULING PRIORITY, not topology — the
# lowest-rank ready node starts first whenever a slot frees. Number them in
# flow order.
#
# Output names are the data plane: a downstream node's declared INPUT is
# filled by an upstream node's declared OUTPUT with the SAME NAME, across an
# in-edge. Choose names once, globally, before writing any edge. Never name
# an output: id, rank, state, output_state, invocations, commits,
# context_size, duration_s, usage, session_id, last_commit_seq — those are
# runtime metrics every handle already exposes.
# --------------------------------------------------------------------------- #

# -- S1: primary script node ------------------------------------------------ #
# A "primary" node is one whose in-edges come only from the Generator. It
# declares NO inputs (validation enforces this); its fn receives an empty
# inputs view. Every graph needs at least one — the Generator's flip to TRUE
# at run start is what arms them.
S1 = ScriptNode(
    "S1", rank=0,
    fn=lambda i: {"topic": "a pocket spectrometer", "audience": "field geologists"},
    outputs={"topic": str, "audience": str},
)

# -- L1: LLM node with a bounded self-revision loop ------------------------- #
# One LLM node = one provider = one fresh CLI session per invocation.
# Outputs: `response` (if declared) receives the raw reply text; every OTHER
# declared output is pulled from the last JSON object in the reply — so the
# prompt must demand that JSON object by name. `cwd` is the agent's working
# directory (its tools run there); default is the launching process's cwd.
#
# The gate is this node's whole firing policy, in one place:
#   term 1: arm once from the seed edge (first draft).
#   term 2: re-arm from the back-edge only while ALL of
#     - L2 has answered my latest draft  (L2.invocations == L1.invocations —
#       required because edge terms are level-triggered: once L2 has committed
#       TRUE, e_back's term STAYS TRUE, and this gate re-evaluates at L1's own
#       commits too. Without the equality the node would re-fire immediately.)
#     - the draft budget is not spent    (< 3 here must match e_pub's >= 3)
#     - the critic wants changes         (score < 7; isDefined guards the
#       first evaluations, which happen before L2 has ever committed —
#       comparing UNDEFINED would error, and an errored gate counts as False.)
L1 = LLMNode(
    "L1", rank=1,
    provider="claude", model="haiku", effort="low",
    prompt=draft_prompt,
    inputs={"topic": str, "audience": str, "notes": str},
    outputs={"response": str, "draft": str},
    cwd=os.environ.get("GW_RUN_DIR"),
    timeout_s=300,
    gate=(
        "(e_topic and L1.invocations == 0)"
        " or (e_back and L2.invocations == L1.invocations"
        " and L1.invocations < 3"
        " and isDefined(L2.score) and L2.score < 7)"
    ),
)

# -- L2: second provider, string prompt ------------------------------------- #
# A string prompt is a string.Template: $name substitutes the captured input
# of that name (non-strings arrive JSON-encoded; $$ is a literal $). L2 uses
# the default gate — the AND of its in-edge terms — so it re-arms every time
# L1 commits a new draft.
L2 = LLMNode(
    "L2", rank=2,
    provider="codex", model="gpt-5.6-luna", effort="low",
    prompt=(
        "You are a strict pitch reviewer. The product: $topic. The draft:\n\n"
        "$draft\n\nScore it 0-10 for specificity and punch (7+ means "
        "publishable), and give one actionable sentence of feedback. "
        "End your reply with exactly one JSON object, no prose after it, "
        'no code fences: {"score": <integer 0-10>, "notes": "<one sentence>"}'
    ),
    inputs={"topic": str, "draft": str},
    outputs={"response": str, "score": int, "notes": str},
    cwd=os.environ.get("GW_RUN_DIR"),
    timeout_s=300,
)

# -- S2: stochastic audit (self-contained block: delete with e_audit) ------- #
# Demonstrates two things:
#   * A custom output_expression. Its evaluation at invocation end IS the
#     commit, and its boolean IS the node's output state — the value every
#     downstream edge term reads. Here FALSE means "audit raised a flag":
#     the node still committed; a downstream edge simply would not pass.
#     (Default output_expression: isDefined(...) of every declared output.)
#   * A bare return: one declared output, so the fn may return the value.
S2 = ScriptNode(
    "S2", rank=3,
    fn=lambda i: i.score < 5,
    inputs={"score": int},
    outputs={"flag": bool},
    output_expression="isDefined(flag) and not flag",
)

# -- S3: assemble ----------------------------------------------------------- #
# Default gate: AND of BOTH in-edge terms (e_final and e_pub). e_pub's
# universal gate is the publish decision; see the edge.
S3 = ScriptNode(
    "S3", rank=4,
    fn=assemble,
    inputs={"draft": str, "score": int, "notes": str},
    outputs={"final_md": str},
)

# -- H1: human gate (delete with e_ship, e_go, and S4) ---------------------- #
# The runtime opens the human-input shell with the rendered prompt and blocks
# until the human submits the declared outputs. timeout_s=None waits forever.
# For unattended runs: GW_HUMAN_AUTO=<file> maps node id -> canned outputs,
# e.g. {"H1": {"approved": true, "remarks": "ok"}}.
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

# -- S4: ship, only on approval --------------------------------------------- #
# The gate reads H1's declared output through its handle. H1 commits TRUE
# whether the answer was yes or no (both outputs defined); the DECISION lives
# here. If not approved, S4 never arms and the run quiesces without it.
S4 = ScriptNode(
    "S4", rank=6,
    fn=ship,
    inputs={"remarks": str},
    outputs={"shipped": bool, "receipt": str},
    gate="e_go and H1.approved",
)

g.add_nodes(S1, L1, L2, S2, S3, H1, S4)

# --------------------------------------------------------------------------- #
# Edges.
#
# An edge is first-class: N input nodes, M output nodes, and gates. It
# contributes exactly ONE boolean term to each output node's gating
# expression:
#
#     term = universal_gate AND output_gate(o)
#            AND, over every input i: input_gate(i) AND output_state(i)
#
# All gates default to True, so a default edge is pure feedforward, and an
# N:M edge behaves exactly like N*M separate 1:1 edges. An input that has
# never committed makes the term False. Terms are LEVEL-TRIGGERED: pure
# functions of current state, re-evaluated only when a node referenced by
# them (or by the target's gate string) commits.
#
# Gates are ordinary Python strings evaluated in this module's namespace plus
# node handles; edge gates additionally see `sampled_value` (the gate's own
# seeded draw in [0,1)) and `p_invocation` (if declared). Guard any read of a
# node output with isDefined() — universal and output gates evaluate BEFORE
# the input output-states are checked, including at run start.
# --------------------------------------------------------------------------- #

g.add_edges(
    # The Generator (g.generator, id "GEN") is not a node: no logic, no
    # outputs, nothing upstream of it. Its output state flips TRUE at run
    # start; that flip arms the primaries.
    Edge("gen_S1", inputs=[g.generator], outputs=[S1]),

    Edge("e_topic", inputs=[S1], outputs=[L1]),

    # Convergent 2-input edge: TRUE only once BOTH S1 and L1 have committed
    # TRUE. Identical in behavior to two separate 1:1 edges; one edge reads
    # better when the target genuinely needs both.
    Edge("e_review", inputs=[S1, L1], outputs=[L2]),

    # The back-edge that makes the cycle. Deliberately default-gated: the
    # revise/stop policy lives in ONE place, L1's gate, not spread over both.
    Edge("e_back", inputs=[L2], outputs=[L1]),

    # Stochastic gate on the S2 leg only. Each gate owns an RNG stream seeded
    # by (run seed, gate id): draw #0 happens at run start, and the gate
    # redraws at every commit of the edge's INPUT nodes — so S2 gets a fresh
    # coin flip per critique. Same seed => same draws => same run, exactly.
    # (A universal gate draws ONCE for the whole edge; per-node output gates
    # like this one draw independently.)
    Edge("e_audit", inputs=[L2], outputs=[S2],
         output_gates={"S2": Gate("sampled_value <= p_invocation",
                                  p_invocation=0.35)}),

    Edge("e_final", inputs=[L1], outputs=[S3]),

    # The publish decision. The escape hatch (`or L1.invocations >= 3`) is
    # what guarantees the run can always end: without it, three sub-7 drafts
    # would leave S3 unreachable and the loop closed — the run would quiesce
    # with nothing published. Its 3 must equal the bound in L1's gate.
    Edge("e_pub", inputs=[L2], outputs=[S3],
         universal_gate="isDefined(L2.score) and (L2.score >= 7 or L1.invocations >= 3)"),

    Edge("e_ship", inputs=[S3], outputs=[H1]),
    Edge("e_go", inputs=[H1], outputs=[S4]),
)

# --------------------------------------------------------------------------- #
# Entry point. Executing the file starts a run; the guard keeps the file
# importable (tests, tools, the Monitor's loader all import or intercept it).
# GW_SEED/GW_RUN_DIR are how launchers (the Monitor, test harnesses) steer a
# definition without editing it — always plumb them exactly like this.
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ["GW_SEED"]) if os.environ.get("GW_SEED") else None,
        run_dir=os.environ.get("GW_RUN_DIR") or None,
        max_commits=50,  # hard stop for runaway cycles; size it generously
    )
    print("REASON:", result.reason)  # "quiescent" is the clean ending
    for nid in ("S1", "L1", "L2", "S2", "S3", "H1", "S4"):
        st = result.states[nid]
        print(f"{nid}: output_state={st.output_state} invocations={st.invocations} "
              f"outputs={ {k: str(v)[:60] for k, v in st.outputs.items()} }")

# --------------------------------------------------------------------------- #
# The rules, in brief — check every new definition against these:
#
#  1. One graph per file; run() under the __main__ guard; the file itself is
#     the entry point.
#  2. Data pairs BY NAME: input names must equal upstream output names on
#     some in-edge, or validation rejects the graph. Latest commit wins when
#     two suppliers share a name. Inputs are snapshotted when the node arms,
#     not when it starts.
#  3. Every node: unique id (a Python identifier, distinct from edge ids),
#     unique int rank.
#  4. Default node gate = AND of all in-edge terms. A node with no in-edges
#     can never arm.
#  5. Everything is level-triggered, and gates re-evaluate ONLY at commits of
#     the nodes they mention. Cycles therefore need BOTH an invocation bound
#     and an alternation guard (peer.invocations == mine).
#  6. Commit = output-expression evaluation; the boolean is the output state.
#     Failures (exceptions, timeouts, missing LLM outputs) commit FALSE with
#     an error recorded — they do not crash the run.
#  7. isDefined()-guard every gate that reads a node output.
#  8. LLM structured outputs: declare them, and have the prompt end with
#     "exactly one JSON object" carrying those names.
#  9. The run is over when nothing is ready and nothing is running. Make sure
#     some path always leads there (bounds, escape hatches, max_commits).
# 10. Verify before shipping:  python -m graph_workflow.gui validate <file>
#     then a seeded headless run with GW_RUN_DIR under a temp directory.
# --------------------------------------------------------------------------- #
