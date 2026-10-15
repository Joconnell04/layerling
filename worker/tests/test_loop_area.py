"""Loop placement follows the customer (docs/runs/fidelity/rerun2): the intake answer names an
area, the approved concept fills in when there is no answer, and only "any" lets geometry move
the loop to wherever the piece balances."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml
from p2p_contracts.job import AddOn, AddOnKind
from p2p_worker import imaging
from p2p_worker.concept_read import ConceptRead
from p2p_worker.product_spec import resolve_loop
from p2p_worker.stages.prompts import LOOP_SPOTS
from PIL import Image

BANK = Path(__file__).resolve().parents[1] / "src/p2p_worker/stages/question_bank.yaml"


def _ring(**params):
    return [AddOn(kind=AddOnKind.KEYRING_LOOP, params=params)]


def test_the_answer_decides_the_area_and_wins_over_the_concept():
    addons, info = resolve_loop(_ring(position="top"), None)
    assert info["area"] == "head" and info["source"] == "intake answer"
    assert addons[0].params["position"] == "top"
    read = {"loop": {"shown": True, "hex": "#161616"}, "loop_spot": "back"}
    _, info = resolve_loop(_ring(position="top"), read)
    assert info["area"] == "head" and "answer was top" in info["conflict"]
    _, info = resolve_loop(_ring(position="any"), read)
    assert info["area"] == "any" and info["conflict"] is None


def test_the_concept_fills_in_when_nobody_answered():
    read = {"loop": {"shown": True, "hex": "#161616"}, "loop_spot": "head"}
    addons, info = resolve_loop(_ring(), read)
    assert info["source"] == "approved concept" and info["area"] == "head"
    assert addons[0].params["position"] == "top"
    _, info = resolve_loop(_ring(), {"loop": {"shown": False}, "loop_spot": None})
    assert info["area"] == "any"
    assert resolve_loop([AddOn(kind=AddOnKind.NAMEPLATE)], read) == (
        [AddOn(kind=AddOnKind.NAMEPLATE)], None)


def test_concept_read_accepts_a_loop_spot():
    r = ConceptRead.model_validate({"form": "figure", "figure_colors": [
        {"area": "coat", "hex": "#FFFFFF", "share": 1.0}], "loop": {"shown": True,
        "hex": "#161616"}, "loop_spot": "head"})
    assert r.loop_spot == "head"
    assert ConceptRead.model_validate({"form": "figure", "figure_colors": [
        {"area": "coat", "hex": "#FFFFFF", "share": 1.0}]}).loop_spot is None


def test_keychain_loop_question_offers_anywhere():
    bank = yaml.safe_load(BANK.read_text())
    loop = next(q for q in bank["keychain"] if q["id"] == "loop")
    opts = {o["id"]: o for o in loop["options"]}
    assert opts["top"]["label"] == "Top of the head"
    assert opts["any"]["addon"]["params"]["position"] == "any"
    assert all(len(o["label"]) <= 40 for o in loop["options"])
    assert "any" in LOOP_SPOTS


def test_concept_draws_a_back_loop_over_the_middle_not_on_the_head(tmp_path):
    """A standing animal facing left: the head is the tall part on the left. The top loop is
    drawn on the head; back and any are drawn over the silhouette's middle."""
    img = np.zeros((200, 300, 4), np.uint8)
    img[100:160, 40:260] = (200, 120, 60, 255)  # body
    img[50:110, 30:90] = (60, 120, 200, 255)  # head, higher, on the left
    src = tmp_path / "fig.png"
    Image.fromarray(img).save(src)
    xs = {}
    for pos in ("top", "back", "any"):
        out = tmp_path / f"{pos}.png"
        imaging.composite_loop(src, out, pos)
        a = np.asarray(Image.open(out).convert("RGBA")).astype(int)
        added = (a[..., 3] > 0) & (np.abs(a[..., :3] - [200, 120, 60]).sum(-1) > 30) & \
            (np.abs(a[..., :3] - [60, 120, 200]).sum(-1) > 30)
        cols = np.nonzero(added.any(axis=0))[0]
        xs[pos] = (cols.min() + cols.max()) / 2 / a.shape[1] if len(cols) else None
    assert xs["top"] is not None and xs["back"] is not None
    assert xs["back"] > xs["top"] + 0.1 and abs(xs["back"] - xs["any"]) < 0.02
