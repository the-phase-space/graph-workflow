"""T14: live Pause/Resume against a real LLM session (claude). A single
primary LLM node with a deliberately slow task; the harness pauses mid-turn
via control.json, the runtime interrupts the session and sends the halt
message, and Resume completes the invocation with the session-bound
continue message."""

import os

from graph_workflow import Edge, Graph, LLMNode, Semaphores, run

g = Graph(
    "t14-llm-pause",
    semaphores=Semaphores(script=1, llm=1, human=1, providers={"claude": 1}),
)

L = LLMNode(
    "L", rank=0,
    provider="claude", model="haiku", effort="low",
    prompt=(
        "Count from 1 to 60 in English words, one per line (one, two, three, "
        "...). Do not skip any. After the complete list, write DONE."
    ),
    outputs={"response": str},
    cwd=os.environ.get("GW_RUN_DIR"),
    timeout_s=300,
)
g.add_node(L)
g.add_edge(Edge("gen_L", inputs=[g.generator], outputs=[L]))

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ.get("GW_SEED", "7")),
        run_dir=os.environ.get("GW_RUN_DIR"),
    )
    print("REASON:", result.reason)
    st = result.states["L"]
    print(f"L: state={st.output_state} inv={st.invocations} "
          f"response_len={len(str(st.outputs.get('response', '')))}")
