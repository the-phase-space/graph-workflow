"""Exception types for graph-workflow."""


class GraphWorkflowError(Exception):
    """Base class for all graph-workflow errors."""


class GraphValidationError(GraphWorkflowError):
    """Raised when a graph definition fails structural validation.

    Carries the full list of problems so authors see everything at once.
    """

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__(
            "graph validation failed:\n" + "\n".join(f"  - {p}" for p in self.problems)
        )


class GraphDefinitionError(GraphWorkflowError):
    """Raised at construction time for malformed element declarations."""


class TraceReplayError(GraphWorkflowError):
    """Raised when a trace cannot be replayed against the given graph/seed."""


class NodeInterrupted(GraphWorkflowError):
    """Internal: a running node's process was interrupted by Pause/Stop."""
