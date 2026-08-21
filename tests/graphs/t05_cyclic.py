"""T5: a cycle is a back-edge. A→B forward, B→A back-edge; the loop bound is
an ordinary gate condition (A.invocations < 3), and a counter value rides
around the cycle: A produces n+1 from B's n, B produces n+1 from A's n.

Edge terms are level-triggered — a pure function of input output states — so
once B has committed, the e_BA term stays TRUE, and A's gate (which
references A.invocations) is re-evaluated at A's own commits too. The
alternation A, B, A, B is therefore asserted explicitly in the gate:
B.invocations == A.invocations means "B has answered my latest output".

Expected: A runs 3 times (n = 1, 3, 5), B runs 3 times (n = 2, 4, 6), then
the run quiesces — A's gating expression goes FALSE once A.invocations
reaches 3.
"""

import os

from graph_workflow import Edge, Graph, ScriptNode, Semaphores, run

g = Graph("t05-cyclic", semaphores=Semaphores(script=1, llm=1, human=1))

A = ScriptNode(
    "A", rank=0,
    fn=lambda i: {"n": i.get("n", 0) + 1},
    inputs={"n": int}, outputs={"n": int},
    # Initial arming comes from the Generator exactly once; re-arming comes
    # from the back-edge — only when B has caught up (see module docstring) —
    # while under the invocation bound.
    gate=(
        "(gen_A and A.invocations == 0)"
        " or (e_BA and B.invocations == A.invocations and A.invocations < 3)"
    ),
)
B = ScriptNode(
    "B", rank=1,
    fn=lambda i: {"n": i.n + 1},
    inputs={"n": int}, outputs={"n": int},
)
g.add_nodes(A, B)
g.add_edges(
    Edge("gen_A", inputs=[g.generator], outputs=[A]),
    Edge("e_AB", inputs=[A], outputs=[B]),
    Edge("e_BA", inputs=[B], outputs=[A]),  # the back-edge
)

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ.get("GW_SEED", "7")),
        run_dir=os.environ.get("GW_RUN_DIR"),
    )
    print("REASON:", result.reason)
    for nid in "AB":
        st = result.states[nid]
        print(f"{nid}: state={st.output_state} outputs={st.outputs} inv={st.invocations}")
