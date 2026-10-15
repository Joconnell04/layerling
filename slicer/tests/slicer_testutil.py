"""Shared helpers for the slicer tests: where Bambu's kept calibration slices are."""

import json
from pathlib import Path

import pytest
from p2p_slicer.compare import find_truth_dir

REPO = Path(__file__).resolve().parents[2]
CAL = REPO / "slicer" / "calibration"
TRUTH = json.loads((CAL / "truth.json").read_text())
CASES = json.loads((CAL / "cases.json").read_text())["cases"]
TRUTH_DIR = find_truth_dir(REPO)
needs_truth = pytest.mark.skipif(TRUTH_DIR is None,
                                 reason="Bambu calibration G-code (_truth/) not on this machine")
CASE_IDS = sorted(TRUTH["cases"])
