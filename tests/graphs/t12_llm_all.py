"""T12: all three providers running concurrently under per-provider
semaphores, converging on one collector. The LLM semaphore admits all three
at once; each provider semaphore admits one."""

import os

from graph_workflow import Edge, Graph, LLMNode, ScriptNode, Semaphores, run

g = Graph(
    "t12-llm-all",
    semaphores=Semaphores(
        script=2, llm=3, human=1,
        providers={"claude": 1, "codex": 1, "grok": 1},
    ),
)

S = ScriptNode(
    "S", rank=0,
    fn=lambda i: {"wc": "aurora", "wx": "quartz", "wg": "basalt"},
    outputs={"wc": str, "wx": str, "wg": str},
)


def echo_prompt(var):
    return (
        "You are a JSON echo function. Reply with ONLY a JSON object — no prose, "
        f'no code fences: {{"{var}_echo": "${var}"}}'
    )


Lc = LLMNode(
    "Lc", rank=1, provider="claude", model="haiku", effort="low",
    prompt=echo_prompt("wc"),
    inputs={"wc": str}, outputs={"response": str, "wc_echo": str},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=300,
)
Lx = LLMNode(
    "Lx", rank=2, provider="codex", model="gpt-5.6-luna", effort="low",
    prompt=echo_prompt("wx"),
    inputs={"wx": str}, outputs={"response": str, "wx_echo": str},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=300,
)
Lg = LLMNode(
    "Lg", rank=3, provider="grok", model="grok-4.5", effort="low",
    prompt=echo_prompt("wg"),
    inputs={"wg": str}, outputs={"response": str, "wg_echo": str},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=300,
)


def collect(i):
    return {
        "ok": str(i.wc_echo) == i.wc and str(i.wx_echo) == i.wx
        and str(i.wg_echo) == i.wg
    }


V = ScriptNode(
    "V", rank=4,
    fn=collect,
    inputs={"wc": str, "wx": str, "wg": str,
            "wc_echo": str, "wx_echo": str, "wg_echo": str},
    outputs={"ok": bool},
    output_expression="isDefined(ok) and ok",
)
g.add_nodes(S, Lc, Lx, Lg, V)
g.add_edges(
    Edge("gen_S", inputs=[g.generator], outputs=[S]),
    Edge("e_fan", inputs=[S], outputs=[Lc, Lx, Lg]),   # divergent
    Edge("e_SV", inputs=[S], outputs=[V]),
    Edge("e_conv", inputs=[Lc, Lx, Lg], outputs=[V]),  # convergent
)

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ.get("GW_SEED", "7")),
        run_dir=os.environ.get("GW_RUN_DIR"),
    )
    print("REASON:", result.reason)
    for nid in ("S", "Lc", "Lx", "Lg", "V"):
        st = result.states[nid]
        print(f"{nid}: state={st.output_state} inv={st.invocations} "
              f"outputs={ {k: (str(v)[:60]) for k, v in st.outputs.items()} }")
