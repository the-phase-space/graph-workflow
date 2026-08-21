"""T4: divergence. One 2-output edge A→{B,C}; both consume A's x. With the
script semaphore at 1, the schedule must start B (rank 1) strictly before C
(rank 2): the next node to start running is always the lowest-rank ready node
the semaphores allow."""

import os

from graph_workflow import Edge, Graph, ScriptNode, Semaphores, run

g = Graph("t04-divergent", semaphores=Semaphores(script=1, llm=1, human=1))

A = ScriptNode("A", rank=0, fn=lambda i: {"x": 6}, outputs={"x": int})
B = ScriptNode("B", rank=1, fn=lambda i: {"y": i.x * 2}, inputs={"x": int},
               outputs={"y": int})
C = ScriptNode("C", rank=2, fn=lambda i: {"z": i.x * 3}, inputs={"x": int},
               outputs={"z": int})
g.add_nodes(A, B, C)
g.add_edges(
    Edge("gen_A", inputs=[g.generator], outputs=[A]),
    Edge("e_div", inputs=[A], outputs=[B, C]),  # one divergent edge
)

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ.get("GW_SEED", "7")),
        run_dir=os.environ.get("GW_RUN_DIR"),
    )
    print("REASON:", result.reason)
    for nid in "ABC":
        st = result.states[nid]
        print(f"{nid}: state={st.output_state} outputs={st.outputs} inv={st.invocations}")
