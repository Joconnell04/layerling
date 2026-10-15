"""Cost-aware palette decision (palette_plan.py): every filament earns its place against purge
and print time, near shades merge, owned spools win ties, small defining colors stay."""

from __future__ import annotations

import numpy as np
import pytest
import trimesh
from p2p_contracts.mcp_tools import PaintRegionIn
from p2p_contracts.regions import Region
from p2p_mcp_paint import palette as pal
from p2p_mcp_paint.job import load_fixture, load_library
from p2p_mcp_paint.palette_plan import (
    DEFINING_DE,
    Planner,
    cap_for,
    plan_de,
    summary_line,
)
from p2p_mcp_paint.session import PaintSession, ToolError
from p2p_sim.mesh import Mesh

WHITE, BLACK, PINK = (245, 245, 245), (22, 22, 22), (217, 169, 169)
FRENCHIE_READ = {"form": "figure", "figure_colors": [
    {"area": "coat", "hex": "#DADADA", "share": 0.55},
    {"area": "face mask", "hex": "#1C1C1C", "share": 0.35},
    {"area": "inner ears", "hex": "#D9A9A9", "share": 0.05}]}


SPEC = [{"subject_id": "s1", "region": "body", "color": "white", "hex": "#F5F5F5",
         "kind": "base"},
        {"subject_id": "s1", "region": "inner ears", "color": "pink", "hex": "#FFB6C1",
         "kind": "feature"}]


def _dog(tmp_path, read=FRENCHIE_READ, coat=None, product="keychain", spec=SPEC):
    """A 20 x 20 x 40 mm block: a speckled white coat (texture averages to gray), a black mask
    down one side sharing its layers (so a third color costs purge on every layer) and a small
    pink patch on top (the inner ears). The photos say white body and pink inner ears."""
    m = trimesh.creation.box((20, 20, 40))
    for _ in range(4):
        m = m.subdivide()
    v = np.asarray(m.vertices) + [0, 0, 20]
    f = np.asarray(m.faces, np.int64)
    mesh = Mesh(v, f)
    c = mesh.face_centroids
    fr = np.ones(len(f), np.int32)  # 1 coat
    fr[(c[:, 0] < -4) & (c[:, 2] > 4)] = 2  # 2 mask, beside the coat on most layers
    fr[(c[:, 2] > 39.5) & (np.abs(c[:, 0]) < 5) & (np.abs(c[:, 1]) < 5)] = 3  # 3 inner ears
    rng = np.random.default_rng(1)
    tex = np.zeros((len(f), 3), np.uint8)
    coat_rgb = np.where(rng.random((len(f), 1)) < 0.8, WHITE, BLACK) if coat is None else \
        np.tile(np.array(coat, np.uint8), (len(f), 1))
    tex[fr == 1] = coat_rgb[fr == 1]
    tex[fr == 2] = BLACK
    tex[fr == 3] = PINK
    regions = [Region(id=i, label=f"s1.{n}", subject_id="s1", part=p, source="sam3",
                      n_faces=0, area_mm2=0, centroid_mm=(0, 0, 0), bbox_min_mm=(0, 0, 0),
                      bbox_max_mm=(0, 0, 0))
               for i, n, p in ((1, "body", "body"), (2, "head", "head"),
                               (3, "ear_inner", "inner_ear"))]
    owned, catalog = load_library()
    job = tmp_path / "job"
    job.mkdir()
    inp = load_fixture(job, mesh, tex_rgb=tex, library=owned, catalog=catalog, color_spec=spec)
    inp.palette_source = "any"
    inp.product_type = product
    inp.concept_read = read
    s = PaintSession.from_inputs(inp, job / "s09_paint" / "a1", face_regions=fr, regions=regions)
    return s


def test_plan_de_blocks_gray_to_brown_but_lets_cream_match_a_cream_filament():
    gray, cream = pal.hex_lab("#808080"), pal.hex_lab("#EDE3D3")
    brown, tan = pal.hex_lab("#7C4B00"), pal.hex_lab("#E8DBB7")  # generic brown, desert tan
    cost, de = plan_de([gray, cream], [brown, tan])
    assert cost[0, 0] > 100 and cost[1, 1] < 15  # gray never brown; cream may be desert tan
    strict, _ = pal.guarded_de([cream], [tan])
    assert strict[0, 0] > 100  # the paint tools' own guard is unchanged


def test_caps_per_product():
    assert cap_for("keychain", 5) == 4 and cap_for("nameplate", 5) == 5
    assert cap_for("figurine", 3) == 3


def test_gray_shade_merges_into_white_and_pink_ears_stay(tmp_path):
    """The speckled coat averages to gray and the concept reads it light gray; light gray
    does not pay for its purge, so the coat prints white (owned). The small pink inner ears
    would print white (another hue), so the pink spool stays."""
    s = _dog(tmp_path)
    d = Planner(s).plan()
    pal_ids = set(d["palette"])
    assert "generic-pla-white" in pal_ids and "generic-pla-black" in pal_ids
    assert any("pink" in i for i in pal_ids)
    assert "bambu-pla-basic-light-gray" not in pal_ids
    assert not {"bambu-pla-basic-jade-white", "bambu-pla-matte-ivory-white"} & pal_ids  # owned
    assert d["buy"] == [i for i in d["palette"] if "pink" in i]
    assert d["score"]["defining_missed"] == 0 and len(d["palette"]) <= d["cap"] == 4
    drop = {x["filament"]: x for x in d["dropped"]}
    assert "bambu-pla-basic-light-gray" in drop
    gray = drop["bambu-pla-basic-light-gray"]
    assert "purge" in gray["why"] and gray["J_with"] > d["score"]["J"]
    pink = next(i for i in d["palette"] if "pink" in i)
    assert "without it" in d["filaments"][pink]["why"]
    assert "palette" in summary_line(d) and "purge" in summary_line(d)


def test_a_clearly_better_catalog_color_is_bought(tmp_path):
    """A tan coat: the owned brown is another hue (dE00 over 15), so a catalog tan is bought."""
    read = {"form": "figure", "figure_colors": [
        {"area": "coat", "hex": "#B97C3E", "share": 0.6},
        {"area": "face mask", "hex": "#1C1C1C", "share": 0.4}]}
    s = _dog(tmp_path, read=read, coat=(185, 124, 62), spec=[])
    d = Planner(s).plan()
    tan = [i for i in d["palette"] if not s.inp.owned(i)]
    assert tan and tan == d["buy"]
    _, de = pal.guarded_de([pal.hex_lab("#B97C3E")], [s.filament(tan[0]).lab])
    assert float(de[0, 0]) < DEFINING_DE


def test_apply_plan_then_extra_filaments_must_earn_their_place(tmp_path):
    s = _dog(tmp_path)
    d = s.apply_palette_plan()
    assert s.palette == d["palette"] and (s.dir / "palette_plan.json").is_file()
    assert s.assignment[1] == "generic-pla-white" and s.assignment[2] == "generic-pla-black"
    assert "pink" in s.assignment[3]
    assert s.checkpoints[-1]["note"].startswith("palette plan")
    # light gray back on the coat: a shade that does not pay for its purge
    with pytest.raises(ToolError, match="does not earn its place"):
        s.paint_region(PaintRegionIn(region_ids=[1], filament_id="bambu-pla-basic-light-gray",
                                     add_to_palette=True))
    assert "bambu-pla-basic-light-gray" not in s.palette
    assert s.plan["refused"] and s.plan["refused"][0]["cost"] > s.plan["refused"][0]["gain"]
    # the ears painted white and the pink dropped: pink comes back, it rescues a defining color
    pink = s.assignment[3]
    s.paint_region(PaintRegionIn(region_ids=[3], filament_id="generic-pla-white"))
    s._prune_palette()
    assert pink not in s.palette
    s.paint_region(PaintRegionIn(region_ids=[3], filament_id=pink, add_to_palette=True))
    assert pink in s.palette and s.plan["added"][-1]["rescues_defining"]


def test_suggest_palette_reruns_the_decision(tmp_path):
    from p2p_contracts.mcp_tools import SuggestPaletteIn

    s = _dog(tmp_path)
    s.apply_palette_plan()
    out = s.suggest_palette(SuggestPaletteIn(k=5, apply=True)).out
    assert set(out.palette) == set(s.palette) and "bambu-pla-basic-light-gray" not in out.palette


def test_face_part_checklist_respects_the_palette_decision(tmp_path):
    """A face part in the palette's nearest color passes when the closer catalog spool does
    not earn its place (the Bronco's mouth line: black, cocoa brown refused on purge)."""
    from p2p_mcp_paint.checklist import check_face_parts

    s = _dog(tmp_path)
    s.apply_palette_plan()
    # the black mask region stands in for a mouth whose texture is dark brown
    s.regions[2] = s.regions[2].model_copy(update={"part": "mouth",
                                                   "color_lab": (28.0, 9.0, 16.0)})
    item = check_face_parts(s, set())
    assert "does not earn its place" in item.evidence
    assert "says" not in item.evidence.split(". ")[0] or item.passed is not False
