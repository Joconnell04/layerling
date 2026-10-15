"""Fixtures: a tiny fake repo with slice reports and a Meshy ledger, and a scratch costs folder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SLICE = {
    "ok": True, "slicer": "bambu_studio", "slicer_version": "2.08",
    "printer_profile": "Bambu Lab X2D 0.4 nozzle", "process_profile": "0.20mm Standard @BBL X2D",
    "print_time_s": 2049.0, "total_grams": 8.58, "purge_grams": 5.39, "slots_used": 4,
    "filaments": [
        {"slot": 1, "filament_id": "generic-pla-161616", "grams": 1.04},
        {"slot": 2, "filament_id": "generic-pla-7c4b00", "grams": 0.97},
        {"slot": 3, "filament_id": "bambu-pla-basic-f72323", "grams": 1.83},
        {"slot": 5, "filament_id": "generic-petg-ffffff", "grams": 4.74},
    ],
}

LISTINGS = [
    {"product": "keychain", "channel": "etsy", "region": "NY", "shop": "A", "title": "t",
     "url": "https://example.com/1", "price_usd": 18.0, "shipping_usd": 4.5, "status": "listed"},
    {"product": "keychain", "channel": "etsy", "region": "national", "shop": "B", "title": "t",
     "url": "https://example.com/2", "price_usd": 30.0, "shipping_usd": 0, "status": "listed"},
    {"product": "keychain", "channel": "ebay", "region": "national", "shop": "C", "title": "t",
     "url": "https://example.com/3", "price_usd": 45.0, "shipping_usd": None, "status": "sold"},
    {"product": "figurine", "channel": "etsy", "region": "NY", "shop": "D", "title": "t",
     "url": "https://example.com/4", "price_usd": 95.0, "shipping_usd": 8.0, "status": "listed"},
    {"product": "figurine", "channel": "etsy", "region": "national", "shop": "E", "title": "t",
     "url": "https://example.com/5", "price_usd": 150.0, "shipping_usd": 0, "status": "listed"},
    {"product": "magnet", "channel": "etsy", "region": "national", "shop": "F", "title": "t",
     "url": "https://example.com/6", "price_usd": None, "shipping_usd": None, "status": "listed"},
]


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    sr = root / "data" / "slice_reports"
    sr.mkdir(parents=True)
    (sr / "prod_keychain.slice_report.json").write_text(json.dumps(SLICE))  # no settings recorded
    big = dict(SLICE, print_time_s=45491.0, total_grams=184.57, purge_grams=148.35,
               filaments=[{"slot": 1, "filament_id": "generic-pla-black", "grams": 184.57}])
    (sr / "meshy_multicolor_x2d.slice_report.json").write_text(json.dumps(big))
    # Purge lab: the procedural keychain and the Meshy beagle at 45 mm, as recorded by the lab.
    flat = dict(SLICE, print_time_s=14389.0, total_grams=49.01, purge_grams=37.88,
                filaments=[{"slot": 2, "filament_id": "generic-pla-7c4b00", "grams": 49.01}])
    upright = dict(SLICE, print_time_s=26131.0, total_grams=100.76, purge_grams=89.73,
                   filaments=[{"slot": 2, "filament_id": "generic-pla-7c4b00", "grams": 100.76}])
    lab = {
        "keychain__z_combined__writer_defaults": (SLICE, {}, "upright"),
        "beagle45__baseline__base": (upright, {"prime_volume_mode": "Default",
                                               "flush_into_infill": "0", "enable_support": "1"},
                                     "upright"),
        "beagle45__z_combined__saving_infill_back": (flat, {"prime_volume_mode": "Saving",
                                                            "flush_into_infill": "1",
                                                            "enable_support": "1"}, "back"),
    }
    rows = []
    for name, (rep, ov, orient) in lab.items():
        (sr / f"{name}.json").write_text(json.dumps(rep))
        fixture, lever, variant = name.split("__")
        rows.append({"fixture": fixture, "lever": lever, "variant": variant, "overrides": ov,
                     "layer_mm": 0.2, "orient": orient, "quality_risk": "", "ok": True,
                     "report": f"{name}.json"})
    (sr / "purge_lab.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    ledger = [
        {"at": "2026-09-24T03:39:45+00:00", "vendor": "meshy", "kind": "image-to-image",
         "credits": 3, "usd": 0.06, "ref": "a", "test_mode": False},
        {"at": "2026-09-24T03:42:14+00:00", "vendor": "meshy", "kind": "image-to-3d",
         "credits": 15, "usd": 0.3, "ref": "b", "test_mode": False},
    ]
    (root / "data" / "ledger.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ledger))
    job = root / "jobs" / "j1"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"id": "j1", "product_type": "nameplate",
                                              "status": "done",
                                              "created_at": "2026-09-24T04:00:00Z"}))
    live = root / "jobs" / "j2"  # still running: listed, not counted
    live.mkdir(parents=True)
    (live / "job.json").write_text(json.dumps({"id": "j2", "product_type": "nameplate",
                                               "status": "waiting_user"}))
    (job / "ledger.jsonl").write_text(json.dumps(
        {"at": "2026-09-24T04:00:01Z", "job_id": "j1", "vendor": "meshy", "kind": "image-to-image",
         "credits": 66, "usd": 1.32, "ref": "c", "test_mode": True}) + "\n")
    return root


@pytest.fixture
def slice_report() -> dict:
    return dict(SLICE)


@pytest.fixture
def costs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "costs"
    (d / "research").mkdir(parents=True)
    (d / "research" / "market_test.json").write_text(json.dumps(LISTINGS))
    return d
