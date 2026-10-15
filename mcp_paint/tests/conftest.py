"""Procedural fixture: the p2p_sim two-animal figurine with the Frenchie's problem planted in it.

Texture colors are the ground-truth region colors. Specks of mid gray (texture) sit on the
white body of subject 2, and the "baseline" paints them brown, the way Meshy's Multi-Color plus
retarget did on the real Frenchie. The loaded library is filaments/library.json (black, brown,
red, PETG blue, white; no gray), the catalog filaments/library.example.json.

`live_claude` tests are opt-in: RUN_LIVE_CLAUDE=1.
"""

from __future__ import annotations

import os
import re

import numpy as np
import pytest
from p2p_contracts.regions import Region
from p2p_mcp_paint.job import load_fixture, load_library
from p2p_mcp_paint.palette import nearest
from p2p_mcp_paint.session import PaintSession
from p2p_sim.fixtures import REGION_LABELS, make_figurine

GRAY = (128, 128, 128)
N_SPECKS = 12


def pytest_configure(config):
    config.addinivalue_line("markers", "live_claude: runs Opus via Claude Code; RUN_LIVE_CLAUDE=1")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_LIVE_CLAUDE") == "1":
        return
    skip = pytest.mark.skip(reason="opt-in test; set RUN_LIVE_CLAUDE=1 to run")
    for item in items:
        if "live_claude" in item.keywords:
            item.add_marker(skip)


def _part(label: str) -> str:
    name = label.split(".", 1)[-1]
    if name.startswith("ear_inner"):
        return "inner_ear"
    head = re.split(r"_", name)[0]
    return {"stripe": "marking", "patch": "marking", "base": "base"}.get(head, head)


@pytest.fixture(scope="session")
def figurine():
    fx = make_figurine(12_000, texture=False)
    mesh = fx.mesh
    pal = np.array([[int(h[i:i + 2], 16) for i in (1, 3, 5)] for h in fx.palette_hex], np.uint8)
    region_rgb = np.zeros((max(REGION_LABELS) + 1, 3), np.uint8)
    region_rgb[0] = pal[-1]
    for rid, slot in fx.region_slot.items():
        region_rgb[rid] = pal[slot - 1]
    tex = region_rgb[fx.face_regions].copy()
    # plant gray specks on subject 2's white body: a face plus its neighbors
    rng = np.random.default_rng(3)
    body2 = np.nonzero(fx.face_regions == 16)[0]
    adj = mesh.face_adjacency
    speck = np.zeros(mesh.n_faces, bool)
    for f in rng.choice(body2, N_SPECKS, replace=False):
        speck[f] = True
        nb = adj[(adj[:, 0] == f) | (adj[:, 1] == f)].ravel()
        speck[nb[fx.face_regions[nb] == 16]] = True
    tex[speck] = GRAY
    return fx, tex, speck


@pytest.fixture()
def session(tmp_path, figurine):
    fx, tex, speck = figurine
    owned, catalog = load_library()
    # baseline: nearest loaded filament to the ground-truth color, specks forced to brown
    j, _ = nearest(np.asarray(
        __import__("p2p_sim.color", fromlist=["srgb_to_lab"]).srgb_to_lab(tex)), owned)
    base = np.array([owned[i].id for i in j], dtype=object)
    base[speck] = "generic-pla-brown"
    regions = [Region(id=rid, label=name, subject_id=name.split(".")[0] if "." in name else None,
                      part=_part(name), source="sam3", n_faces=0, area_mm2=0, centroid_mm=(0, 0, 0),
                      bbox_min_mm=(0, 0, 0), bbox_max_mm=(0, 0, 0))
               for rid, (name, _) in REGION_LABELS.items()]
    job_dir = tmp_path / "job-fixture"
    job_dir.mkdir()
    spec = [{"subject_id": "s2", "region": "body", "color": "white", "hex": "#F4F4F0",
             "kind": "base"},
            {"subject_id": "s2", "region": "nose", "color": "black", "hex": "#1C1C1E",
             "kind": "feature"}]
    inp = load_fixture(job_dir, fx.mesh, tex_rgb=tex, baseline_filaments=base, color_spec=spec,
                       library=owned, catalog=catalog)
    s = PaintSession.from_inputs(inp, job_dir / "s09_paint" / "a1", face_regions=fx.face_regions,
                                 regions=regions)
    s._speck = speck
    return s
