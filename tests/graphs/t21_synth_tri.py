"""T21: 3-way divergence -> 3-way convergence synthesis (two LLM layers).

The rank-0 script splits three seed-unique codeword/number pairs across one
divergent edge; each layer-1 relay sees exactly one pair (declared inputs
select the slice). The 3-input convergent edge delivers all six relayed
values to Z (codex terra), whose committed outputs must contain the
cross-branch join and sum — values that appear verbatim in no prompt."""

import os

from graph_workflow import Edge, Graph, LLMNode, ScriptNode, Semaphores, run

SEED = int(os.environ.get("GW_SEED", "7"))


def derive(seed: int) -> dict:
    """Deterministic seed-unique payloads; the test recomputes these."""
    return {
        "w1": f"vq{seed % 83}xr", "n1": 10 + seed % 37,
        "w2": f"pn{seed % 79}wt", "n2": 20 + seed % 31,
        "w3": f"gd{seed % 73}hs", "n3": 30 + seed % 29,
    }


g = Graph(
    "t21-synth-tri",
    semaphores=Semaphores(
        script=2, llm=2, human=1, providers={"claude": 1, "codex": 1}
    ),
)

S = ScriptNode(
    "S", rank=0,
    fn=lambda i: derive(SEED),
    outputs={"w1": str, "n1": int, "w2": str, "n2": int, "w3": str, "n3": int},
)


def relay_prompt(k: str) -> str:
    return (
        f"Relay task. codeword=$w{k} number=$n{k}. "
        "End your reply with exactly one JSON object, no prose after it, "
        f'no code fences: {{"t{k}": "$w{k}", "m{k}": <$n{k} multiplied by 2>}}'
    )


P1 = LLMNode(
    "P1", rank=1, provider="claude", model="haiku", effort="low",
    prompt=relay_prompt("1"),
    inputs={"w1": str, "n1": int},
    outputs={"response": str, "t1": str, "m1": int},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=420,
)
P2 = LLMNode(
    "P2", rank=2, provider="codex", model="gpt-5.6-luna", effort="low",
    prompt=relay_prompt("2"),
    inputs={"w2": str, "n2": int},
    outputs={"response": str, "t2": str, "m2": int},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=420,
)
P3 = LLMNode(
    "P3", rank=3, provider="claude", model="haiku", effort="low",
    prompt=relay_prompt("3"),
    inputs={"w3": str, "n3": int},
    outputs={"response": str, "t3": str, "m3": int},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=420,
)

Z = LLMNode(
    "Z", rank=4,
    provider="codex", model="gpt-5.6-terra", effort="low",
    prompt=(
        "Synthesize three branches. t1=$t1 t2=$t2 t3=$t3 "
        "m1=$m1 m2=$m2 m3=$m3. "
        "End your reply with exactly one JSON object (replace each <...> "
        "with the actual value, keep the t1,t2,t3 order), no prose after "
        'it, no code fences: {"combined": "<t1>-<t2>-<t3>", '
        '"sum": <m1 plus m2 plus m3>}'
    ),
    inputs={"t1": str, "m1": int, "t2": str, "m2": int, "t3": str, "m3": int},
    outputs={"response": str, "combined": str, "sum": int},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=420,
)

g.add_nodes(S, P1, P2, P3, Z)
g.add_edges(
    Edge("gen_S", inputs=[g.generator], outputs=[S]),
    Edge("e_fan", inputs=[S], outputs=[P1, P2, P3]),    # 3-way divergent
    Edge("e_conv", inputs=[P1, P2, P3], outputs=[Z]),   # 3-way convergent
)

if __name__ == "__main__":
    result = run(
        g,
        seed=SEED,
        run_dir=os.environ.get("GW_RUN_DIR"),
    )
    print("REASON:", result.reason)
    for nid in ("S", "P1", "P2", "P3", "Z"):
        st = result.states[nid]
        print(f"{nid}: state={st.output_state} inv={st.invocations} "
              f"outputs={ {k: (str(v)[:80]) for k, v in st.outputs.items()} }")
