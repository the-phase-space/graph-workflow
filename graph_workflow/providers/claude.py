"""Claude provider adapter (``claude -p``).

Invocations use ``--input-format stream-json`` with stdin held open — the
steering channel: a user message written to the live session reaches the
agent after its current tool call, no interrupt required. That is how the
runtime's Pause delivers the halt message to a running node. stream-json
input requires stream-json output (with ``--verbose``); the terminal
``result`` event carries the reply text, session id, and usage, after which
stdin is closed and the process exits — deviations from the plain
``--output-format json`` reference form that this steering behavior
requires.
"""

from __future__ import annotations

import json

from .base import InvocationPlan, InvocationResult, ProviderAdapter


def _user_message_line(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
    ) + "\n"


def _is_result_line(line: str) -> bool:
    return line.lstrip().startswith("{") and '"type":"result"' in line


_CONTEXT_KEYS = ("input_tokens", "cache_creation_input_tokens",
                 "cache_read_input_tokens", "output_tokens")


def _context_tokens(usage: dict) -> int:
    """Tokens in one model call's context window: fresh + cached input and
    the reply (what Claude Code's footer reports)."""
    return sum(int(usage[k]) for k in _CONTEXT_KEYS
               if isinstance(usage.get(k), (int, float)))


class ClaudeAdapter(ProviderAdapter):
    name = "claude"
    executable = "claude"
    config_var = "CLAUDE_CONFIG_DIR"
    default_config_dir = ".claude"
    # Stripped just in case headless behavior under a parent Claude Code
    # session ever reverts (see Node Execution Contract in the spec).
    extra_env_strip = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION")

    def _common(self, node) -> list[str]:
        argv = ["-p", "--dangerously-skip-permissions"]
        argv += ["--model", node.model]
        if node.effort:
            argv += ["--effort", node.effort]
        if node.settings:
            argv += ["--settings", node.settings]
        argv += [
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
        ]
        argv += list(node.extra_args)
        return argv

    def _plan(self, argv: list[str], prompt: str) -> InvocationPlan:
        return InvocationPlan(
            argv=argv,
            stdin_initial=_user_message_line(prompt),
            hold_stdin=True,
            turn_end=_is_result_line,
            steer_encode=_user_message_line,
        )

    def plan_new(self, node, session_id, cwd, prompt, prompt_file):
        return self._plan(
            [self.resolve_exe(), *self._common(node), "--session-id", session_id],
            prompt,
        )

    def plan_resume(self, node, session_id, cwd, prompt, prompt_file):
        return self._plan(
            [self.resolve_exe(), *self._common(node), "--resume", session_id],
            prompt,
        )

    def parse(self, stdout, stderr, returncode):
        r = InvocationResult(raw=None)
        result_evt = None
        assistant_texts: list[str] = []
        last_call_usage: dict | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = obj.get("type")
            if etype == "result":
                result_evt = obj
            elif etype == "assistant":
                message = obj.get("message") or {}
                if isinstance(message.get("usage"), dict):
                    last_call_usage = message["usage"]
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        if block.get("text"):
                            assistant_texts.append(block["text"])
        if result_evt is not None:
            r.raw = result_evt
            r.text = result_evt.get("result") or (
                assistant_texts[-1] if assistant_texts else ""
            )
            r.session_id = result_evt.get("session_id") or result_evt.get("sessionId")
            usage = result_evt.get("usage") or {}
            if isinstance(usage, dict):
                r.usage = usage
                # The result usage is summed over the turn's model calls;
                # the context size is the *last* call's window: its
                # ``iterations`` entry when present, else the last assistant
                # event's usage snapshot.
                iterations = usage.get("iterations")
                last = (iterations[-1] if isinstance(iterations, list) and iterations
                        and isinstance(iterations[-1], dict) else last_call_usage)
                r.context_size = (
                    _context_tokens(last) if last else self._sum_usage(usage)
                )
            if result_evt.get("is_error"):
                r.ok = False
                r.error = f"claude reported error: {str(r.text)[:2000]}"
        else:
            r.raw = stdout
            r.text = assistant_texts[-1] if assistant_texts else stdout.strip()
            if returncode == 0:
                r.ok = False
                r.error = "claude produced no result event"
        if returncode != 0:
            r.ok = False
            r.error = r.error or (
                f"claude exited {returncode}: {(stderr or stdout).strip()[-2000:]}"
            )
        return r


class ClaudeBAdapter(ClaudeAdapter):
    """``claude`` on its second config dir — the second login."""

    name = "claudeb"
    config_dir = ".claudeb"
