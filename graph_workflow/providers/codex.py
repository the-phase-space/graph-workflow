"""Codex provider adapter (``codex exec``).

Codex generates its own thread ID: new sessions are launched without a
session argument and ``thread_id`` is retained from the ``thread.started``
JSONL event. The resume form re-enters that thread.
"""

from __future__ import annotations

import json

from .base import InvocationPlan, InvocationResult, ProviderAdapter


class CodexAdapter(ProviderAdapter):
    name = "codex"
    executable = "codex"
    #: The runtime cannot choose the session id; it is read from output.
    provider_assigns_session = True
    # ``codex exec`` reads piped stdin fully at startup as prompt material (a
    # ``<stdin>`` block) — it is NOT a live steering channel; holding stdin
    # open blocks the turn from starting. Verified empirically against codex
    # 0.148.0. Pause therefore falls back to interrupt + halt-via-resume.

    def _common(self, node) -> list[str]:
        argv = ["--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
        argv += ["--model", node.model, "--json"]
        if node.effort:
            argv += ["--config", f"model_reasoning_effort={node.effort}"]
        argv += list(node.extra_args)
        return argv

    def plan_new(self, node, session_id, cwd, prompt, prompt_file):
        return InvocationPlan(
            argv=[self.resolve_exe(), "exec", *self._common(node), "--cd", cwd, "-"],
            stdin_initial=prompt,
        )

    def plan_resume(self, node, session_id, cwd, prompt, prompt_file):
        return InvocationPlan(
            argv=[self.resolve_exe(), "exec", "resume", *self._common(node),
                  session_id, "-"],
            stdin_initial=prompt,
        )

    def parse(self, stdout, stderr, returncode):
        r = InvocationResult(raw=None)
        messages: list[str] = []
        events = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(obj)
            etype = obj.get("type", "")
            if etype == "thread.started":
                r.session_id = obj.get("thread_id") or obj.get("threadId")
            elif etype == "item.completed":
                item = obj.get("item") or {}
                if item.get("item_type") == "agent_message" or item.get("type") == "agent_message":
                    text = item.get("text") or item.get("message") or ""
                    if text:
                        messages.append(text)
            elif etype == "turn.completed":
                usage = obj.get("usage") or {}
                if isinstance(usage, dict):
                    r.usage = usage
                    r.context_size = self._sum_usage(usage)
            elif "msg" in obj:  # legacy event shape
                msg = obj.get("msg") or {}
                if msg.get("type") == "agent_message":
                    text = msg.get("message") or ""
                    if text:
                        messages.append(text)
                if msg.get("type") == "session_configured" and not r.session_id:
                    r.session_id = msg.get("session_id")
        r.raw = events
        r.text = messages[-1] if messages else ""
        if returncode != 0:
            r.ok = False
            r.error = f"codex exited {returncode}: {(stderr or stdout).strip()[-2000:]}"
        elif not messages:
            r.ok = False
            r.error = "codex produced no agent message"
        return r
