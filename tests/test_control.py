"""Pause/Resume (in-process, script nodes) and resume-from-disk replay."""

import json
import threading
import time

from conftest import commits, evs, import_graph_module, load_trace, run_graph_file


def _wait(predicate, timeout=15.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_pause_resume_script(run_dir):
    """Pause lets running script invocations complete, blocks new starts,
    reaches `paused`; Resume lets the schedule continue to quiescence."""
    from graph_workflow import Edge, Graph, Runtime, ScriptNode, Semaphores

    def slow(i):
        time.sleep(1.2)
        return {"x": 1}

    g = Graph("pause-script", semaphores=Semaphores(script=1, llm=1, human=1))
    A = ScriptNode("A", rank=0, fn=slow, outputs={"x": int})
    B = ScriptNode("B", rank=1, fn=lambda i: {"y": i.x + 1}, inputs={"x": int},
                   outputs={"y": int})
    g.add_nodes(A, B)
    g.add_edges(
        Edge("gen_A", inputs=[g.generator], outputs=[A]),
        Edge("e_AB", inputs=[A], outputs=[B]),
    )
    rt = Runtime(g, seed=3, namespace={}, run_dir=run_dir, console=False)
    box = {}
    th = threading.Thread(target=lambda: box.update(result=rt.run()), daemon=True)
    th.start()
    assert _wait(lambda: evs(load_trace(run_dir), "start", "A")), "A never started"
    rt.pause()
    assert _wait(lambda: evs(load_trace(run_dir), "paused")), "never reached paused"
    events = load_trace(run_dir)
    assert commits(events, "A"), "running script must complete and commit under pause"
    assert not evs(events, "start", "B"), "no new starts while pausing/paused"
    rt.resume()
    th.join(30)
    assert "result" in box and box["result"].reason == "quiescent"
    events = load_trace(run_dir)
    assert commits(events, "B")[0]["outputs"] == {"y": 2}
    order = [
        evs(events, "start", "A")[0]["seq"],
        evs(events, "pause_requested")[0]["seq"],
        commits(events, "A")[0]["seq"],
        evs(events, "paused")[0]["seq"],
        evs(events, "resumed")[0]["seq"],
        evs(events, "start", "B")[0]["seq"],
    ]
    assert order == sorted(order), f"event order wrong: {order}"


def test_resume_from_disk(tmp_path):
    """Truncate a finished run's trace mid-run (simulated crash) and resume:
    the replayed run must complete with exactly the same final state."""
    from graph_workflow import resume_run

    full_dir = tmp_path / "full"
    proc, full_events = run_graph_file("t05_cyclic.py", 7, full_dir)
    assert proc.returncode == 0, proc.stderr
    full_commits = [(e["node"], e["invocation"], e["outputs"]) for e in commits(full_events)]
    assert len(full_commits) == 6

    # Cut immediately after the third commit — the dependent re-evaluation
    # events after it are lost, like a process killed mid-pipeline.
    third_commit_seq = commits(full_events)[2]["seq"]
    kept = [e for e in full_events if e["seq"] <= third_commit_seq]
    crash_dir = tmp_path / "crashed"
    crash_dir.mkdir()
    with open(crash_dir / "trace.jsonl", "w", encoding="utf-8") as fh:
        for e in kept:
            fh.write(json.dumps(e) + "\n")

    mod = import_graph_module("t05_cyclic")
    result = resume_run(mod.g, crash_dir / "trace.jsonl", namespace=vars(mod),
                        console=False)
    assert result.reason == "quiescent"
    assert result.states["A"].outputs == {"n": 5}
    assert result.states["B"].outputs == {"n": 6}
    resumed_events = load_trace(crash_dir)
    assert evs(resumed_events, "resumed_from_disk")
    resumed_commits = [(e["node"], e["invocation"], e["outputs"]) for e in commits(resumed_events)]
    assert resumed_commits == full_commits, "resumed run diverged from the original"


def test_resume_refuses_ended_run(tmp_path):
    from graph_workflow import TraceReplayError, resume_run

    d = tmp_path / "done"
    proc, _ = run_graph_file("t01_single.py", 7, d)
    assert proc.returncode == 0, proc.stderr
    mod = import_graph_module("t01_single")
    try:
        resume_run(mod.g, d / "trace.jsonl", namespace=vars(mod), console=False)
        raise AssertionError("resume of an ended run must fail")
    except TraceReplayError:
        pass
