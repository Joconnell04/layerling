from pathlib import Path

import numpy as np
import pytest
from p2p_sim.fixtures import make_figurine
from p2p_sim.mesh import Mesh

MESHY_GLB = Path(__file__).resolve().parents[2] / "data/fixtures/meshy/02_image_to_3d_lite/model.glb"


@pytest.fixture(scope="session")
def fig50k():
    return make_figurine(50_000)


@pytest.fixture(scope="session")
def fig200k():
    return make_figurine(200_000)


@pytest.fixture(scope="session")
def ico():
    """Coarse closed sphere (320 faces) sitting on z = 0: big faces for pixel-exact checks."""
    import trimesh

    s = trimesh.creation.icosphere(subdivisions=2, radius=10.0)
    return Mesh(np.asarray(s.vertices) + [0, 0, 10.0], np.asarray(s.faces))


@pytest.fixture(scope="session")
def meshy_glb():
    if not MESHY_GLB.is_file():
        pytest.skip("real Meshy GLB fixture not present")
    return MESHY_GLB
