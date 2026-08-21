"""Provider adapter base: CLI construction, environment stripping, process
control.

LLM nodes shell out to their provider CLIs. Provider API-key environment
variables are stripped so an invocation can never fall through to a direct
API request. Each headless command exits after one response; sessions persist
provider-side and are re-entered with the resume form bound to the retained
session ID.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..errors import GraphDefinitionError

#: Stripped from every LLM node's environment regardless of provider.
COMMON_ENV_STRIP = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY")


@dataclass
class InvocationResult:
    text: str = ""
    session_id: str | None = None
    usage: dict = field(default_factory=dict)
    context_size: int = 0
    raw: Any = None
    ok: bool = True
    error: str | None = None


class ProviderAdapter:
    name: str = "abstract"
    executable: str = ""
    extra_env_strip: tuple[str, ...] = ()
    #: True if the CLI takes its prompt from a file path instead of stdin.
    needs_prompt_file: bool = False

    _resolved_exe: str | None = None

    def resolve_exe(self) -> str:
        if self._resolved_exe is None:
            exe = shutil.which(self.executable)
            if not exe:
                raise GraphDefinitionError(
                    f"provider {self.name!r}: executable {self.executable!r} not found on PATH"
                )
            type(self)._resolved_exe = exe
        return self._resolved_exe

    def build_env(self) -> dict[str, str]:
        strip = {s.upper() for s in (*COMMON_ENV_STRIP, *self.extra_env_strip)}
        return {k: v for k, v in os.environ.items() if k.upper() not in strip}

    # -- invocation planning (implemented per provider) -------------------- #

    def plan_new(self, node, session_id: str, cwd: str, prompt: str,
                 prompt_file: str | None) -> "InvocationPlan":
        raise NotImplementedError

    def plan_resume(self, node, session_id: str, cwd: str, prompt: str,
                    prompt_file: str | None) -> "InvocationPlan":
        raise NotImplementedError

    def parse(self, stdout: str, stderr: str, returncode: int) -> InvocationResult:
        raise NotImplementedError

    # -- shared helpers ---------------------------------------------------- #

    @staticmethod
    def _sum_usage(usage: dict) -> int:
        """Best-effort context size: total tokens across the usage record."""
        total = 0
        for key, value in usage.items():
            if isinstance(value, (int, float)) and "token" in key.lower():
                total += int(value)
        return total


@dataclass
class InvocationPlan:
    """Everything needed to launch one CLI invocation.

    ``hold_stdin`` keeps stdin open after the initial payload — the steering
    channel: a message written to the live session reaches the agent after
    its current tool call, no interrupt required (claude's stream-json input
    mode). ``turn_end`` detects the stdout line that ends the interaction so
    stdin can be closed and the process allowed to exit; ``steer_encode``
    wraps a steering message in the provider's wire format (None = the
    provider exposes no steering channel and Pause falls back to interrupt).
    """

    argv: list[str]
    stdin_initial: str | None = None
    hold_stdin: bool = False
    turn_end: Callable[[str], bool] | None = None
    steer_encode: Callable[[str], str] | None = None


class LiveInvocation:
    """A running CLI invocation: incremental output capture, optional live
    steering via held-open stdin, and tree-kill for Stop/timeout/fallback."""

    def __init__(self, plan: InvocationPlan, env: dict[str, str], cwd: str | None,
                 tee_path: str | None = None):
        self.plan = plan
        self.steered = False
        self.killed = False
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._turn_ended = threading.Event()
        self._stdin_lock = threading.Lock()
        # Live activity surface for the Monitor GUI: provider stdout (the
        # session's event stream) is teed line-by-line to a file as it
        # arrives, so an external process can tail the session in real time.
        self._tee = None
        if tee_path:
            try:
                self._tee = open(tee_path, "a", encoding="utf-8", buffering=1)
            except OSError:
                self._tee = None
        wants_stdin = plan.stdin_initial is not None or plan.hold_stdin
        self.proc = subprocess.Popen(
            plan.argv,
            stdin=subprocess.PIPE if wants_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=cwd,
        )
        self._t_out = threading.Thread(target=self._read_stdout, daemon=True)
        self._t_err = threading.Thread(target=self._read_stderr, daemon=True)
        self._t_out.start()
        self._t_err.start()
        if plan.stdin_initial is not None:
            with self._stdin_lock:
                try:
                    self.proc.stdin.write(plan.stdin_initial)
                    self.proc.stdin.flush()
                except OSError:
                    pass
        if not plan.hold_stdin:
            self._close_stdin()

    def _read_stdout(self):
        try:
            for line in self.proc.stdout:
                self._stdout.append(line)
                if self._tee is not None:
                    try:
                        self._tee.write(line)
                    except Exception:
                        self._tee = None
                if self.plan.turn_end is not None and self.plan.turn_end(line):
                    self._turn_ended.set()
        except Exception:
            pass
        self._turn_ended.set()  # EOF ends the interaction too
        if self._tee is not None:
            try:
                self._tee.close()
            except Exception:
                pass

    def _read_stderr(self):
        try:
            for line in self.proc.stderr:
                self._stderr.append(line)
        except Exception:
            pass

    def _close_stdin(self):
        with self._stdin_lock:
            stdin = self.proc.stdin
            if stdin is not None and not stdin.closed:
                try:
                    stdin.close()
                except OSError:
                    pass

    def steer(self, message: str) -> bool:
        """Deliver a message to the live session mid-turn. True only if the
        provider supports steering and the write actually landed."""
        if self.plan.steer_encode is None or self.killed:
            return False
        with self._stdin_lock:
            stdin = self.proc.stdin
            if stdin is None or stdin.closed or self.proc.poll() is not None:
                return False
            try:
                stdin.write(self.plan.steer_encode(message))
                stdin.flush()
            except OSError:
                return False
        self.steered = True
        return True

    def kill(self):
        self.killed = True
        if self.proc.poll() is None:
            kill_tree(self.proc.pid)

    def finish(self, timeout_s: float | None) -> tuple[str, str, int, bool]:
        """Block until the invocation completes; returns
        (stdout, stderr, returncode, timed_out)."""
        timed_out = False
        deadline = time.monotonic() + timeout_s if timeout_s else None

        def remaining():
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        if self.plan.hold_stdin:
            if not self._turn_ended.wait(timeout=remaining()):
                timed_out = True
                self.kill()
            self._close_stdin()
        try:
            rc = self.proc.wait(timeout=remaining())
        except subprocess.TimeoutExpired:
            timed_out = True
            self.kill()
            try:
                rc = self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                rc = -1
        self._t_out.join(timeout=5)
        self._t_err.join(timeout=5)
        return "".join(self._stdout), "".join(self._stderr), rc, timed_out


def kill_tree(pid: int):
    """Terminate a process and its descendants (CLI shims spawn children)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(pid), 15)
        except Exception:
            try:
                os.kill(pid, 9)
            except Exception:
                pass


def invoke(
    argv: list[str],
    stdin_text: str | None,
    env: dict[str, str],
    cwd: str | None,
    timeout_s: float | None,
    on_proc=None,
) -> tuple[str, str, int, bool]:
    """Run one CLI invocation. Returns (stdout, stderr, returncode, timed_out).

    ``on_proc`` receives the Popen handle so the runtime can interrupt the
    invocation (Pause/Stop) by killing the process tree; an interrupt
    surfaces here as an ordinary nonzero exit with partial output.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=cwd,
    )
    if on_proc is not None:
        on_proc(proc)
    timed_out = False
    try:
        stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=15)
        except Exception:
            stdout, stderr = "", ""
    finally:
        if on_proc is not None:
            on_proc(None)
    return stdout or "", stderr or "", proc.returncode if proc.returncode is not None else -1, timed_out
