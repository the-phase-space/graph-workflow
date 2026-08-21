"""T20: full context synthesis across THREE LLM layers.

A rank-0 script derives seed-unique payloads and splits them across a
divergent edge: A (claude sonnet) sees only (wa, na), B (codex terra) sees
only (wb, nb) — no layer-1 node sees everything. A convergent edge joins the
branches at M (claude sonnet), which must emit the cross-branch combination;
F (codex luna) extends it one more layer. The combined string and the sums
appear nowhere verbatim in any prompt — they can only exist in F's committed
outputs if the data actually flowed edge-wise through every layer."""

import os

from graph_workflow import Edge, Graph, LLMNode, ScriptNode, Semaphores, run

SEED = int(os.environ.get("GW_SEED", "7"))


def derive(seed: int) -> dict:
    """Deterministic seed-unique payloads; the test recomputes these."""
    return {
        "wa": f"zx{seed % 97}qv",
        "na": 100 + seed % 41,
        "wb": f"kj{seed % 89}mp",
        "nb": 200 + seed % 43,
    }


g = Graph(
    "t20-synth-deep",
    semaphores=Semaphores(
        script=2, llm=2, human=1, providers={"claude": 1, "codex": 1}
    ),
)

S = ScriptNode(
    "S", rank=0,
    fn=lambda i: derive(SEED),
    outputs={"wa": str, "na": int, "wb": str, "nb": int},
)

A = LLMNode(
    "A", rank=1,
    provider="claude", model="sonnet", effort="low",
    prompt=(
        "Relay task. codeword=$wa number=$na. "
        "End your reply with exactly one JSON object, no prose after it, "
        'no code fences: {"token_a": "$wa", "num_a": <$na plus 10>}'
    ),
    inputs={"wa": str, "na": int},
    outputs={"response": str, "token_a": str, "num_a": int},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=420,
)

B = LLMNode(
    "B", rank=2,
    provider="codex", model="gpt-5.6-terra", effort="low",
    prompt=(
        "Relay task. codeword=$wb number=$nb. "
        "End your reply with exactly one JSON object, no prose after it, "
        'no code fences: {"token_b": "$wb", "num_b": <$nb plus 10>}'
    ),
    inputs={"wb": str, "nb": int},
    outputs={"response": str, "token_b": str, "num_b": int},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=420,
)

M = LLMNode(
    "M", rank=3,
    provider="claude", model="sonnet", effort="low",
    prompt=(
        "Combine two branches. token_a=$token_a num_a=$num_a "
        "token_b=$token_b num_b=$num_b. "
        "End your reply with exactly one JSON object (replace each <...> "
        "with the actual value), no prose after it, no code fences: "
        '{"combined": "<token_a>-<token_b>", "total": <num_a plus num_b>}'
    ),
    inputs={"token_a": str, "num_a": int, "token_b": str, "num_b": int},
    outputs={"response": str, "combined": str, "total": int},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=420,
)

F = LLMNode(
    "F", rank=4,
    provider="codex", model="gpt-5.6-luna", effort="low",
    prompt=(
        "Finalize. combined=$combined total=$total. "
        "End your reply with exactly one JSON object (replace each <...> "
        "with the actual value), no prose after it, no code fences: "
        '{"final_tag": "<combined>!", "grand": <total plus 1>}'
    ),
    inputs={"combined": str, "total": int},
    outputs={"response": str, "final_tag": str, "grand": int},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=420,
)

g.add_nodes(S, A, B, M, F)
g.add_edges(
    Edge("gen_S", inputs=[g.generator], outputs=[S]),
    Edge("e_fan", inputs=[S], outputs=[A, B]),    # divergent: split payloads
    Edge("e_conv", inputs=[A, B], outputs=[M]),   # convergent: join branches
    Edge("e_MF", inputs=[M], outputs=[F]),        # third LLM layer
)

if __name__ == "__main__":
    result = run(
        g,
        seed=SEED,
        run_dir=os.environ.get("GW_RUN_DIR"),
    )
    print("REASON:", result.reason)
    for nid in ("S", "A", "B", "M", "F"):
        st = result.states[nid]
        print(f"{nid}: state={st.output_state} inv={st.invocations} "
              f"outputs={ {k: (str(v)[:80]) for k, v in st.outputs.items()} }")
