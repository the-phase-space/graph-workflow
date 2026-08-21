"""graph-workflow: a runtime library for composing and executing directed
graphs of script, LLM, and human nodes.

A graph definition is an executable Python file: it declares its topology as
data using this API and invokes the reusable runtime —

    from graph_workflow import Graph, ScriptNode, Edge, Semaphores, run

    g = Graph("example", semaphores=Semaphores(script=4, llm=2, human=1))
    A = ScriptNode("A", rank=0, fn=lambda i: {"x": 1}, outputs={"x": int})
    g.add_node(A)
    g.add_edge(Edge("gen_A", inputs=[g.generator], outputs=[A]))
    result = run(g, seed=42)

Executing the file starts a run of its graph.
"""

from .engine import (
    DEFAULT_PAUSE_MESSAGE,
    DEFAULT_RESUME_MESSAGE,
    RunResult,
    Runtime,
    resume_run,
    run,
)
from .errors import (
    GraphDefinitionError,
    GraphValidationError,
    GraphWorkflowError,
    TraceReplayError,
)
from .model import (
    UNDEFINED,
    Edge,
    Gate,
    Generator,
    Graph,
    HumanNode,
    LLMNode,
    Node,
    ScriptNode,
    Semaphores,
    isDefined,
)
from .trace import read_events
from .validation import validate

__all__ = [
    "DEFAULT_PAUSE_MESSAGE",
    "DEFAULT_RESUME_MESSAGE",
    "Edge",
    "Gate",
    "Generator",
    "Graph",
    "GraphDefinitionError",
    "GraphValidationError",
    "GraphWorkflowError",
    "HumanNode",
    "LLMNode",
    "Node",
    "RunResult",
    "Runtime",
    "ScriptNode",
    "Semaphores",
    "TraceReplayError",
    "UNDEFINED",
    "isDefined",
    "read_events",
    "resume_run",
    "run",
    "validate",
]
