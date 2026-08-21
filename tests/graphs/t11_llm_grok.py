"""T11: real LLM node, provider grok. Same shape as T9."""

import os

from graph_workflow import Edge, Graph, LLMNode, ScriptNode, Semaphores, run

g = Graph(
    "t11-llm-grok",
    semaphores=Semaphores(script=2, llm=1, human=1, providers={"grok": 1}),
)

S = ScriptNode(
    "S", rank=0,
    fn=lambda i: {"word": "confluence", "n": 14},
    outputs={"word": str, "n": int},
)
L = LLMNode(
    "L", rank=1,
    provider="grok", model="grok-4.5", effort="low",
    prompt=(
        "You are a JSON echo function. Reply with ONLY a JSON object — no prose, "
        'no code fences: {"echo": "$word", "double": <$n multiplied by 2>}'
    ),
    inputs={"word": str, "n": int},
    outputs={"response": str, "echo": str, "double": int},
    cwd=os.environ.get("GW_RUN_DIR"),
    timeout_s=300,
)


def check(i):
    return {"ok": str(i.echo) == i.word and int(i.double) == 2 * i.n}


V = ScriptNode(
    "V", rank=2,
    fn=check,
    inputs={"word": str, "n": int, "echo": str, "double": int},
    outputs={"ok": bool},
    output_expression="isDefined(ok) and ok",
)
g.add_nodes(S, L, V)
g.add_edges(
    Edge("gen_S", inputs=[g.generator], outputs=[S]),
    Edge("e_SL", inputs=[S], outputs=[L]),
    Edge("e_SV", inputs=[S], outputs=[V]),
    Edge("e_LV", inputs=[L], outputs=[V]),
)

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ.get("GW_SEED", "7")),
        run_dir=os.environ.get("GW_RUN_DIR"),
    )
    print("REASON:", result.reason)
    for nid in ("S", "L", "V"):
        st = result.states[nid]
        print(f"{nid}: state={st.output_state} inv={st.invocations} "
              f"outputs={ {k: (str(v)[:80]) for k, v in st.outputs.items()} }")
