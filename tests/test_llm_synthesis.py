"""Full context-synthesis tests over multi-layer LLM graphs.

Seed-derived payloads are split across divergent branches at rank 0; the
tests verify — against the trace, machine-checkably — that every layer
captured exactly its slice at arming, carried its branch's payload in its
committed outputs, and that the final node's committed outputs synthesize
both/all branches (join + arithmetic that appear verbatim in no prompt).
These invoke real provider CLIs; expect minutes."""

import time
from pathlib import Path

from conftest import commits, evs, import_graph_module, run_graph_file

# The codex adapter is being reworked concurrently (codex exec -> app-server).
# Transport-ish codex failures get exactly one retry after 60s.
_CODEX_TRANSPORT = ("codex exited", "no agent message", "timed out")


def _green(events, node_ids):
    return all(
        (cs := commits(events, nid)) and cs[-1]["output_state"] is True
        for nid in node_ids
    )


def _codex_flaky(proc, events, codex_nodes):
    for nid in codex_nodes:
        for c in commits(events, nid):
            err = c.get("error") or ""
            if c["output_state"] is not True and any(
                p in err for p in _CODEX_TRANSPORT
            ):
                return True
    return proc.returncode != 0 and "codex" in (proc.stderr or "").lower()


def _run(fname, seed, run_dir, timeout, node_ids, codex_nodes):
    proc, events = run_graph_file(fname, seed, run_dir, timeout=timeout)
    if (proc.returncode != 0 or not _green(events, node_ids)) and _codex_flaky(
        proc, events, codex_nodes
    ):
        time.sleep(60)
        retry_dir = Path(run_dir).parent / "run_retry"
        proc, events = run_graph_file(fname, seed, retry_dir, timeout=timeout)
    return proc, events


def _one_commit_each(events, node_ids):
    out = {}
    for nid in node_ids:
        cs = commits(events, nid)
        assert len(cs) == 1, f"{nid}: expected exactly 1 commit, got {len(cs)}"
        c = cs[0]
        assert c["output_state"] is True, (
            f"{nid} output_state FALSE: outputs={c['outputs']} "
            f"error={c['error']}"
        )
        out[nid] = c
    return out


def _one_ready(events, nid):
    rs = evs(events, "ready", nid)
    assert len(rs) == 1, f"{nid}: expected exactly 1 ready event, got {len(rs)}"
    return rs[0]


def test_t20_three_llm_layers_full_synthesis(run_dir):
    seed = 20257
    exp = import_graph_module("t20_synth_deep").derive(seed)
    proc, events = _run(
        "t20_synth_deep.py", seed, run_dir, timeout=1500,
        node_ids=("S", "A", "B", "M", "F"), codex_nodes=("B", "F"),
    )
    assert proc.returncode == 0, (proc.stderr or "")[-3000:]
    assert evs(events, "run_end")[-1]["reason"] == "quiescent"

    # (c) exactly one commit per node, output_state TRUE on each.
    cs = _one_commit_each(events, ("S", "A", "B", "M", "F"))

    # Layer 0: the script derived exactly the expected payloads.
    assert cs["S"]["outputs"] == exp

    # (b) edge-wise captures at arming; branch isolation: each layer-1 node
    # saw exactly its slice of S's commit and nothing else.
    ra, rb = _one_ready(events, "A"), _one_ready(events, "B")
    assert ra["inputs"] == {"wa": exp["wa"], "na": exp["na"]}
    assert rb["inputs"] == {"wb": exp["wb"], "nb": exp["nb"]}
    assert ra["seq"] > cs["S"]["seq"] and rb["seq"] > cs["S"]["seq"]

    # Layer 1 committed outputs carry each branch's payload (failure here
    # localizes to a branch).
    a, b = cs["A"]["outputs"], cs["B"]["outputs"]
    assert str(a["token_a"]).lower() == exp["wa"].lower(), a
    assert int(a["num_a"]) == exp["na"] + 10, a
    assert str(b["token_b"]).lower() == exp["wb"].lower(), b
    assert int(b["num_b"]) == exp["nb"] + 10, b

    # Layer 2 armed only after both branches committed, capturing both
    # branches' actual committed values across the convergent edge.
    rm = _one_ready(events, "M")
    assert rm["seq"] > max(cs["A"]["seq"], cs["B"]["seq"])
    assert rm["inputs"] == {
        "token_a": a["token_a"], "num_a": a["num_a"],
        "token_b": b["token_b"], "num_b": b["num_b"],
    }
    m = cs["M"]["outputs"]
    assert str(m["combined"]).lower() == f"{exp['wa']}-{exp['wb']}".lower(), m
    assert int(m["total"]) == exp["na"] + exp["nb"] + 20, m

    # Layer 3: the finisher captured M's synthesis and extended it.
    rf = _one_ready(events, "F")
    assert rf["seq"] > cs["M"]["seq"]
    assert rf["inputs"] == {"combined": m["combined"], "total": m["total"]}
    f = cs["F"]["outputs"]
    tag = str(f["final_tag"]).lower()
    # (a) BOTH branch payloads present in the final committed outputs, and
    # the derived combination is exact.
    assert exp["wa"].lower() in tag and exp["wb"].lower() in tag, f
    assert tag == f"{exp['wa']}-{exp['wb']}!".lower(), f
    assert int(f["grand"]) == exp["na"] + exp["nb"] + 20 + 1, f


def test_t21_three_way_convergence_synthesis(run_dir):
    seed = 31415
    exp = import_graph_module("t21_synth_tri").derive(seed)
    proc, events = _run(
        "t21_synth_tri.py", seed, run_dir, timeout=1500,
        node_ids=("S", "P1", "P2", "P3", "Z"), codex_nodes=("P2", "Z"),
    )
    assert proc.returncode == 0, (proc.stderr or "")[-3000:]
    assert evs(events, "run_end")[-1]["reason"] == "quiescent"

    cs = _one_commit_each(events, ("S", "P1", "P2", "P3", "Z"))
    assert cs["S"]["outputs"] == exp

    # Each relay armed with exactly its pair (branch isolation) and its
    # committed outputs carry that branch's payload.
    outs = {}
    for k in ("1", "2", "3"):
        r = _one_ready(events, f"P{k}")
        assert r["inputs"] == {f"w{k}": exp[f"w{k}"], f"n{k}": exp[f"n{k}"]}
        assert r["seq"] > cs["S"]["seq"]
        o = cs[f"P{k}"]["outputs"]
        assert str(o[f"t{k}"]).lower() == exp[f"w{k}"].lower(), o
        assert int(o[f"m{k}"]) == 2 * exp[f"n{k}"], o
        outs[k] = o

    # Z armed only after all three relays committed, capturing all six
    # relayed values across the single 3-input convergent edge.
    rz = _one_ready(events, "Z")
    assert rz["seq"] > max(cs[f"P{k}"]["seq"] for k in ("1", "2", "3"))
    assert rz["inputs"] == {
        "t1": outs["1"]["t1"], "m1": outs["1"]["m1"],
        "t2": outs["2"]["t2"], "m2": outs["2"]["m2"],
        "t3": outs["3"]["t3"], "m3": outs["3"]["m3"],
    }

    z = cs["Z"]["outputs"]
    combined = str(z["combined"]).lower()
    for k in ("1", "2", "3"):
        assert exp[f"w{k}"].lower() in combined, z
    assert combined == (
        f"{exp['w1']}-{exp['w2']}-{exp['w3']}".lower()
    ), z
    assert int(z["sum"]) == 2 * (exp["n1"] + exp["n2"] + exp["n3"]), z
