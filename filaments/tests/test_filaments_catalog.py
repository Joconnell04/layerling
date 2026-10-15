"""filaments/catalog.json build and the Supabase seed (against a fake PostgREST)."""

from __future__ import annotations

import json
import re
from urllib.parse import unquote

import httpx
import pytest
from p2p_contracts import FilamentLibrary
from p2p_filaments import catalog as cat
from p2p_filaments.colour import hex_to_lab
from p2p_filaments.seed import SeedError, seed, to_row

OWNED = {"generic-pla-black", "generic-pla-brown", "bambu-pla-basic-red", "generic-petg-blue",
         "generic-pla-white"}


@pytest.fixture(scope="module")
def built():
    colors = json.loads(cat.COLORS.read_text())
    presets = json.loads(cat.PRESETS.read_text())
    return cat.build_catalog(colors, presets, OWNED)


def test_catalog_rules(built):
    lib, rep = built
    ids = [f.id for f in lib.filaments]
    assert len(ids) == len(set(ids)) == rep["total"] > 200
    assert not set(ids) & OWNED and rep["owned_ids_skipped"] == ["bambu-pla-basic-red"]
    lines = list(dict.fromkeys(f.line for f in lib.filaments))
    assert lines[:4] == ["PLA Basic", "PLA Matte", "PETG Basic", "PETG HF"]
    assert not any(re.match(r"(PVA|Support)", x) for x in lines)
    for f in lib.filaments:
        assert re.fullmatch(r"bambu-[a-z0-9-]+", f.id) and f.brand == "Bambu Lab"
        assert not f.owned and f.spools == 0 and f.ams_slot is None
        assert f.slicer_preset.endswith("@BBL X2D 0.4 nozzle") and f.bambu_filament_id
        assert re.fullmatch(r"#[0-9A-F]{6}", f.hex) and f.lab_source == "nominal"
        assert f.lab == pytest.approx(tuple(hex_to_lab(f.hex)), abs=0.006)
    jade = next(f for f in lib.filaments if f.id == "bambu-pla-basic-jade-white")
    assert jade.material == "PLA" and jade.slicer_preset == "Bambu PLA Basic @BBL X2D 0.4 nozzle"
    assert rep["skipped"]["not single color"] > 0 and rep["skipped"]["no X2D preset"] > 0


def test_slugs_keep_plus_lines_apart():
    assert cat.filament_id("PLA Silk+", "Gold") == "bambu-pla-silk-plus-gold"
    assert cat.filament_id("PLA Silk", "Gold") == "bambu-pla-silk-gold"
    assert cat.filament_id("PETG HF", "Jade White") == "bambu-petg-hf-jade-white"


def test_committed_catalog_is_current_and_rebuild_is_stable(built, tmp_path):
    lib, _ = built
    committed = cat.load_catalog()
    assert committed.filaments == lib.filaments  # rerun `python -m p2p_filaments catalog`
    out = tmp_path / "catalog.json"
    first = cat.write_catalog(out, owned=OWNED)
    stamp = json.loads(out.read_text())["updated_at"]
    second = cat.write_catalog(out, owned=OWNED)
    assert first["changed"] and not second["changed"]
    assert json.loads(out.read_text())["updated_at"] == stamp


# ---------------------------------------------------------------- seed


class FakeRest:
    """Just enough PostgREST for seed(): GET with id/owned/owner_id filters, POST with
    on_conflict=id and ignore-duplicates."""

    def __init__(self, rows: list[dict], tamper: bool = False):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.tamper = tamper
        self.posts = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        q = dict(req.url.params)
        if req.method == "GET":
            rows = list(self.rows.values())
            if q.get("owned") == "is.true":
                rows = [r for r in rows if r["owned"]]
            if q.get("owner_id") == "is.null":
                rows = [r for r in rows if r["owner_id"] is None]
            if "id" in q:
                want = set(unquote(q["id"]).removeprefix("in.(").removesuffix(")").split(","))
                rows = [r for r in rows if r["id"] in want]
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 10**9))
            rows = rows[off:off + lim]
            if q.get("select") == "id":
                rows = [{"id": r["id"]} for r in rows]
            return httpx.Response(200, json=rows)
        if req.method == "POST":
            assert q.get("on_conflict") == "id"
            assert "ignore-duplicates" in req.headers["Prefer"]
            self.posts += 1
            for r in json.loads(req.content):
                self.rows.setdefault(r["id"], r)
            if self.tamper:
                self.rows["generic-pla-black"]["spools"] = 9
            return httpx.Response(201)
        return httpx.Response(405)


def owned_rows() -> list[dict]:
    lib = FilamentLibrary.model_validate_json(
        (cat.REPO / "filaments" / "library.json").read_text())
    rows = []
    for f in lib.filaments:
        r = to_row(f)
        r.update(owner_id=None, owned=True, spools=1, ams_slot=f.ams_slot)
        rows.append(r)
    return rows


def test_seed_inserts_once_and_never_touches_owned_rows(built):
    lib, _ = built
    fake = FakeRest(owned_rows())
    owned_before = {k: dict(v) for k, v in fake.rows.items()}
    client = httpx.Client(transport=httpx.MockTransport(fake), base_url="https://x/rest/v1")
    rep = seed(lib, client, protect=OWNED)
    assert rep["inserted"] == len(lib.filaments) == rep["catalog_rows_in_table"]
    assert rep["owned_unchanged"] and rep["owned_id_clash"] == []
    assert all(fake.rows[k] == v for k, v in owned_before.items())
    shared = [r for r in fake.rows.values() if r["id"] not in OWNED]
    assert all(r["owner_id"] is None and not r["owned"] and r["spools"] == 0 for r in shared)
    again = seed(lib, client, protect=OWNED)
    assert again["inserted"] == 0 and again["already_present"] == len(lib.filaments)
    dry = seed(lib, client, protect=OWNED, dry_run=True)
    batches = -(-len(lib.filaments) // 100)
    assert dry["to_insert"] == 0 and fake.posts == batches  # reruns posted nothing


def test_seed_skips_a_catalog_id_an_owned_row_holds_and_fails_if_an_owned_row_changes(built):
    lib, _ = built
    clash = lib.model_copy(update={"filaments": [
        lib.filaments[0].model_copy(update={"id": "bambu-pla-basic-red"}), *lib.filaments[1:3]]})
    fake = FakeRest(owned_rows())
    red = dict(fake.rows["bambu-pla-basic-red"])
    client = httpx.Client(transport=httpx.MockTransport(fake), base_url="https://x/rest/v1")
    rep = seed(clash, client, protect=OWNED)
    assert rep["owned_id_clash"] == ["bambu-pla-basic-red"] and rep["inserted"] == 2
    assert fake.rows["bambu-pla-basic-red"] == red
    bad = FakeRest(owned_rows(), tamper=True)
    client = httpx.Client(transport=httpx.MockTransport(bad), base_url="https://x/rest/v1")
    with pytest.raises(SeedError, match="owned rows changed"):
        seed(lib, client, protect=OWNED)
