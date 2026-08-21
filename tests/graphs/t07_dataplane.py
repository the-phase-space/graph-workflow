"""T7: the data plane. Outputs are typed, anything you can write down;
outputs and inputs pair by name across edges; an unconsumed output is
harmless (A.note is consumed by B but not C); when two upstream nodes supply
the same name, the latest commit wins (B and C both output tag; D captures
the later one)."""

import os

from graph_workflow import Edge, Graph, ScriptNode, Semaphores, run

g = Graph("t07-dataplane", semaphores=Semaphores(script=1, llm=1, human=1))

A = ScriptNode(
    "A", rank=0,
    fn=lambda i: {"config": {"depth": 3, "mode": "fast"}, "note": "hello"},
    outputs={"config": dict, "note": str},
)
B = ScriptNode(
    "B", rank=1,
    fn=lambda i: {"tag": f"B:{i.config['mode']}:{i.note}"},
    inputs={"config": dict, "note": str}, outputs={"tag": str},
)
C = ScriptNode(
    "C", rank=2,
    fn=lambda i: {"tag": f"C:{i.config['depth']}"},
    inputs={"config": dict}, outputs={"tag": str},
)
D = ScriptNode(
    "D", rank=3,
    fn=lambda i: {"final": i.tag},
    inputs={"tag": str}, outputs={"final": str},
)
g.add_nodes(A, B, C, D)
g.add_edges(
    Edge("gen_A", inputs=[g.generator], outputs=[A]),
    Edge("e_A_BC", inputs=[A], outputs=[B, C]),
    Edge("e_BC_D", inputs=[B, C], outputs=[D]),
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
