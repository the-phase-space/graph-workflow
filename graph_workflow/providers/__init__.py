"""Provider adapter registry. 1 LLM node = 1 provider = 1 CLI invocation."""

from __future__ import annotations

from ..errors import GraphDefinitionError
from .base import (
    COMMON_ENV_STRIP,
    CONFIG_DIR_VARS,
    InvocationPlan,
    InvocationResult,
    LiveInvocation,
    ProviderAdapter,
    invoke,
    kill_tree,
)
from .claude import ClaudeAdapter, ClaudeBAdapter
from .codex import CodexAdapter, CodexBAdapter
from .grok import GrokAdapter
from .openrouter import OpenRouterAdapter

_REGISTRY: dict[str, ProviderAdapter] = {
    a.name: a for a in (
        ClaudeAdapter(), ClaudeBAdapter(), CodexAdapter(), CodexBAdapter(), GrokAdapter(),
        OpenRouterAdapter(),
    )
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


def available_providers() -> list[str]:
    """Known providers whose CLI is on PATH and whose login (config dir)
    exists on this machine — what the Monitor offers in its provider menus.
    Delete ``~/.codexb`` (or uninstall ``codex``) and ``codexb`` drops out;
    the runtime itself still accepts any known name."""
    return [name for name in sorted(_REGISTRY) if _REGISTRY[name].available()]


__all__ = [
    "COMMON_ENV_STRIP",
    "CONFIG_DIR_VARS",
    "InvocationPlan",
    "InvocationResult",
    "LiveInvocation",
    "ProviderAdapter",
    "available_providers",
    "get_adapter",
    "invoke",
    "kill_tree",
    "known_providers",
]
