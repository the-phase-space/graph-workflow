"""OpenRouter LLM sessions through the local DSH CLI (``dsh -p``)."""

from __future__ import annotations

import os

from ..errors import GraphDefinitionError
from .base import InvocationPlan, InvocationResult, ProviderAdapter

_MODEL_ALIASES = {"muse-spark-1.3": "meta/muse-spark-1.3-contributor"}


class OpenRouterAdapter(ProviderAdapter):
    name = "openrouter"
    executable = "dsh.exe" if os.name == "nt" else "dsh"
    config_var = "DSH_HOME"
    extra_env_strip = ("DSH_HOME", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION")

    def plan_new(self, node, session_id, cwd, prompt, prompt_file):
        argv = [self.resolve_exe(), "-p", "--provider", "openrouter",
                "--model", _MODEL_ALIASES.get(node.model, node.model)]
        if node.effort:
            argv += ["--effort", node.effort]
        argv += [*node.extra_args, "--session-id", session_id, "--", "-"]
        # Explicit stdin mode preserves arbitrary prompt length and literal
        # dash-only prompts; LiveInvocation closes stdin after this payload.
        return InvocationPlan(argv=argv, stdin_initial=prompt)

    def plan_resume(self, node, session_id, cwd, prompt, prompt_file):
        raise GraphDefinitionError(
            "openrouter: dsh -p does not support session resume; "
            "persist_from and resuming an interrupted turn are unavailable"
        )

    def parse(self, stdout, stderr, returncode):
        result = InvocationResult(text=stdout.strip(), raw=stdout)
        if returncode != 0:
            result.ok = False
            result.error = f"openrouter (dsh) exited {returncode}: {(stderr or stdout).strip()[-2000:]}"
        elif not result.text:
            result.ok = False
            result.error = "openrouter (dsh) produced no assistant text"
        return result
