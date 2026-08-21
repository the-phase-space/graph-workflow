"""T13: a human node between two script nodes. The runtime launches the
human-input shell; automated runs answer via GW_HUMAN_AUTO (runtime bypass)
or GW_HUMAN_SHELL_AUTO (the real shell exe, headless auto-response)."""

import os

from graph_workflow import Edge, Graph, HumanNode, ScriptNode, Semaphores, run

g = Graph("t13-human", semaphores=Semaphores(script=2, llm=1, human=1))

S = ScriptNode(
    "S", rank=0,
    fn=lambda i: {"proposal": "deploy graph-workflow 1.0.0"},
    outputs={"proposal": str},
)
H = HumanNode(
    "H", rank=1,
    prompt="Proposal: $proposal\n\nApprove? Answer yes or no in `approval`.",
    inputs={"proposal": str},
    outputs={"approval": str},
    timeout_s=120,
)
V = ScriptNode(
    "V", rank=2,
    fn=lambda i: {"ok": str(i.approval).strip().lower().startswith("y")},
    inputs={"approval": str},
    outputs={"ok": bool},
    output_expression="isDefined(ok) and ok",
)
g.add_nodes(S, H, V)
g.add_edges(
    Edge("gen_S", inputs=[g.generator], outputs=[S]),
    Edge("e_SH", inputs=[S], outputs=[H]),
    Edge("e_HV", inputs=[H], outputs=[V]),
)

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ.get("GW_SEED", "7")),
        run_dir=os.environ.get("GW_RUN_DIR"),
    )
    print("REASON:", result.reason)
    for nid in ("S", "H", "V"):
        st = result.states[nid]
        print(f"{nid}: state={st.output_state} outputs={st.outputs} inv={st.invocations}")
