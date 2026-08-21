"""T8: commit and output state. A custom output expression can commit FALSE
even though the invocation succeeded (A: x=3 fails `x > 5`), and a raised
exception ends the invocation with undefined outputs — the default output
expression then commits FALSE. Downstream nodes gated on those edges never
fire; the run still quiesces cleanly."""

import os

from graph_workflow import Edge, Graph, ScriptNode, Semaphores, run

g = Graph("t08-output-expr", semaphores=Semaphores(script=1, llm=1, human=1))

A = ScriptNode(
    "A", rank=0,
    fn=lambda i: {"x": 3},
    outputs={"x": int},
    output_expression="isDefined(x) and x > 5",
)
B = ScriptNode("B", rank=1, fn=lambda i: {"never": 1}, inputs={"x": int},
               outputs={"never": int})


def boom(inputs):
    raise RuntimeError("deliberate failure")


E = ScriptNode("E", rank=2, fn=boom, outputs={"y": int})
F = ScriptNode("F", rank=3, fn=lambda i: {"never": 2}, inputs={"y": int},
               outputs={"never": int})
g.add_nodes(A, B, E, F)
g.add_edges(
    Edge("gen_A", inputs=[g.generator], outputs=[A]),
    Edge("e_AB", inputs=[A], outputs=[B]),
    Edge("gen_E", inputs=[g.generator], outputs=[E]),
    Edge("e_EF", inputs=[E], outputs=[F]),
)

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ.get("GW_SEED", "7")),
        run_dir=os.environ.get("GW_RUN_DIR"),
    )
    print("REASON:", result.reason)
    for nid in "ABEF":
        st = result.states[nid]
        print(f"{nid}: state={st.output_state} outputs={st.outputs} inv={st.invocations}")
