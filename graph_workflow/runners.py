"""Node runners: one invocation of a script, LLM, or human node.

A runner executes the node's function and returns raw outputs plus metrics.
It never commits — the engine evaluates the output expression (the commit)
when the invocation ends. A failed invocation returns an error and whatever
outputs it managed to define; undefined outputs make the default output
expression FALSE, so failure surfaces as a FALSE output state, not a crash.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from string import Template

from .model import UNDEFINED, HumanNode, LLMNode, ScriptNode
from .providers import LiveInvocation, get_adapter, invoke
from .trace import json_safe

_UUID_NS = uuid.uuid5(uuid.NAMESPACE_URL, "graph-workflow")


def session_uuid(run_id: str, node_id: str, invocation: int) -> str:
    """Deterministic per-invocation session UUID (run_id carries the start
    timestamp, so ids are unique across runs but reproducible within one)."""
    return str(uuid.uuid5(_UUID_NS, f"{run_id}:{node_id}:{invocation}"))


# --------------------------------------------------------------------------- #
# Inputs view
# --------------------------------------------------------------------------- #


class Inputs:
    """Attribute + mapping access to a node's captured inputs. Declared but
    unsupplied inputs read as UNDEFINED."""

    def __init__(self, declared: dict, values: dict):
        self._declared = dict(declared)
        self._values = dict(values)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._declared or name in self._values:
            return self._values.get(name, UNDEFINED)
        raise AttributeError(f"no declared input {name!r}")

    def __getitem__(self, name):
        return self.__getattr__(name)

    def get(self, name, default=None):
        value = self._values.get(name, UNDEFINED)
        return default if value is UNDEFINED else value

    def keys(self):
        return self._values.keys()

    def items(self):
        return self._values.items()

    def __contains__(self, name):
        return name in self._values

    def __iter__(self):
        return iter(self._values)

    def __repr__(self):
        return f"Inputs({self._values!r})"


def render_prompt(prompt, inputs: Inputs) -> str:
    """A prompt is a ``string.Template`` string ($name placeholders substitute
    captured inputs) or a callable ``prompt(inputs) -> str``."""
    if callable(prompt):
        return str(prompt(inputs))
    mapping = {}
    for name, value in inputs.items():
        if value is UNDEFINED:
            continue
        mapping[name] = value if isinstance(value, str) else json.dumps(json_safe(value))
    return Template(str(prompt)).safe_substitute(mapping)


# --------------------------------------------------------------------------- #
# Output extraction
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def extract_json_object(text: str) -> dict | None:
    """Find the last JSON object in a reply: fenced blocks first (last wins),
    then the last balanced ``{...}`` span in the raw text."""
    for block in reversed(_FENCE_RE.findall(text)):
        try:
            obj = json.loads(block)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    end = len(text)
    while True:
        close = text.rfind("}", 0, end)
        if close < 0:
            return None
        depth = 0
        i = close
        start = -1
        in_str = False
        while i >= 0:
            ch = text[i]
            if ch == '"' and (i == 0 or text[i - 1] != "\\"):
                in_str = not in_str
            elif not in_str:
                if ch == "}":
                    depth += 1
                elif ch == "{":
                    depth -= 1
                    if depth == 0:
                        start = i
                        break
            i -= 1
        if start >= 0:
            try:
                obj = json.loads(text[start : close + 1])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
        end = close


def _map_declared_outputs(node, data: dict, warn) -> dict:
    out = {}
    for key, value in data.items():
        if key in node.outputs:
            out[key] = value
        else:
            warn(f"node {node.id}: extracted output {key!r} is not declared; ignored")
    return out


# --------------------------------------------------------------------------- #
# Script
# --------------------------------------------------------------------------- #


def run_script(node: ScriptNode, inputs: Inputs, warn) -> tuple[dict, dict, str | None]:
    t0 = time.monotonic()
    outputs: dict = {}
    error = None
    try:
        result = node.fn(inputs)
        if isinstance(result, dict):
            outputs = _map_declared_outputs(node, result, warn)
        elif result is None:
            outputs = {}
        elif len(node.outputs) == 1:
            outputs = {next(iter(node.outputs)): result}
        else:
            error = (
                f"script node {node.id}: fn returned {type(result).__name__}; expected a "
                f"dict over declared outputs (or a bare value with exactly one declared output)"
            )
    except Exception as exc:
        error = f"script node {node.id}: {type(exc).__name__}: {exc}"
    metrics = {"duration_s": round(time.monotonic() - t0, 3)}
    return outputs, metrics, error


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #


def _write_prompt_file(run_dir: Path, node_id: str, invocation: int, tag: str,
                       prompt: str) -> Path:
    pdir = Path(run_dir) / "prompts"
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{node_id}-{invocation:03d}{tag}.txt"
    path.write_text(prompt, encoding="utf-8")
    return path


def _llm_outputs(node: LLMNode, result, warn) -> dict:
    outputs = {}
    if "response" in node.outputs:
        outputs["response"] = result.text
    extra_declared = [name for name in node.outputs if name != "response"]
    if node.parse is not None:
        try:
            parsed = node.parse(result.text, result.raw)
            if isinstance(parsed, dict):
                outputs.update(_map_declared_outputs(node, parsed, warn))
            else:
                warn(f"node {node.id}: parse() returned {type(parsed).__name__}, expected dict")
        except Exception as exc:
            warn(f"node {node.id}: parse() raised {type(exc).__name__}: {exc}")
    elif extra_declared:
        data = extract_json_object(result.text)
        if data:
            outputs.update(
                {k: v for k, v in _map_declared_outputs(node, data, warn).items()
                 if k in extra_declared}
            )
    return outputs


def _invoke_llm(node: LLMNode, prompt: str, *, resume_session: str | None,
                session_id: str, run_dir, invocation: int, tag: str,
                register_proc, timeout_s):
    adapter = get_adapter(node.provider)
    cwd = node.cwd or os.getcwd()
    prompt_path = str(_write_prompt_file(run_dir, node.id, invocation, tag, prompt))
    # The session's event stream is teed here live — the Monitor GUI's
    # per-node activity log tails this file.
    sdir = Path(run_dir) / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    tee_path = str(sdir / f"{node.id}-{invocation:03d}{tag}.jsonl")
    if getattr(adapter, "uses_app_server", False) and not os.environ.get("GW_CODEX_EXEC"):
        handle = adapter.start_app_server(node, resume_session, prompt, cwd,
                                          tee_path=tee_path)
    else:
        pfile = prompt_path if adapter.needs_prompt_file else None
        if resume_session:
            plan = adapter.plan_resume(node, resume_session, cwd, prompt, pfile)
        else:
            plan = adapter.plan_new(node, session_id, cwd, prompt, pfile)
        handle = LiveInvocation(plan, adapter.build_env(), cwd, tee_path=tee_path)
    if register_proc is not None:
        register_proc(handle)
    try:
        stdout, stderr, rc, timed_out = handle.finish(timeout_s)
    finally:
        if register_proc is not None:
            register_proc(None)
    result = adapter.parse(stdout, stderr, rc)
    if timed_out:
        result.ok = False
        result.error = f"timed out after {timeout_s}s"
    if not result.session_id:
        result.session_id = None if getattr(adapter, "provider_assigns_session", False) else session_id
    return result, handle


def run_llm(node: LLMNode, inputs: Inputs, *, run_id: str, run_dir, invocation: int,
            register_proc, interrupted, warn,
            resume_session: str | None = None, resume_message: str | None = None,
            pause_text: str | None = None):
    """One CLI invocation of an LLM node. Returns
    (status, outputs, metrics, error, ack_text) with status "ok",
    "interrupted" (killed mid-turn; halt message still owed to the session),
    or "interrupted_acked" (the halt message was steered into the live
    session and ``ack_text`` is the model's reply ending its following turn).

    With ``resume_session`` set this is the Resume path completing a paused
    invocation: the session-bound resume form carries ``resume_message`` and
    the reply is the invocation's response; declared outputs missing from it
    are also sought in ``pause_text`` (work the model emitted before the
    pause landed).
    """
    t0 = time.monotonic()
    if resume_session:
        prompt = resume_message or "continue"
        tag = "-resume"
    else:
        prompt = render_prompt(node.prompt, inputs)
        tag = ""
    session_id = resume_session or session_uuid(run_id, node.id, invocation)
    result, handle = _invoke_llm(
        node, prompt,
        resume_session=resume_session, session_id=session_id,
        run_dir=run_dir, invocation=invocation, tag=tag,
        register_proc=register_proc, timeout_s=node.timeout_s,
    )
    metrics = {
        "duration_s": round(time.monotonic() - t0, 3),
        "session_id": result.session_id,
        "context_size": result.context_size,
        "usage": result.usage,
    }
    if interrupted():
        if handle.steered:
            return "interrupted_acked", {}, metrics, None, result.text
        if not (result.ok and not handle.killed):
            return "interrupted", {}, metrics, None, None
        # The invocation completed before the pause could act on it: commit.
    outputs = _llm_outputs(node, result, warn) if result.ok else {}
    if resume_session and result.ok and pause_text:
        missing = [n for n in node.outputs if n != "response" and n not in outputs]
        if missing:
            data = extract_json_object(pause_text)
            if data:
                outputs.update(
                    {k: v for k, v in _map_declared_outputs(node, data, warn).items()
                     if k in missing}
                )
    return "ok", outputs, metrics, result.error, None


def send_llm_message(node: LLMNode, session_id: str, message: str, *,
                     run_dir, invocation: int, tag: str, register_proc,
                     timeout_s: float | None = 600.0):
    """Session-bound one-turn message (the Pause halt message, when it could
    not be steered into the live session). Returns (text, error)."""
    result, _ = _invoke_llm(
        node, message,
        resume_session=session_id, session_id=session_id,
        run_dir=run_dir, invocation=invocation, tag=tag,
        register_proc=register_proc, timeout_s=timeout_s,
    )
    return result.text, result.error


# --------------------------------------------------------------------------- #
# Human
# --------------------------------------------------------------------------- #


def _default_shell_path() -> Path:
    exe = "human-shell.exe" if os.name == "nt" else "human-shell"
    return Path(__file__).resolve().parent.parent / "human-shell" / "bin" / exe


def _coerce_human_outputs(node: HumanNode, data: dict, warn) -> dict:
    outputs = {}
    for name, value in _map_declared_outputs(node, data, warn).items():
        declared = node.outputs[name]
        if declared in (str, "str") or not isinstance(value, str):
            outputs[name] = value
        else:
            try:
                outputs[name] = json.loads(value)
            except json.JSONDecodeError:
                outputs[name] = value
    return outputs


def run_human(node: HumanNode, inputs: Inputs, *, run_id: str, run_dir,
              invocation: int, register_proc, warn) -> tuple[dict, dict, str | None]:
    t0 = time.monotonic()

    def done(outputs, error=None):
        return outputs, {"duration_s": round(time.monotonic() - t0, 3)}, error

    # Test hook: bypass the shell entirely with canned responses.
    auto = os.environ.get("GW_HUMAN_AUTO")
    if auto:
        try:
            mapping = json.loads(Path(auto).read_text(encoding="utf-8"))
            data = mapping.get(node.id, mapping.get("*"))
            if data is None:
                return done({}, f"human node {node.id}: no entry in GW_HUMAN_AUTO mapping")
            return done(_coerce_human_outputs(node, data, warn))
        except Exception as exc:
            return done({}, f"human node {node.id}: GW_HUMAN_AUTO failed: {exc}")

    exe = node.shell_cmd or os.environ.get("GW_HUMAN_SHELL") or str(_default_shell_path())
    if not Path(exe).exists():
        return done({}, f"human node {node.id}: human-input shell not found at {exe}")

    hdir = Path(run_dir) / "human"
    hdir.mkdir(parents=True, exist_ok=True)
    request_path = hdir / f"{node.id}-{invocation:03d}.request.json"
    response_path = hdir / f"{node.id}-{invocation:03d}.response.json"
    request_path.write_text(
        json.dumps(
            {
                "node": node.id,
                "run_id": run_id,
                "invocation": invocation,
                "prompt": render_prompt(node.prompt, inputs),
                "inputs": {k: json_safe(v) for k, v in inputs.items()},
                "outputs": {
                    k: (t if isinstance(t, str) else getattr(t, "__name__", repr(t)))
                    for k, t in node.outputs.items()
                },
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    argv = [exe, "--request", str(request_path), "--response", str(response_path)]
    shell_auto = os.environ.get("GW_HUMAN_SHELL_AUTO")
    if shell_auto:
        argv += ["--auto-response", shell_auto]
    stdout, stderr, rc, timed_out = invoke(
        argv, None, dict(os.environ), None, node.timeout_s, register_proc
    )
    if timed_out:
        return done({}, f"human node {node.id}: timed out after {node.timeout_s}s")
    if not response_path.exists():
        detail = (stderr or stdout).strip()[-500:]
        return done(
            {},
            f"human node {node.id}: shell exited {rc} without a response"
            + (f" ({detail})" if detail else ""),
        )
    try:
        data = json.loads(response_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return done({}, f"human node {node.id}: unreadable response file: {exc}")
    if isinstance(data, dict) and isinstance(data.get("outputs"), dict):
        data = data["outputs"]
    if not isinstance(data, dict):
        return done({}, f"human node {node.id}: response is not an object")
    return done(_coerce_human_outputs(node, data, warn))
