"""T3: convergence, and the N-to-M ≡ N×M identity.

Layer 1: primaries A, B. Layer 2: C fed by ONE convergent 2-input edge
{A,B}→C; D fed by TWO separate 1:1 edges A→D and B→D under the default node
gating expression (AND of in-edge terms). Per the spec the two constructions
behave identically: both C and D become ready only once both A and B have
committed TRUE, and both receive a and b.
"""

import os

from graph_workflow import Edge, Graph, ScriptNode, Semaphores, run

g = Graph("t03-convergent", semaphores=Semaphores(script=1, llm=1, human=1))

A = ScriptNode("A", rank=0, fn=lambda i: {"a": 10}, outputs={"a": int})
B = ScriptNode("B", rank=1, fn=lambda i: {"b": 7}, outputs={"b": int})
C = ScriptNode(
    "C", rank=2,
    fn=lambda i: {"sum_c": i.a + i.b},
    inputs={"a": int, "b": int}, outputs={"sum_c": int},
)
D = ScriptNode(
    "D", rank=3,
    fn=lambda i: {"sum_d": i.a + i.b},
    inputs={"a": int, "b": int}, outputs={"sum_d": int},
)
g.add_nodes(A, B, C, D)
g.add_edges(
    Edge("gen_A", inputs=[g.generator], outputs=[A]),
    Edge("gen_B", inputs=[g.generator], outputs=[B]),
    Edge("e_conv", inputs=[A, B], outputs=[C]),   # one convergent edge
    Edge("e_AD", inputs=[A], outputs=[D]),        # ... vs two 1:1 edges
    Edge("e_BD", inputs=[B], outputs=[D]),
)

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ.get("GW_SEED", "7")),
        run_dir=os.environ.get("GW_RUN_DIR"),
    )
    print("REASON:", result.reason)
    for nid in "ABCD":
        st = result.states[nid]
        print(f"{nid}: state={st.output_state} outputs={st.outputs} inv={st.invocations}")
