"""Grok provider adapter (``grok``). Prompts are passed via ``--prompt-file``;
the ``max`` reasoning effort is converted to ``xhigh``."""

from __future__ import annotations

import json

from .base import InvocationPlan, InvocationResult, ProviderAdapter

_EFFORT_MAP = {"max": "xhigh"}


class GrokAdapter(ProviderAdapter):
    name = "grok"
    executable = "grok"
    config_var = "GROK_HOME"
    needs_prompt_file = True
    # Headless grok (--prompt-file single-turn) exposes no stdin steering
    # channel; Pause falls back to interrupt + halt-via-resume.

    def _common(self, node, cwd, prompt_file) -> list[str]:
        argv = ["--prompt-file", prompt_file, "--yolo", "--no-auto-update"]
        argv += ["--cwd", cwd, "--model", node.model]
        if node.effort:
            argv += ["--reasoning-effort", _EFFORT_MAP.get(node.effort, node.effort)]
        argv += ["--verbatim", "--output-format", "json"]
        argv += list(node.extra_args)
        return argv

    def plan_new(self, node, session_id, cwd, prompt, prompt_file):
        return InvocationPlan(
            argv=[self.resolve_exe(), *self._common(node, cwd, prompt_file),
                  "--session-id", session_id],
        )

    def plan_resume(self, node, session_id, cwd, prompt, prompt_file):
        return InvocationPlan(
            argv=[self.resolve_exe(), *self._common(node, cwd, prompt_file),
                  "--resume", session_id],
        )

    def parse(self, stdout, stderr, returncode):
        r = InvocationResult(raw=stdout)
        data = None
        try:
            data = json.loads(stdout[stdout.index("{"):])
        except (ValueError, json.JSONDecodeError):
            # Maybe JSONL: take the last parseable object with a result field.
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
        if isinstance(data, dict):
            r.raw = data
            for key in ("result", "response", "text", "output", "content", "message"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    r.text = value
                    break
            r.session_id = data.get("sessionId") or data.get("session_id")
            usage = data.get("usage") or data.get("tokens") or {}
            if isinstance(usage, dict):
                r.usage = usage
                r.context_size = self._sum_usage(usage)
        else:
            r.text = stdout.strip()
        if returncode != 0:
            r.ok = False
            r.error = f"grok exited {returncode}: {(stderr or stdout).strip()[-2000:]}"
        return r
