"""Every input carries its provenance: SOURCE with a link, FROM JACKSON, or ASSUMED with what
moves if it is wrong. Checked on the defaults and on the committed workbook Jackson edits."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook
from p2p_costs.defaults import INPUTS, PROVENANCES, SOURCE
from p2p_costs.workbook import INPUTS as SHEET
from p2p_costs.workbook import read_inputs

WORKBOOK = Path(__file__).resolve().parents[1] / "cost_model.xlsx"


def test_defaults_have_provenance():
    keys = [i.key for i in INPUTS]
    assert len(keys) == len(set(keys)), "duplicate input keys"
    for i in INPUTS:
        assert i.provenance in PROVENANCES, i.key
        if i.provenance == SOURCE:
            assert i.source, f"{i.key}: SOURCE needs a link"
        if i.provenance == "ASSUMED":
            assert i.moves, f"{i.key}: ASSUMED needs 'if wrong, what moves'"


def test_no_em_dashes_in_defaults():
    for i in INPUTS:
        for text in (i.label, i.notes, i.moves, i.section):
            assert "—" not in text and "–" not in text, i.key


@pytest.mark.skipif(not WORKBOOK.exists(), reason="workbook not built yet")
def test_committed_workbook_inputs_have_provenance():
    wb = load_workbook(WORKBOOK)
    rows = read_inputs(wb[SHEET])
    assert rows
    for key, d in rows.items():
        assert d["provenance"] in PROVENANCES, key
        if d["provenance"] == SOURCE:
            assert d["source"], f"{key}: SOURCE needs a link"
