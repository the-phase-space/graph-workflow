"""Codex provider adapter (``codex app-server``).

Codex invocations speak the app-server stdio JSON-RPC protocol rather than
``codex exec``: ``exec`` reads piped stdin fully at startup as prompt
material (verified against codex 0.148.0 — not a live channel), while the
app-server exposes ``turn/steer`` — a first-class mid-turn steering method
whose message reaches the agent after its current tool call, no interrupt —
plus ``thread/resume`` for re-entering a persisted thread from any process.

Codex assigns the session id: it is the thread id from the ``thread/start``
response. One caveat governs process hygiene: a live app-server holds an
exclusive writer lock on its thread, so every invocation must shut its
server down cleanly (stdin close, then kill) or later resumes fail with
"thread already has an active writer".

Set ``GW_CODEX_EXEC=1`` to fall back to the legacy ``codex exec`` path
(no steering; Pause interrupts the turn instead).
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

from .base import InvocationPlan, InvocationResult, ProviderAdapter, kill_tree

#: Parity with ``exec --dangerously-bypass-approvals-and-sandbox``.
_THREAD_POLICY = {"approvalPolicy": "never", "sandbox": "danger-full-access"}


class CodexAppServerInvocation:
    """One node invocation driven over a private ``codex app-server``.

    Exposes the surface the engine and runner use on ``LiveInvocation``:
    ``steer``/``kill``/``finish``/``steered``/``killed`` and a ``plan``
    attribute. The JSONL transcript (responses + notifications) is returned
    from ``finish`` as "stdout" and consumed by ``CodexAdapter.parse``.

    Sequence: initialize -> initialized -> thread/start | thread/resume ->
    turn/start -> (turn/steer on Pause) -> turn/completed -> stdin close.
    """

    plan = None  # engine's marker for a live (kill()-able) invocation handle

    def __init__(self, adapter: "CodexAdapter", node, resume_session: str | None,
                 prompt: str, cwd: str, tee_path: str | None = None):
        self.steered = False
        self.killed = False
        # Live activity surface for the Monitor GUI (see LiveInvocation).
        self._tee = None
        if tee_path:
            try:
                self._tee = open(tee_path, "a", encoding="utf-8", buffering=1)
            except OSError:
                self._tee = None
        self._node = node
        self._resume_session = resume_session
        self._prompt = prompt
        self._cwd = cwd
        self._lines: list[str] = []
        self._stderr: list[str] = []
        self._next_id = 0
        self._responses: dict[int, dict] = {}
        self._resp_events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()          # ids + response registry
        self._stdin_lock = threading.Lock()
        self.thread_id: str | None = resume_session
        self.turn_id: str | None = None
        self._turn_started = threading.Event()
        self._turn_done = threading.Event()
        self._turn_status: str | None = None
        self._fatal: str | None = None
        self.proc = subprocess.Popen(
            [adapter.resolve_exe(), "app-server", *node.extra_args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=adapter.build_env(),
            cwd=cwd,
        )
        self._t_out = threading.Thread(target=self._read_stdout, daemon=True)
        self._t_err = threading.Thread(target=self._read_stderr, daemon=True)
        self._t_out.start()
        self._t_err.start()
        threading.Thread(target=self._handshake, daemon=True).start()

    # -- wire ------------------------------------------------------------- #

    def _send(self, method: str, params: dict | None = None,
              notify: bool = False) -> int | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        rid = None
        if not notify:
            with self._lock:
                self._next_id += 1
                rid = self._next_id
                self._resp_events[rid] = threading.Event()
            msg["id"] = rid
        line = json.dumps(msg)
        with self._stdin_lock:
            stdin = self.proc.stdin
            if stdin is None or stdin.closed:
                raise OSError("app-server stdin closed")
            stdin.write(line + "\n")
            stdin.flush()
        return rid

    def _rpc(self, method: str, params: dict | None, timeout: float) -> dict:
        rid = self._send(method, params)
        event = self._resp_events[rid]
        if not event.wait(timeout=timeout):
            raise TimeoutError(f"codex app-server: no response to {method}")
        with self._lock:
            resp = self._responses.pop(rid, None)
            self._resp_events.pop(rid, None)
        if resp is None:
            raise OSError(f"codex app-server exited during {method}")
        if "error" in resp:
            raise RuntimeError(f"{method} -> {json.dumps(resp['error'])[:500]}")
        return resp.get("result") or {}

    def _read_stdout(self):
        try:
            for line in self.proc.stdout:
                stripped = line.strip()
                if not stripped:
                    continue
                self._lines.append(stripped)
                if self._tee is not None:
                    try:
                        self._tee.write(stripped + "\n")
                    except Exception:
                        self._tee = None
                try:
                    msg = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if "method" not in msg and "id" in msg:  # response
                    with self._lock:
                        rid = msg["id"]
                        self._responses[rid] = msg
                        event = self._resp_events.get(rid)
                    if event is not None:
                        event.set()
                    continue
                method = msg.get("method", "")
                params = msg.get("params") or {}
                if method == "turn/started":
                    turn = params.get("turn") or {}
                    if turn.get("id") and self.turn_id is None:
                        self.turn_id = turn["id"]
                    self._turn_started.set()
                elif method in ("turn/completed", "turn/failed"):
                    turn = params.get("turn") or {}
                    self._turn_status = turn.get("status") or (
                        "completed" if method == "turn/completed" else "failed"
                    )
                    self._turn_done.set()
        except Exception:
            pass
        if self._tee is not None:
            try:
                self._tee.close()
            except Exception:
                pass
        # EOF: unblock every waiter.
        self._turn_started.set()
        self._turn_done.set()
        with self._lock:
            events = list(self._resp_events.values())
        for event in events:
            event.set()

    def _read_stderr(self):
        try:
            for line in self.proc.stderr:
                self._stderr.append(line)
        except Exception:
            pass

    # -- session state machine -------------------------------------------- #

    def _handshake(self):
        node = self._node
        try:
            self._rpc("initialize", {"clientInfo": {
                "name": "graph-workflow", "title": "graph-workflow",
                "version": "1"}}, timeout=60)
            self._send("initialized", notify=True)
            if self._resume_session:
                self._rpc("thread/resume", {
                    "threadId": self._resume_session, "cwd": self._cwd,
                    **_THREAD_POLICY}, timeout=120)
            else:
                thread = self._rpc("thread/start", {
                    "cwd": self._cwd, "model": node.model,
                    **_THREAD_POLICY}, timeout=120)
                self.thread_id = (thread.get("thread") or {}).get("id") or \
                    thread.get("threadId")
                if not self.thread_id:
                    raise RuntimeError(
                        f"thread/start returned no thread id: "
                        f"{json.dumps(thread)[:300]}")
            turn_params = {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": self._prompt}],
                "model": node.model,
            }
            if node.effort:
                turn_params["effort"] = node.effort
            turn = self._rpc("turn/start", turn_params, timeout=120)
            tid = (turn.get("turn") or {}).get("id") or turn.get("turnId")
            if tid and self.turn_id is None:
                self.turn_id = tid
            self._turn_started.set()
        except Exception as exc:
            self._fatal = f"{type(exc).__name__}: {exc}"
            self._turn_started.set()
            self._turn_done.set()

    # -- LiveInvocation surface ------------------------------------------- #

    def steer(self, message: str) -> bool:
        """turn/steer: the message reaches the agent after its current tool
        call. True only if the server accepted it for the live turn."""
        if self.killed or self._turn_done.is_set():
            return False
        if not self._turn_started.wait(timeout=10) or not self.turn_id:
            return False
        try:
            self._rpc("turn/steer", {
                "threadId": self.thread_id,
                "expectedTurnId": self.turn_id,
                "input": [{"type": "text", "text": message}],
            }, timeout=15)
        except Exception:
            return False
        self.steered = True
        return True

    def kill(self):
        self.killed = True
        self._turn_done.set()
        if self.proc.poll() is None:
            kill_tree(self.proc.pid)

    def finish(self, timeout_s: float | None) -> tuple[str, str, int, bool]:
        """Block until the turn ends; returns (stdout, stderr, rc, timed_out).
        Always shuts the server down so the thread's writer lock is released."""
        timed_out = not self._turn_done.wait(timeout=timeout_s)
        if timed_out:
            self.kill()
        with self._stdin_lock:
            stdin = self.proc.stdin
            if stdin is not None and not stdin.closed:
                try:
                    stdin.close()
                except OSError:
                    pass
        try:
            rc = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            kill_tree(self.proc.pid)
            try:
                rc = self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                rc = -1
        self._t_out.join(timeout=5)
        self._t_err.join(timeout=5)
        if self._fatal:
            self._stderr.append(f"codex app-server: {self._fatal}\n")
        if self._turn_status == "completed" and not self.killed and not self._fatal:
            rc = 0  # the turn is the unit of success, not the server's exit
        elif rc == 0:
            rc = 1  # server exited cleanly but the turn never completed
        return ("\n".join(self._lines) + "\n" if self._lines else ""), \
            "".join(self._stderr), rc, timed_out


class CodexAdapter(ProviderAdapter):
    name = "codex"
    executable = "codex"
    #: The runtime cannot choose the session id; it is the app-server thread
    #: id (or, on the exec fallback, the ``thread.started`` event's id).
    provider_assigns_session = True
    #: Invoke via CodexAppServerInvocation (steerable) instead of plans.
    uses_app_server = True

    def start_app_server(self, node, resume_session: str | None, prompt: str,
                         cwd: str, tee_path: str | None = None
                         ) -> CodexAppServerInvocation:
        return CodexAppServerInvocation(self, node, resume_session, prompt, cwd,
                                        tee_path=tee_path)

    # -- legacy ``codex exec`` fallback (GW_CODEX_EXEC=1) ------------------ #

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
        """Handles both wire vocabularies: app-server JSON-RPC lines
        (responses + notifications) and ``exec --json`` events."""
        r = InvocationResult(raw=None)
        messages: list[tuple[str, str]] = []  # (text, phase)
        failure: str | None = None
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
            method = obj.get("method")
            if method is not None:  # app-server notification
                params = obj.get("params") or {}
                if method == "item/completed":
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage":
                        text = item.get("text") or ""
                        if text:
                            messages.append((text, item.get("phase") or ""))
                elif method == "thread/tokenUsage/updated":
                    last = (params.get("tokenUsage") or {}).get("last") or {}
                    if isinstance(last, dict) and last:
                        r.usage = last
                        r.context_size = int(
                            last.get("totalTokens") or self._sum_usage(last))
                elif method == "turn/failed":
                    turn = params.get("turn") or {}
                    failure = json.dumps(
                        turn.get("failure") or turn or params)[:500]
                elif method == "error":
                    failure = failure or str(
                        params.get("message") or json.dumps(params)[:300])
                continue
            if "result" in obj and isinstance(obj.get("result"), dict):
                thread = obj["result"].get("thread")
                if isinstance(thread, dict) and thread.get("id"):
                    r.session_id = thread["id"]
                continue
            etype = obj.get("type", "")
            if etype == "thread.started":
                r.session_id = obj.get("thread_id") or obj.get("threadId")
            elif etype == "item.completed":
                item = obj.get("item") or {}
                if item.get("item_type") == "agent_message" or item.get("type") == "agent_message":
                    text = item.get("text") or item.get("message") or ""
                    if text:
                        messages.append((text, ""))
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
                        messages.append((text, ""))
                if msg.get("type") == "session_configured" and not r.session_id:
                    r.session_id = msg.get("session_id")
        r.raw = events
        finals = [t for t, phase in messages if phase == "final_answer"]
        r.text = finals[-1] if finals else (messages[-1][0] if messages else "")
        if returncode != 0:
            r.ok = False
            r.error = f"codex exited {returncode}: " + (
                failure or (stderr or stdout).strip()[-2000:])
        elif not messages:
            r.ok = False
            r.error = "codex produced no agent message" + (
                f" ({failure})" if failure else "")
        return r
