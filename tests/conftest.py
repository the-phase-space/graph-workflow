"""Harness for running executable graph definitions and asserting on traces."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
GRAPHS = PROJECT / "tests" / "graphs"

if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


def run_graph_file(name, seed, run_dir, env_extra=None, timeout=180):
    """Execute a graph definition file the way a user would: as a process.
    Returns (completed_process, trace_events)."""
    run_dir = Path(run_dir)
    # The subprocess is launched with cwd=run_dir, and the runtime writes its
    # trace there: the directory must exist before Popen (a nonexistent cwd
    # raises NotADirectoryError on Windows before Python even starts).
    run_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT)
    env["GW_SEED"] = str(seed)
    env["GW_RUN_DIR"] = str(run_dir)
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(GRAPHS / name)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(run_dir),
        timeout=timeout,
    )
    return proc, load_trace(run_dir)


def load_trace(run_dir) -> list[dict]:
    path = Path(run_dir) / "trace.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def import_graph_module(name: str):
    """Import a graph definition module (without running it) so in-process
    tests can reach its Graph object and namespace."""
    path = GRAPHS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- trace query helpers ------------------------------------------------- #


def evs(events, etype, node=None):
    return [
        e for e in events
        if e["event"] == etype and (node is None or e.get("node") == node)
    ]


def commits(events, node=None):
    return evs(events, "commit", node)


def first_seq(events, etype, node=None):
    found = evs(events, etype, node)
    return found[0]["seq"] if found else None


def last_event(events):
    return events[-1] if events else None


@pytest.fixture()
def run_dir(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    return d
