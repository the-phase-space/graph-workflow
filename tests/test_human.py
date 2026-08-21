"""Human node tests: runtime-level bypass (GW_HUMAN_AUTO) and the real Tauri
shell exe answering headlessly (GW_HUMAN_SHELL_AUTO)."""

import json
from pathlib import Path

import pytest

from conftest import PROJECT, commits, run_graph_file

SHELL_EXE = PROJECT / "human-shell" / "bin" / "human-shell.exe"


def _mapping_file(tmp_path) -> Path:
    path = tmp_path / "auto.json"
    path.write_text(json.dumps({"H": {"approval": "yes"}}), encoding="utf-8")
    return path


def _assert_approved(proc, events):
    assert proc.returncode == 0, (proc.stderr or "")[-2000:]
    [ch] = commits(events, "H")
    assert ch["outputs"] == {"approval": "yes"} and ch["output_state"] is True
    [cv] = commits(events, "V")
    assert cv["output_state"] is True


def test_human_runtime_bypass(run_dir, tmp_path):
    proc, events = run_graph_file(
        "t13_human.py", 7, run_dir,
        env_extra={"GW_HUMAN_AUTO": str(_mapping_file(tmp_path))},
    )
    _assert_approved(proc, events)


@pytest.mark.skipif(not SHELL_EXE.exists(), reason="human-shell.exe not built")
def test_human_shell_exe_headless(run_dir, tmp_path):
    proc, events = run_graph_file(
        "t13_human.py", 7, run_dir,
        env_extra={"GW_HUMAN_SHELL_AUTO": str(_mapping_file(tmp_path))},
        timeout=240,
    )
    _assert_approved(proc, events)
    # The real exe ran: it wrote the response file next to the request.
    responses = list((Path(run_dir) / "human").glob("H-*.response.json"))
    assert responses, "shell response file missing"
