import os
from pathlib import Path

import numpy as np
import pytest
from p2p_contracts import Filament, PrintSettings
from p2p_threemf.demo import capsule, jackson_palette

BAMBU_BIN = Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")
BAMBU_PROFILES = Path("/Applications/BambuStudio.app/Contents/Resources/profiles/BBL.json")
# Projects saved by Bambu Studio on Jackson's machine (not in the repo). Tests using them skip
# elsewhere.
BAMBU_SAVED = Path(os.environ.get("P2P_BAMBU_SAVED_3MF", "data/bambu_saved.3mf"))


def _find_meshy_fixture() -> Path | None:
    """Meshy Multi-Color 3MF captured by scripts/meshy_capture.py (never committed)."""
    rel = Path("data/fixtures/meshy/04_multi_color/meshy_multicolor.3mf")
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / rel).exists():
            return parent / rel
    copy = here.parents[1] / "_out/fixtures/meshy_multicolor.3mf"
    return copy if copy.exists() else None


MESHY_FIXTURE = _find_meshy_fixture()
needs_meshy = pytest.mark.skipif(MESHY_FIXTURE is None, reason="Meshy fixture not present")
needs_bambu = pytest.mark.skipif(not BAMBU_BIN.exists(), reason="Bambu Studio not installed")
needs_profiles = pytest.mark.skipif(not BAMBU_PROFILES.exists(),
                                    reason="Bambu Studio profiles not installed")
needs_saved = pytest.mark.skipif(not BAMBU_SAVED.exists(), reason="Bambu-saved project not here")

LIB_HEXES = ["#FFFFFF", "#000000", "#8E9089", "#C12E1F", "#F4EE2A", "#0A2989", "#00AE42",
             "#FF6A13", "#5E43B7", "#F55A74", "#E4BD68", "#9D432C", "#D1D3D5", "#0086D6",
             "#6F5034", "#F7E6DE"]


def pla_palette(n: int) -> list[Filament]:
    """n Bambu PLA Basic filaments named the way filaments/library.example.json names them."""
    return [
        Filament(id=f"bambu-pla-basic-{i}", brand="Bambu Lab", line="PLA Basic", name=f"c{i}",
                 hex=LIB_HEXES[i], lab=(50.0, 0.0, 0.0), bambu_filament_id="GFA00",
                 slicer_preset="Bambu PLA Basic @BBL X2D")
        for i in range(n)
    ]


def settings(n: int) -> PrintSettings:
    return PrintSettings(printer="Bambu Lab X2D", nozzle_mm=0.4, layer_mm=0.2,
                         ams_slots=max(4, n))


@pytest.fixture(scope="session")
def mesh():
    return capsule(radius=8.0, height=24.0, n_seg=32, n_ring=10)


@pytest.fixture
def jackson():
    return jackson_palette()


def random_slots(n_faces: int, n_slots: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    s = rng.integers(1, n_slots + 1, size=n_faces)
    s[: n_faces // 3] = 1  # a clear majority so the default slot is known
    return s


def _find_meshy_glb() -> Path | None:
    """Meshy image-to-3D glTF captured with the multi-color fixture (never committed)."""
    rel = Path("data/fixtures/meshy/02_image_to_3d_lite/model.glb")
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).exists():
            return parent / rel
    return None


MESHY_GLB = _find_meshy_glb()
needs_glb = pytest.mark.skipif(MESHY_GLB is None, reason="Meshy glTF fixture not present")


def read_glb(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """First primitive's positions and triangle indices from a .glb (test helper)."""
    import json
    import struct

    b = path.read_bytes()
    jl = struct.unpack("<I", b[12:16])[0]
    j = json.loads(b[20:20 + jl])
    binary = b[20 + jl + 8:]

    def acc(i):
        a = j["accessors"][i]
        bv = j["bufferViews"][a["bufferView"]]
        start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        ncomp = {"SCALAR": 1, "VEC3": 3}[a["type"]]
        dt = {5126: np.float32, 5125: np.uint32, 5123: np.uint16}[a["componentType"]]
        return np.frombuffer(binary, dtype=dt, count=a["count"] * ncomp, offset=start)

    prim = j["meshes"][0]["primitives"][0]
    v = acc(prim["attributes"]["POSITION"]).reshape(-1, 3).astype(np.float64)
    f = acc(prim["indices"]).reshape(-1, 3).astype(np.int64)
    return v, f
