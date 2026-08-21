"""Real LLM node tests — one per provider, all three concurrently, and live
Pause/Resume against a real claude session. These invoke actual provider
CLIs; expect minutes, not seconds."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import GRAPHS, PROJECT, commits, evs, load_trace, run_graph_file


def _assert_echo_graph(proc, events):
    assert proc.returncode == 0, (proc.stderr or "")[-3000:]
    lc = commits(events, "L")
    assert lc, "LLM node never committed"
    last = lc[-1]
    assert last["output_state"] is True, (
        f"LLM commit incomplete: outputs={last['outputs']} error={last['error']}"
    )
    vc = commits(events, "V")
    assert vc and vc[-1]["output_state"] is True, f"verifier failed: {vc}"


@pytest.mark.parametrize(
    "fname",
    ["t09_llm_claude.py", "t10_llm_codex.py", "t11_llm_grok.py"],
)
def test_provider_echo(fname, run_dir):
    proc, events = run_graph_file(fname, 7, run_dir, timeout=420)
    _assert_echo_graph(proc, events)


def test_all_providers_concurrent(run_dir):
    proc, events = run_graph_file("t12_llm_all.py", 7, run_dir, timeout=700)
    assert proc.returncode == 0, (proc.stderr or "")[-3000:]
    for nid in ("Lc", "Lx", "Lg"):
        nc = commits(events, nid)
        assert nc and nc[-1]["output_state"] is True, (
            f"{nid} failed: {nc and nc[-1]}"
        )
    vc = commits(events, "V")
    assert vc and vc[-1]["output_state"] is True


def _control(run_dir, command):
    (Path(run_dir) / "control.json").write_text(
        json.dumps({"command": command}), encoding="utf-8"
    )


def _wait(predicate, timeout, interval=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.mark.parametrize(
    "fname", ["t14_llm_pause.py", "t15_llm_pause_codex.py"]
)
def test_llm_pause_resume(fname, run_dir):
    """Pause mid-turn: the runtime steers the halt message into the live
    session (claude via stream-json stdin, codex via app-server turn/steer),
    the model ends its following turn (the ack) and the run reaches
    `paused`; Resume re-enters the session with `continue` and the reply
    completes invocation 1."""
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT)
    env["GW_SEED"] = "7"
    env["GW_RUN_DIR"] = str(run_dir)
    proc = subprocess.Popen(
        [sys.executable, str(GRAPHS / fname)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(run_dir),
    )
    try:
        assert _wait(lambda: evs(load_trace(run_dir), "start", "L"), 60), \
            "LLM node never started"
        time.sleep(1.5)  # let the CLI get properly mid-turn
        _control(run_dir, "pause")
        assert _wait(lambda: evs(load_trace(run_dir), "paused"), 180), \
            "never reached paused"
        events = load_trace(run_dir)
        assert evs(events, "node_interrupted", "L")
        _control(run_dir, "resume")
        assert _wait(lambda: evs(load_trace(run_dir), "run_end"), 300), \
            "run never ended after resume"
    finally:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    events = load_trace(run_dir)
    assert evs(events, "resumed")
    interrupted = evs(events, "node_interrupted", "L")
    assert interrupted and interrupted[0].get("steered") is True, (
        f"halt was not steered into the live session: {interrupted}"
    )
    assert evs(events, "node_pause_acked"), "no pause ack recorded"
    lc = commits(events, "L")
    assert lc, "LLM node never committed after resume"
    assert lc[0]["invocation"] == 1, "resume must complete the open invocation"
    resumed_starts = [e for e in evs(events, "start", "L") if e.get("resumed")]
    assert resumed_starts, "no resumed start recorded"
    assert evs(events, "run_end")[0]["reason"] == "quiescent"
