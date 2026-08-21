"""T2: dual node, single edge. A produces x; B consumes x, produces y = x * 2."""

import os

from graph_workflow import Edge, Graph, ScriptNode, Semaphores, run

g = Graph("t02-chain", semaphores=Semaphores(script=2, llm=1, human=1))

A = ScriptNode("A", rank=0, fn=lambda i: {"x": 21}, outputs={"x": int})
B = ScriptNode(
    "B",
    rank=1,
    fn=lambda i: {"y": i.x * 2},
    inputs={"x": int},
    outputs={"y": int},
)
g.add_nodes(A, B)
g.add_edges(
    Edge("gen_A", inputs=[g.generator], outputs=[A]),
    Edge("e_AB", inputs=[A], outputs=[B]),
)

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ.get("GW_SEED", "7")),
        run_dir=os.environ.get("GW_RUN_DIR"),
    )
    print("REASON:", result.reason)
    print("A:", result.output_state("A"), result.outputs("A"))
    print("B:", result.output_state("B"), result.outputs("B"))
