"""T1: single primary script node. Runs once, commits TRUE, run quiesces."""

import os

from graph_workflow import Edge, Graph, ScriptNode, Semaphores, run

g = Graph("t01-single", semaphores=Semaphores(script=2, llm=1, human=1))


def answer(inputs):
    return {"x": 41 + 1}


A = ScriptNode("A", rank=0, fn=answer, outputs={"x": int})
g.add_node(A)
g.add_edge(Edge("gen_A", inputs=[g.generator], outputs=[A]))

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ.get("GW_SEED", "7")),
        run_dir=os.environ.get("GW_RUN_DIR"),
    )
    print("REASON:", result.reason)
    print("A:", result.output_state("A"), result.outputs("A"),
          "invocations:", result.states["A"].invocations)
