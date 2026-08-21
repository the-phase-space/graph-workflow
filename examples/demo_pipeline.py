"""demo-pipeline — a graph exercising most of the Monitor's visual surface.

Topology: GEN -> A (ingest) fans out to workers B/C/D; B+C converge into E
(merge); D feeds F, which always errors; E loops back to A once (bounded
cycle), invokes a real claude node, and gates off a codex node, a grok node,
and a human node behind a False universal gate (visible but never ready).
Script semaphore 2 keeps a worker queued (ready) while two run.
"""

import os
import time

from graph_workflow import Edge, Graph, HumanNode, LLMNode, ScriptNode, Semaphores, run

g = Graph("demo-pipeline", semaphores=Semaphores(script=2, llm=2, human=1))


def ingest(i):
    time.sleep(3)
    return {"x": 10}


def work_b(i):
    time.sleep(9)
    return {"y1": i.x * 2}


def work_c(i):
    time.sleep(12)
    return {"y2": i.x + 5}


def work_d(i):
    time.sleep(15)
    return {"z": i.x - 1}


def merge(i):
    time.sleep(6)
    return {"merged": f"y1={i.y1} y2={i.y2}"}


def explode(i):
    raise ValueError(f"deliberate demo failure on z={i.z}")


A = ScriptNode("A", rank=0, fn=ingest, outputs={"x": int},
               gate="(gen_A and A.invocations == 0) or (back_EA and A.invocations < 2)")
B = ScriptNode("B", rank=1, fn=work_b, inputs={"x": int}, outputs={"y1": int})
C = ScriptNode("C", rank=2, fn=work_c, inputs={"x": int}, outputs={"y2": int})
D = ScriptNode("D", rank=3, fn=work_d, inputs={"x": int}, outputs={"z": int})
E = ScriptNode("E", rank=4, fn=merge, inputs={"y1": int, "y2": int},
               outputs={"merged": str})
F = ScriptNode("F", rank=5, fn=explode, inputs={"z": int}, outputs={"ok": bool})

Lc = LLMNode(
    "Lc", rank=6, provider="claude", model="haiku", effort="low",
    prompt=(
        "Compose a one-line haiku about the measurement '$merged'. Reply with "
        'ONLY a JSON object, no prose: {"haiku": "<the haiku>"}'
    ),
    inputs={"merged": str}, outputs={"response": str, "haiku": str},
    cwd=os.environ.get("GW_RUN_DIR"), timeout_s=300,
)
Lx = LLMNode(
    "Lx", rank=7, provider="codex", model="gpt-5.6-luna", effort="low",
    prompt="Unreachable in this demo: $merged",
    inputs={"merged": str}, outputs={"response": str},
)
Lg = LLMNode(
    "Lg", rank=8, provider="grok", model="grok-4.5", effort="low",
    prompt="Unreachable in this demo: $merged",
    inputs={"merged": str}, outputs={"response": str},
)
H = HumanNode(
    "H", rank=9, prompt="Unreachable in this demo: ratify $merged",
    inputs={"merged": str}, outputs={"approved": bool},
)

g.add_nodes(A, B, C, D, E, F, Lc, Lx, Lg, H)
g.add_edges(
    Edge("gen_A", inputs=[g.generator], outputs=[A]),
    Edge("e_fan", inputs=[A], outputs=[B, C, D]),
    Edge("e_conv", inputs=[B, C], outputs=[E]),
    Edge("e_DF", inputs=[D], outputs=[F]),
    Edge("e_Lc", inputs=[E], outputs=[Lc]),
    Edge("e_off", inputs=[E], outputs=[Lx, Lg, H], universal_gate="False"),
    Edge("back_EA", inputs=[E], outputs=[A],
         input_gates={"E": "E.invocations < 2"}),
)

if __name__ == "__main__":
    result = run(
        g,
        seed=int(os.environ["GW_SEED"]) if os.environ.get("GW_SEED") else None,
        run_dir=os.environ.get("GW_RUN_DIR") or None,
    )
    print("REASON:", result.reason)
