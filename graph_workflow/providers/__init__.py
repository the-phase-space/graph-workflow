"""Provider adapter registry. 1 LLM node = 1 provider = 1 CLI invocation."""

from __future__ import annotations

from ..errors import GraphDefinitionError
from .base import (
    COMMON_ENV_STRIP,
    InvocationPlan,
    InvocationResult,
    LiveInvocation,
    ProviderAdapter,
    invoke,
    kill_tree,
)
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .grok import GrokAdapter

_REGISTRY: dict[str, ProviderAdapter] = {
    a.name: a for a in (ClaudeAdapter(), CodexAdapter(), GrokAdapter())
}


def get_adapter(name: str) -> ProviderAdapter:
    try:
        return _REGISTRY[name.lower()]
    except KeyError:
        raise GraphDefinitionError(
            f"unknown provider {name!r}; known providers: {sorted(_REGISTRY)}"
        ) from None


def known_providers() -> list[str]:
    return sorted(_REGISTRY)


__all__ = [
    "COMMON_ENV_STRIP",
    "InvocationPlan",
    "InvocationResult",
    "LiveInvocation",
    "ProviderAdapter",
    "get_adapter",
    "invoke",
    "kill_tree",
    "known_providers",
]
