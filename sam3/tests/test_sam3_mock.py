import numpy as np
import pytest
from p2p_sam3 import rle
from p2p_sam3.client import BudgetExceeded, get_segmenter, segment
from p2p_sam3.mock import MockSegmenter
from p2p_sam3.parts import part_for_prompt


def face_render(size: int = 256) -> np.ndarray:
    """Flat render: gray background, tan head with two ears, black eyes and nose, base."""
    img = np.full((size, size, 3), 235, np.uint8)
    yy, xx = np.mgrid[:size, :size]
    tan = (200, 170, 130)
    head = (yy - 110) ** 2 + (xx - 128) ** 2 < 70**2
    ear_l = (yy > 25) & (yy < 70) & (np.abs(xx - 80) < (yy - 25) * 0.6)
    ear_r = (yy > 25) & (yy < 70) & (np.abs(xx - 176) < (yy - 25) * 0.6)
    body = (yy > 150) & (yy < 215) & (np.abs(xx - 128) < 60)
    base = (yy >= 215) & (yy < 240) & (np.abs(xx - 128) < 90)
    for m in (head, ear_l, ear_r, body):
        img[m] = tan
    img[base] = (90, 110, 160)
    img[(yy - 95) ** 2 + (xx - 100) ** 2 < 8**2] = (15, 15, 15)
    img[(yy - 95) ** 2 + (xx - 156) ** 2 < 8**2] = (15, 15, 15)
    img[(yy - 130) ** 2 + (xx - 128) ** 2 < 6**2] = (30, 20, 20)
    return img


def centroid(mask):
    ys, xs = np.nonzero(mask)
    return ys.mean(), xs.mean()


def test_heuristics_find_eyes_nose_ears():
    res = MockSegmenter().segment([face_render()], ["cat eyes", "nose", "ears", "body", "tail"])
    im = res.images[0]
    eyes = im.get("cat eyes").decoded()
    assert len(eyes) == 2
    (ly, lx), (_, rx) = centroid(eyes[0]), centroid(eyes[1])
    assert abs(lx - 100) < 2 and abs(rx - 156) < 2 and abs(ly - 95) < 2
    nose = im.get("nose").decoded()
    assert len(nose) == 1 and np.allclose(centroid(nose[0]), (130, 128), atol=2)
    ears = im.get("ears").decoded()
    assert len(ears) == 2 and centroid(ears[0])[1] < 128 < centroid(ears[1])[1]
    assert len(im.get("body").masks) == 1
    assert im.get("tail").masks == []
    assert res.backend == "mock" and res.usd == 0.0
    assert res.spend[0].test_mode and res.spend[0].usd == 0.0


def test_color_rules_and_fixtures():
    img = face_render()
    fixture = np.zeros(img.shape[:2], bool)
    fixture[10:20, 10:20] = True
    seg = MockSegmenter(
        fixtures={(0, "collar"): [(fixture, 0.95)]}, color_rules={"base": (90, 110, 160)}
    )
    res = seg.segment([img], ["collar", "blue base"], score_threshold=0.5)
    collar = res.images[0].get("collar")
    assert collar.scores == [0.95] and np.array_equal(collar.decoded()[0], fixture)
    base = res.images[0].get("blue base").decoded()
    assert len(base) == 1 and base[0].sum() == int(
        (np.abs(img.astype(int) - (90, 110, 160)).max(axis=2) <= 12).sum()
    )


def test_threshold_and_topk():
    img = face_render()
    masks = []
    for i in range(5):
        m = np.zeros(img.shape[:2], bool)
        m[i * 10 : i * 10 + 5, :5] = True
        masks.append((m, 0.4 + 0.1 * i))
    seg = MockSegmenter(fixtures={(0, "spot"): masks})
    p = seg.segment([img], ["spot"], score_threshold=0.55, max_masks_per_prompt=2).images[0]
    assert p.get("spot").scores == [0.8, 0.7]
    assert p.get("spot").boxes[0] == (0.0, 40.0, 5.0, 45.0)
    assert rle.area(p.get("spot").masks[0].model_dump()) == 25


def test_budget_guard_and_spend_callback():
    events = []
    res = segment(
        [face_render(64)],
        ["eye"],
        segmenter=MockSegmenter(),
        on_spend=events.append,
        job_id="j1",
    )
    assert events == res.spend and events[0].job_id == "j1" and events[0].vendor == "runpod"
    with pytest.raises(BudgetExceeded):
        segment([face_render(64)], ["eye"], segmenter=MockSegmenter(), budget_guard=lambda u: False)


def test_backend_from_env(monkeypatch):
    monkeypatch.delenv("SAM3_BACKEND", raising=False)
    assert get_segmenter().backend == "mock"
    monkeypatch.setenv("SAM3_BACKEND", "runpod")
    monkeypatch.setenv("RUNPOD_API_KEY", "k")
    monkeypatch.setenv("RUNPOD_SAM3_ENDPOINT_ID", "ep1")
    seg = get_segmenter()
    assert seg.backend == "runpod" and seg.usd_per_hour == 0.58
    monkeypatch.setenv("SAM3_BACKEND", "gpu")
    with pytest.raises(ValueError):
        get_segmenter()


@pytest.mark.parametrize(
    "prompt, part",
    [
        ("eye", "eye"),
        ("cat eyes", "eye"),
        ("left eye", "eye"),
        ("dog nose", "nose"),
        ("inner ear", "inner_ear"),
        ("pink paw pads", "paw_pad"),
        ("ears", "ear"),
        ("black patch on the back", "marking"),
        ("white stripe", "marking"),
        ("the pedestal", "base"),
        ("red collar", "collar"),
        ("whiskers", "other"),
        ("eye highlight", "eye_highlight"),
        ("pupil", "pupil"),
    ],
)
def test_part_for_prompt(prompt, part):
    assert part_for_prompt(prompt) == part


def test_cli_mock_writes_result_and_overlay(tmp_path, capsys):
    import json

    from p2p_sam3.__main__ import main
    from PIL import Image

    img = tmp_path / "render.png"
    Image.fromarray(face_render()).save(img)
    jpg = tmp_path / "photo.jpg"
    Image.fromarray(face_render()).save(jpg)
    main(["--backend", "mock", "--images", str(img), str(jpg), "--out", str(tmp_path / "o")])
    res = json.loads((tmp_path / "o" / "result.json").read_text())
    assert res["backend"] == "mock" and len(res["images"]) == 2
    assert (tmp_path / "o" / "render_overlay.png").is_file()
    assert "total $0.0000" in capsys.readouterr().out
