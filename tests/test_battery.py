"""Script-node battery: single node → chain → convergent → divergent →
cyclic → combined, plus data plane, output-expression semantics, seeded
determinism, and validation negatives."""

import json

from conftest import commits, evs, first_seq, last_event, run_graph_file


def _reason(proc):
    for line in proc.stdout.splitlines():
        if line.startswith("REASON:"):
            return line.split(":", 1)[1].strip()
    return None


def test_t01_single(run_dir):
    proc, events = run_graph_file("t01_single.py", 7, run_dir)
    assert proc.returncode == 0, proc.stderr
    assert _reason(proc) == "quiescent"
    [c] = commits(events, "A")
    assert c["outputs"] == {"x": 42} and c["output_state"] is True
    assert last_event(events)["event"] == "run_end"


def test_t02_chain(run_dir):
    proc, events = run_graph_file("t02_chain.py", 7, run_dir)
    assert proc.returncode == 0, proc.stderr
    [ca] = commits(events, "A")
    [cb] = commits(events, "B")
    assert cb["outputs"] == {"y": 42}
    # B armed strictly after A committed, with A's output captured at arming.
    ready_b = evs(events, "ready", "B")[0]
    assert ready_b["seq"] > ca["seq"]
    assert ready_b["inputs"] == {"x": 21}


def test_t03_convergent_identity(run_dir):
    proc, events = run_graph_file("t03_convergent.py", 7, run_dir)
    assert proc.returncode == 0, proc.stderr
    assert _reason(proc) == "quiescent"
    [ca] = commits(events, "A")
    [cb] = commits(events, "B")
    both = max(ca["seq"], cb["seq"])
    # One N-input edge (C) and N separate 1:1 edges (D) behave identically:
    # ready only after both inputs committed, both inputs delivered.
    for nid, out_key in (("C", "sum_c"), ("D", "sum_d")):
        [ready] = evs(events, "ready", nid)
        assert ready["seq"] > both, f"{nid} armed before both inputs committed"
        assert ready["inputs"] == {"a": 10, "b": 7}
        [c] = commits(events, nid)
        assert c["outputs"] == {out_key: 17} and c["output_state"] is True


def test_t04_divergent_rank_order(run_dir):
    proc, events = run_graph_file("t04_divergent.py", 7, run_dir)
    assert proc.returncode == 0, proc.stderr
    [cb] = commits(events, "B")
    [cc] = commits(events, "C")
    assert cb["outputs"] == {"y": 12} and cc["outputs"] == {"z": 18}
    # script semaphore = 1: B (rank 1) must fully precede C (rank 2)
    assert first_seq(events, "start", "B") < cb["seq"] < first_seq(events, "start", "C")


def test_t05_cycle(run_dir):
    proc, events = run_graph_file("t05_cyclic.py", 7, run_dir)
    assert proc.returncode == 0, proc.stderr
    assert _reason(proc) == "quiescent"
    a_commits = commits(events, "A")
    b_commits = commits(events, "B")
    assert [c["outputs"]["n"] for c in a_commits] == [1, 3, 5]
    assert [c["outputs"]["n"] for c in b_commits] == [2, 4, 6]
    assert last_event(events)["event"] == "run_end"


def _commit_signature(events):
    return [
        (e["node"], e["invocation"], json.dumps(e["outputs"], sort_keys=True),
         e["output_state"])
        for e in commits(events)
    ]


def _draw_signature(events):
    return [(e["gate"], e["index"], e["value"]) for e in evs(events, "draw")]


def test_t06_combo_and_determinism(tmp_path):
    d1, d2, d3 = (tmp_path / n for n in ("r1", "r2", "r3"))
    p1, e1 = run_graph_file("t06_combo.py", 11, d1)
    p2, e2 = run_graph_file("t06_combo.py", 11, d2)
    p3, e3 = run_graph_file("t06_combo.py", 12, d3)
    assert p1.returncode == 0, p1.stderr
    assert _reason(p1) == "quiescent"
    m_inv = [c["invocation"] for c in commits(e1, "M")]
    assert m_inv == [1, 2, 3, 4]
    assert [c["invocation"] for c in commits(e1, "Y")] == [1, 2, 3, 4]
    # X fired exactly on the seed-determined draws under p_invocation = 0.5.
    x_gate_draws = [
        e for e in evs(e1, "draw")
        if e["gate"] == "e_div::out::X" and 1 <= e["index"] <= 4
    ]
    assert len(x_gate_draws) == 4
    expected_x = sum(1 for e in x_gate_draws if e["value"] <= 0.5)
    assert len(commits(e1, "X")) == expected_x
    # Determinism over the seed: identical commit and draw sequences.
    assert _commit_signature(e1) == _commit_signature(e2)
    assert _draw_signature(e1) == _draw_signature(e2)
    # A different seed draws differently.
    assert _draw_signature(e1) != _draw_signature(e3)


def test_t07_dataplane(run_dir):
    proc, events = run_graph_file("t07_dataplane.py", 7, run_dir)
    assert proc.returncode == 0, proc.stderr
    [cb] = commits(events, "B")
    [cc] = commits(events, "C")
    assert cb["outputs"] == {"tag": "B:fast:hello"}
    assert cc["outputs"] == {"tag": "C:3"}
    # Two suppliers of `tag`: the later commit (C, rank 2 after B) wins.
    [cd] = commits(events, "D")
    assert cd["outputs"] == {"final": "C:3"}


def test_t08_output_expression(run_dir):
    proc, events = run_graph_file("t08_output_expr.py", 7, run_dir)
    assert proc.returncode == 0, proc.stderr
    assert _reason(proc) == "quiescent"
    [ca] = commits(events, "A")
    assert ca["output_state"] is False and ca["outputs"] == {"x": 3}
    [ce] = commits(events, "E")
    assert ce["output_state"] is False
    assert "deliberate failure" in (ce["error"] or "")
    assert commits(events, "B") == [] and commits(events, "F") == []
    assert evs(events, "start", "B") == [] and evs(events, "start", "F") == []


def test_t99_validation(run_dir):
    proc, _ = run_graph_file("t99_invalid.py", 1, run_dir)
    assert proc.returncode == 0, proc.stderr
    assert "VALIDATION_ERROR_OK" in proc.stdout
    problems = [l for l in proc.stdout.splitlines() if l.startswith(" -")]
    text = "\n".join(problems)
    assert len(problems) >= 5
    for needle in ("rank", "primary", "nowhere", "invalid Python", "upstream"):
        assert needle in text, f"missing validation problem about {needle!r}"
