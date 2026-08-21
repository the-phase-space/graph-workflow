"""T99: structural validation negatives. Duplicate rank, an unsatisfiable
declared input, a primary node declaring data inputs, a syntax error in a
gate, and an edge upstream of the Generator must all be reported together in
one GraphValidationError."""

from graph_workflow import (
    Edge,
    Graph,
    GraphValidationError,
    ScriptNode,
    Semaphores,
    run,
)

g = Graph("t99-invalid", semaphores=Semaphores())

A = ScriptNode("A", rank=0, fn=lambda i: {"x": 1}, outputs={"x": int},
               inputs={"illegal": str})          # primary node declaring inputs
B = ScriptNode("B", rank=0, fn=lambda i: {}, )   # duplicate rank
C = ScriptNode("C", rank=2, fn=lambda i: {},
               inputs={"nowhere": str},           # no upstream supplier
               gate="A.x >")                      # syntax error
g.add_nodes(A, B, C)
g.add_edges(
    Edge("gen_A", inputs=[g.generator], outputs=[A]),
    Edge("gen_B", inputs=[g.generator], outputs=[B]),
    Edge("e_AC", inputs=[A], outputs=[C]),
    Edge("e_CG", inputs=[C], outputs=["GEN"]),    # nothing can be upstream of GEN
)

if __name__ == "__main__":
    try:
        run(g, seed=1)
        print("VALIDATION_MISSED")
    except GraphValidationError as exc:
        print("VALIDATION_ERROR_OK")
        for p in exc.problems:
            print(" -", p)
