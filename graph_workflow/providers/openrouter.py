"""OpenRouter LLM sessions through the local DSH CLI (``dsh -p``).

``dsh -p`` prints only the final assistant text, so usage is recovered after
the fact from DSH's persisted transcript: ``dsh export <session-id>`` decodes
it to JSONL, and each ``assistant/message`` event carries that step's
``usage`` (``inputTokens``/``outputTokens``/``costUsd``). Output tokens and
cost are summed across steps; the context size is the last step's input +
output (the context the session ended at).
"""

from __future__ import annotations

import json
import os
import subprocess

from ..errors import GraphDefinitionError
from .base import CREATIONFLAGS, InvocationPlan, InvocationResult, ProviderAdapter

_MODEL_ALIASES = {
    "muse-spark-1.3": "meta/muse-spark-1.3-contributor",
    "muse-wonder": "meta/muse-wonder",
}

#: Reading one transcript takes ~0.2 s; this is only a guard against a
#: wedged export holding the node's commit hostage.
_EXPORT_TIMEOUT_S = 30


class OpenRouterAdapter(ProviderAdapter):
    name = "openrouter"
    executable = "dsh.exe" if os.name == "nt" else "dsh"
    config_var = "DSH_HOME"
    default_config_dir = ".dsh"
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

    def after_parse(self, result, session_id):
        if not session_id:
            return
        try:
            usage, context_size = self._usage_from_transcript(session_id)
        except Exception:
            return
        if usage:
            result.usage = usage
            result.context_size = context_size

    def _usage_from_transcript(self, session_id: str) -> tuple[dict, int]:
        proc = subprocess.run(
            [self.resolve_exe(), "export", session_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.build_env(),
            timeout=_EXPORT_TIMEOUT_S,
            check=False,
            creationflags=CREATIONFLAGS,
        )
        if proc.returncode != 0:
            return {}, 0
        return _sum_transcript_usage(proc.stdout)


def _sum_transcript_usage(jsonl: str) -> tuple[dict, int]:
    input_tokens = output_tokens = steps = 0
    cost = 0.0
    last_context = 0
    for line in jsonl.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant/message":
            continue
        usage = (obj.get("data") or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        i = usage.get("inputTokens")
        o = usage.get("outputTokens")
        i = int(i) if isinstance(i, (int, float)) else 0
        o = int(o) if isinstance(o, (int, float)) else 0
        input_tokens += i
        output_tokens += o
        c = usage.get("costUsd")
        if isinstance(c, (int, float)):
            cost += float(c)
        last_context = i + o
        steps += 1
    if not steps:
        return {}, 0
    usage = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "steps": steps,
    }
    if cost:
        usage["costUsd"] = round(cost, 6)
    return usage, last_context
