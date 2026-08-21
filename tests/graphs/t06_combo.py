"""T6: the combination graph — convergence + divergence + back-edge +
stochastic gating, fully deterministic over the run seed.

  GEN → P1, P2  (primaries)
  {P1,P2} → M   (one convergent edge)
  M → {X, Y}    (one divergent edge; X's output gate is stochastic)
  Y → M         (back-edge; M loops until M.invocations == 4)

A round counter rides M → Y → M. X fires only when its gate's seeded draw
falls under p_invocation = 0.5; draws are redrawn at every commit of M, so X
fires on a seed-determined subset of the four rounds. Same seed ⇒ identical
trace; different seed ⇒ (almost surely) different draws.

Edge terms are level-triggered (pure over output states): once Y has
committed, e_back's term stays TRUE, and M's gate — referencing
M.invocations — is re-evaluated at M's own commits too. The M→Y→M
alternation is therefore asserted explicitly: Y.invocations == M.invocations
means "Y has echoed my latest round".
"""

import os

from graph_workflow import Edge, Gate, Graph, ScriptNode, Semaphores, run

g = Graph("t06-combo", semaphores=Semaphores(script=1, llm=1, human=1))

P1 = ScriptNode("P1", rank=0, fn=lambda i: {"p1": 1}, outputs={"p1": int})
P2 = ScriptNode("P2", rank=1, fn=lambda i: {"p2": 2}, outputs={"p2": int})
M = ScriptNode(
    "M", rank=2,
    fn=lambda i: {"round": i.get("round", 0) + 1},
    inputs={"round": int}, outputs={"round": int},
    gate=(
        "(e_conv and M.invocations == 0)"
        " or (e_back and Y.invocations == M.invocations and M.invocations < 4)"
    ),
)
X = ScriptNode(
    "X", rank=3,
    fn=lambda i: {"seen": i.round},
    inputs={"round": int}, outputs={"seen": int},
)
Y = ScriptNode(
    "Y", rank=4,
    fn=lambda i: {"round": i.round},
    inputs={"round": int}, outputs={"round": int},
)
g.add_nodes(P1, P2, M, X, Y)
g.add_edges(
    Edge("gen_P1", inputs=[g.generator], outputs=[P1]),
    Edge("gen_P2", inputs=[g.generator], outputs=[P2]),
    Edge("e_conv", inputs=[P1, P2], outputs=[M]),
    Edge(
        "e_div", inputs=[M], outputs=[X, Y],
        # Stochastic gating is ordinary conditionality: a seeded draw against
        # a probability, conjoined with whatever else the gate asserts.
        output_gates={"X": Gate("sampled_value <= p_invocation", p_invocation=0.5)},
    ),
    Edge("e_back", inputs=[Y], outputs=[M]),
)

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ.get("GW_SEED", "11")),
        run_dir=os.environ.get("GW_RUN_DIR"),
    )
    print("REASON:", result.reason)
    for nid in ("P1", "P2", "M", "X", "Y"):
        st = result.states[nid]
        print(f"{nid}: state={st.output_state} outputs={st.outputs} inv={st.invocations}")
