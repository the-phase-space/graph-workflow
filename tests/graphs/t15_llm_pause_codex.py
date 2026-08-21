"""T15: live Pause/Resume against a real codex app-server session. Mirrors
T14 with a tool-heavy task (a 20s sleep command) so the pause lands mid-turn:
the runtime steers the halt message into the live turn via ``turn/steer``,
the message reaches the agent after the tool call, the model ends its
following turn (the ack), and Resume re-enters the persisted thread with the
session-bound continue message to complete the invocation."""

import os

from graph_workflow import Edge, Graph, LLMNode, Semaphores, run

g = Graph(
    "t15-llm-pause-codex",
    semaphores=Semaphores(script=1, llm=1, human=1, providers={"codex": 1}),
)

L = LLMNode(
    "L", rank=0,
    provider="codex", model="gpt-5.6-luna", effort="low",
    prompt=(
        "Run this exact shell command and wait for it to finish: "
        "python -c \"import time; time.sleep(20); print('MARKER')\" . "
        "Then count from 1 to 30 in English words, one per line, "
        "and end with DONE."
    ),
    outputs={"response": str},
    cwd=os.environ.get("GW_RUN_DIR"),
    timeout_s=420,
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
