"""GUI backend contract round-trips: helper preservation and fail-safe saves.

Regression battery for the save-pipeline data loss where converting an
unstructured (handwritten) definition to the structured format dropped every
module-level helper, emitting a gw:graph section whose bare callable
references (``prompt=fable_prompt``) raised NameError — and left the broken
file on disk.
"""

from __future__ import annotations

import textwrap

import pytest

from graph_workflow import gui

UNSTRUCTURED = textwrap.dedent(
    '''
    """Handwritten ratify loop used as a conversion fixture."""

    import textwrap

    from graph_workflow import Edge, Graph, HumanNode, LLMNode, Semaphores, run

    ROUND_CAP = 5


    def wants_rework(decision):
        return decision == 2


    def fable_prompt(inputs):
        comments = inputs.get("comments")
        base = "Do the thing."
        if comments:
            return base + "\\nRework comments:\\n" + comments
        return base


    def me_prompt(inputs):
        return "Review:\\n" + str(inputs.get("response", ""))


    g = Graph("fixture-loop", semaphores=Semaphores(script=1, llm=1, human=1))
    fable = LLMNode(
        "fable", rank=1, provider="claude", model="fable", effort="low",
        prompt=fable_prompt,
        inputs={"decision": int, "comments": str},
        outputs={"response": str},
        gate="gen_f or (e_back and wants_rework(me.decision) and fable.invocations < ROUND_CAP)",
    )
    me = HumanNode(
        "me", rank=2, prompt=me_prompt,
        inputs={"response": str},
        outputs={"decision": int, "comments": str},
        gate="e_fwd and me.invocations < 8",
    )
    g.add_nodes(fable, me)
    g.add_edges(
        Edge("gen_f", inputs=[g.generator], outputs=[fable]),
        Edge("e_fwd", inputs=[fable], outputs=[me]),
        Edge("e_back", inputs=[me], outputs=[fable]),
    )

    if __name__ == "__main__":
        run(g)
    '''
).strip("\n")


@pytest.fixture()
def unstructured_def(tmp_path):
    path = tmp_path / "handwritten.py"
    path.write_text(UNSTRUCTURED, encoding="utf-8", newline="\n")
    return path


def node_by_id(contract: dict, nid: str) -> dict:
    return next(n for n in contract["graph"]["nodes"] if n["id"] == nid)


def test_unstructured_load_carves_helpers(unstructured_def):
    c = gui.load_contract(unstructured_def)
    assert c["ok"] and not c["structured"]
    helpers = c["helpers"]
    for needed in (
        "import textwrap",
        "ROUND_CAP = 5",
        "def wants_rework",
        "def fable_prompt",
        "def me_prompt",
    ):
        assert needed in helpers
    # The pieces the structured regeneration re-emits stay out of helpers.
    for excluded in ("Graph(", "LLMNode(", "add_nodes", "add_edges", "__main__",
                     "from graph_workflow"):
        assert excluded not in helpers
    assert node_by_id(c, "fable")["prompt_ref"] == "fable_prompt"
    assert node_by_id(c, "fable")["prompt"] is None
    assert c["warnings"] == []


def test_save_round_trip_preserves_helpers(unstructured_def, tmp_path):
    c = gui.load_contract(unstructured_def)
    target = tmp_path / "structured.py"
    saved = gui.save_contract(target, c, force=True)
    assert saved["ok"] and saved["structured"]
    assert saved["path"] == str(target.resolve())
    text = target.read_text(encoding="utf-8")
    assert "def fable_prompt" in text and "def wants_rework" in text
    assert "prompt=fable_prompt" in text and "prompt=me_prompt" in text
    # The regenerated definition executes and re-loads identically.
    again = gui.load_contract(target)
    assert node_by_id(again, "fable")["prompt_ref"] == "fable_prompt"
    assert again["warnings"] == []
    # Structured saves are a fixed point: regenerating the reloaded contract
    # reproduces the file byte for byte.
    assert gui.generate(again) == text


def test_broken_save_refused_and_disk_untouched(unstructured_def, tmp_path):
    c = gui.load_contract(unstructured_def)
    target = tmp_path / "structured.py"
    gui.save_contract(target, c, force=True)
    before = target.read_text(encoding="utf-8")

    c2 = gui.load_contract(target)
    c2["helpers"] = ""  # the historic data-loss condition
    with pytest.raises(RuntimeError, match="left untouched"):
        gui.save_contract(target, c2, force=True)
    assert target.read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob("*.gw-tmp"))


def test_literal_prompt_overrides_callable(unstructured_def, tmp_path):
    c = gui.load_contract(unstructured_def)
    fable = node_by_id(c, "fable")
    fable["prompt"] = "Say hi to $comments"  # typed override; prompt_ref intact
    target = tmp_path / "structured.py"
    gui.save_contract(target, c, force=True)
    text = target.read_text(encoding="utf-8")
    assert "prompt='Say hi to $comments'" in text
    again = gui.load_contract(target)
    assert node_by_id(again, "fable")["prompt"] == "Say hi to $comments"
    assert node_by_id(again, "fable")["prompt_ref"] is None
    # The unused helper stays available in the helpers section.
    assert "def fable_prompt" in again["helpers"]


def test_cleared_prompt_restores_callable(unstructured_def, tmp_path):
    c = gui.load_contract(unstructured_def)
    fable = node_by_id(c, "fable")
    assert fable["prompt"] is None and fable["prompt_ref"] == "fable_prompt"
    target = tmp_path / "structured.py"
    gui.save_contract(target, c, force=True)
    assert "prompt=fable_prompt" in target.read_text(encoding="utf-8")


def test_undefined_expression_name_warns(unstructured_def, tmp_path):
    c = gui.load_contract(unstructured_def)
    fable = node_by_id(c, "fable")
    fable["gate"] = "gen_f or missing_helper(me.decision)"
    fable["prompt"] = "literal"
    target = tmp_path / "structured.py"
    saved = gui.save_contract(target, c, force=True)
    assert any("missing_helper" in w for w in saved["warnings"])
