"""A band prints as one strip or not at all (live2 sombrero: the hat's striped band printed as
15 red spots, largest 12%, gaps up to 8.3 mm, and nothing in the paint rules noticed).

Synthetic flat grid for the region ops; the procedural figurine for the session, the
checklist, the final check's fixes and the paint run."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from p2p_contracts.mcp_tools import MergeRegionsIn, SplitRegionIn
from p2p_mcp_paint import regionops
from p2p_mcp_paint.agent import StubDriver, run_paint
from p2p_mcp_paint.checklist import check_islands
from p2p_mcp_paint.concept_check import Discrepancy, Verdict, apply_fixes
from p2p_mcp_paint.session import PaintSession
from p2p_sim.mesh import Mesh

GAPS = (2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 3.0, 4.0)  # mm, like the sombrero's 3.7 to 8.3
DASH = 4.0


def grid(w: float = 100.0, h: float = 20.0, step: float = 0.5) -> Mesh:
    nx, ny = int(w / step), int(h / step)
    xs, ys = np.meshgrid(np.arange(nx + 1) * step, np.arange(ny + 1) * step, indexing="ij")
    v = np.stack([xs.ravel(), ys.ravel(), np.zeros(xs.size)], 1)
    idx = np.arange((nx + 1) * (ny + 1)).reshape(nx + 1, ny + 1)
    a, b = idx[:-1, :-1].ravel(), idx[1:, :-1].ravel()
    c, d = idx[1:, 1:].ravel(), idx[:-1, 1:].ravel()
    f = np.concatenate([np.stack([a, b, c], 1), np.stack([a, c, d], 1)])
    return Mesh(vertices=v, faces=f)


def dashed_band(mesh: Mesh, y0: float = 9.0, y1: float = 11.0, x0: float = 9.0):
    """Background 1; a 2 mm band (label 2) drawn as 10 dashes of 4 mm with GAPS between."""
    c = mesh.face_centroids
    fr = np.ones(mesh.n_faces, np.int32)
    x = x0
    for k in range(10):
        on = (c[:, 0] >= x) & (c[:, 0] < x + DASH) & (c[:, 1] >= y0) & (c[:, 1] < y1)
        fr[on] = 2
        x += DASH + (GAPS[k] if k < len(GAPS) else 0.0)
    return fr


def test_a_dashed_band_is_bridged_into_one_strip():
    mesh = grid()
    fr = dashed_band(mesh)
    c = mesh.face_centroids
    # an eye (protected) sits in the 6 mm gap; the band must go around it, never over it
    eye_x = 9.0 + 5 * DASH + sum(GAPS[:4]) + 3.0
    eye = (np.abs(c[:, 0] - eye_x) < 1.0) & (np.abs(c[:, 1] - 10.0) < 1.5)
    fr[eye] = 3
    before = regionops.pieces(mesh, fr, 2)
    assert before["pieces"] == 10 and regionops.fragmented(before)
    out, info = regionops.consolidate(mesh, fr, 2, protect=[3])
    assert info["action"] == "bridged", info
    after = regionops.pieces(mesh, out, 2)
    assert after["pieces"] <= 2 and not regionops.fragmented(after)  # the eye splits it once
    assert (out[eye] == 3).all()
    band = out == 2
    # bridges at the band's own width (2 mm): nothing farther than one width from its middle
    assert info["width_mm"] == pytest.approx(2.0, abs=0.6)
    off = np.abs(c[:, 1] - 10.0)
    assert off[band & (np.abs(c[:, 0] - eye_x) > 4.0)].max() <= 2.0 + 0.3
    assert off[band].max() <= 3.0  # the bridge past the eye goes around it
    # the gaps filled: a strip about as long as the dashes plus the gaps
    assert after["area_mm2"] > 1.8 * before["area_mm2"]
    assert info["growth"] <= regionops.BRIDGE_MAX_GROWTH


def test_a_band_that_cannot_be_one_strip_takes_the_color_around_it():
    mesh = grid()
    fr = dashed_band(mesh)
    out, info = regionops.consolidate(mesh, fr, 2, gap_mm=1.0)  # no gap is that small
    assert info["action"] == "merged" and info["into"] == [1]
    assert not (out == 2).any()
    left, _ = regionops.consolidate(mesh, fr, 2, gap_mm=1.0, merge=False)
    assert (left == fr).all()


def test_band_words_decide_what_a_band_is():
    assert regionops.band_like("s1.hat_trim") and regionops.band_like("s2.collar")
    assert regionops.band_like("s1.hat", "Hat trim prints as patchy red spots, not a band")
    assert not regionops.band_like("s1.marking_spots", "spots print too small")
    assert not regionops.band_like("s1.stripe_1")  # coat stripes are markings


def test_spots_are_markings_and_never_change(session):
    """A dalmatian's spots are many pieces by design: nothing bridges or merges them, even when
    the final check names them."""
    s = session
    spots = _break_collar(s)
    s.regions[spots] = s.regions[spots].model_copy(update={"label": "s2.marking_spots"})
    before = s.fr.copy()
    assert regionops.fragmented(regionops.pieces(s.inp.figure, s.fr, spots))
    assert s.consolidate_bands(notes={spots: "black spots print as small dots"}) == []
    assert (s.fr == before).all()


def _rid(s: PaintSession, label: str) -> int:
    return next(r for r, reg in s.regions.items() if reg.label == label)


def _break_collar(s: PaintSession) -> int:
    """Cut the fixture's collar into dashes around the neck, the way Meshy's paint left the
    sombrero's band: every other slice of it goes to the body around it."""
    collar, body = _rid(s, "s2.collar"), _rid(s, "s2.body")
    fig = s.inp.figure
    m = s.fr == collar
    c = fig.face_centroids[m]
    center = c.mean(0)
    ang = np.arctan2(c[:, 1] - center[1], c[:, 0] - center[0])
    fr = s.fr.copy()
    idx = np.nonzero(m)[0]
    fr[idx[(np.floor((ang + np.pi) / (2 * np.pi) * 16) % 2) == 1]] = body
    s._apply(fr, "test: the collar printed as dashes")
    return collar


def test_a_broken_collar_fails_islands_and_the_final_check_makes_it_one_strip(session):
    s = session
    collar = _break_collar(s)
    info = regionops.pieces(s.inp.figure, s.fr, collar)
    assert regionops.fragmented(info), info
    item = check_islands(s, s.run_sim())
    assert item.passed is False and "bands printing as spots" in item.evidence
    assert "s2.collar" in s.region_table()  # the table shows its pieces
    row = next(r for r in s.region_table().splitlines() if "s2.collar" in r)
    assert f"| {info['pieces']} (" in row
    verdict = Verdict(matches=False, confidence=0.5, summary="collar", discrepancies=[
        Discrepancy(view="front", what="Collar prints as red spots instead of a continuous band",
                    route="paint_region", region_ids=[collar],
                    filament_id="generic-pla-red")])
    applied, agent, blocked = apply_fixes(s, verdict)
    assert any("bridged" in a or "merged" in a for a in applied), applied
    assert not agent and not blocked
    after = regionops.pieces(s.inp.figure, s.fr, collar)
    assert not regionops.fragmented(after)
    assert "bands printing as spots" not in check_islands(s, s.run_sim()).evidence


def test_merging_band_pieces_bridges_them(session):
    s = session
    collar = _break_collar(s)
    split = s.split_region(SplitRegionIn(region_id=collar, method="connected_components"))
    pieces = [collar, *[r for r in split.out.changed_region_ids if r != collar]]
    assert len(pieces) >= 4
    res = s.merge_regions(MergeRegionsIn(region_ids=pieces, label="s2.collar"))
    assert "bridged" in (res.out.message or ""), res.out.message
    assert not regionops.fragmented(regionops.pieces(s.inp.figure, s.fr, collar))


def test_the_paint_run_never_writes_a_band_as_spots(session):
    """The safety net before the final write: whatever the agent did, a band-like region that
    still prints as pieces is one strip in the 3MF."""
    from p2p_contracts.reports import SliceReport

    s = session
    collar = _break_collar(s)

    def fake_slice(sess):
        return SliceReport(ok=True, slicer="bambu_studio", slicer_version="stub",
                           printer_profile="x", process_profile="y", print_time_s=60.0,
                           total_grams=5.0, purge_grams=1.2)

    driver = StubDriver(lambda: PaintSession.resume(s.inp, s.dir, s.attempt),
                        plan=lambda sess, k: [])
    run = asyncio.run(run_paint(s.inp.job_id, session=s, driver=driver, slice_fn=fake_slice,
                                log=lambda m: None, concept_png=None))
    final = PaintSession.resume(s.inp, s.dir, s.attempt)
    assert collar not in final.regions or not regionops.fragmented(
        regionops.pieces(final.inp.figure, final.fr, collar))
    assert any("bands (" in c["note"] for c in final.checkpoints)
    assert run.threemf is not None
