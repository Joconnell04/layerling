import base64
import importlib.util
import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

HANDLER_PATH = Path(__file__).resolve().parents[1] / "runpod" / "handler.py"


@pytest.fixture(scope="session")
def handler_mod():
    """Import sam3/runpod/handler.py by path. It must not need torch at import time."""
    spec = importlib.util.spec_from_file_location("sam3_runpod_handler", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def png_bytes(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def png_b64(arr: np.ndarray) -> str:
    return base64.b64encode(png_bytes(arr)).decode()


@pytest.fixture
def small_png_b64():
    return png_b64(np.full((32, 48, 3), 200, np.uint8))


@pytest.fixture(name="png_b64")
def png_b64_fixture():
    return png_b64


@pytest.fixture(name="png_bytes")
def png_bytes_fixture():
    return png_bytes
