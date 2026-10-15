import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

REPO = Path(__file__).resolve().parents[2]
BEAGLE = Path(__file__).resolve().parents[2] / ("data/fixtures/meshy/02_image_to_3d_lite/model.glb")


@pytest.fixture(scope="session")
def library():
    from p2p_contracts.filaments import FilamentLibrary

    return FilamentLibrary.model_validate_json((REPO / "filaments" / "library.json").read_text())


@pytest.fixture(scope="session")
def figure():
    from geo_fixtures import hard_figure

    return hard_figure()


@pytest.fixture(scope="session")
def products(figure, library):
    """All five products built once from the hard figure."""
    from geo_fixtures import PRODUCT_CASES
    from p2p_geometry import build_product_geometry

    return {
        name: build_product_geometry(figure.vertices, figure.faces, p, a, t, library, None)
        for name, (p, a, t) in PRODUCT_CASES.items()
    }


@pytest.fixture(scope="session")
def beagle():
    if not BEAGLE.exists():
        pytest.skip("Meshy beagle GLB not on this machine")
    from p2p_geometry import load_meshy_glb

    return load_meshy_glb(BEAGLE)
