import numpy as np
import pytest
from p2p_threemf.encoding import (
    MAX_STATE,
    WHOLE_CODES,
    PaintCodeError,
    PaintNode,
    decode_summary,
    decode_tree,
    encode_state,
    encode_tree,
    leaf_areas,
    leaf_triangles,
    split_children,
)

# Whole-triangle codes. 1-5 match PLAN.md and mmu-remapper; 18+ is Bambu's 32-filament extension.
GOLDEN = {
    1: "4", 2: "8", 3: "0C", 4: "1C", 5: "2C", 6: "3C", 7: "4C", 8: "5C", 9: "6C", 10: "7C",
    11: "8C", 12: "9C", 13: "AC", 14: "BC", 15: "CC", 16: "DC", 17: "EC", 18: "0FC", 19: "1FC",
    31: "DFC", 32: "EFC",
}

# Split-triangle strings copied from a project saved by Bambu Studio 02.08.02.61 (Meshy model,
# brush painted), with the area per state our decoder derives.
BAMBU_SPLIT = {
    "0C442": {1: 0.75, 3: 0.25},
    "8444344434443": {1: 0.984375, 2: 0.015625},
    "80C0C0C30C0C0C30C0C0C30C0C0C3": {2: 0.00390625, 3: 0.99609375},
    "44444844344344343": {1: 0.99609375, 2: 0.00390625},
    "88884883883883883": {1: 0.00390625, 2: 0.99609375},
    "0C0C0C0C0C80C0C30C0C30C0C30C3": {2: 0.00390625, 3: 0.99609375},
}


@pytest.mark.parametrize(("state", "code"), sorted(GOLDEN.items()))
def test_golden_whole_codes(state, code):
    assert encode_state(state) == code
    assert decode_tree(code) == PaintNode(state=state)
    assert decode_summary(code) == (state, False, ((state, 1.0),))


def test_every_state_round_trips():
    assert encode_state(0) == ""
    assert len(WHOLE_CODES) == MAX_STATE
    for s in range(1, MAX_STATE + 1):
        assert decode_tree(encode_state(s)).state == s
    with pytest.raises(ValueError):
        encode_state(MAX_STATE + 1)


@pytest.mark.parametrize("code", sorted(BAMBU_SPLIT))
def test_real_bambu_split_codes(code):
    node = decode_tree(code)
    assert node.is_split
    assert encode_tree(node) == code  # our encoder reproduces Bambu byte for byte
    areas = leaf_areas(node)
    assert areas == pytest.approx(BAMBU_SPLIT[code])
    assert sum(areas.values()) == pytest.approx(1.0)
    majority, is_split, _ = decode_summary(code)
    assert is_split and majority == max(BAMBU_SPLIT[code], key=BAMBU_SPLIT[code].get)


def _random_tree(rng, depth=0) -> PaintNode:
    if depth < 4 and rng.random() < 0.45:
        sides = int(rng.integers(1, 4))
        special = 0 if sides == 3 else int(rng.integers(0, 3))
        return PaintNode(split_sides=sides, special_side=special,
                         children=[_random_tree(rng, depth + 1) for _ in range(sides + 1)])
    return PaintNode(state=int(rng.integers(0, MAX_STATE + 1)))


def test_random_trees_round_trip():
    rng = np.random.default_rng(7)
    for _ in range(500):
        tree = _random_tree(rng)
        code = encode_tree(tree)
        if code == "":
            assert tree == PaintNode(state=0)
            continue
        assert decode_tree(code) == tree
        assert encode_tree(decode_tree(code)) == code


@pytest.mark.parametrize("sides", [1, 2, 3])
@pytest.mark.parametrize("special", [0, 1, 2])
def test_split_geometry_matches_area_table(sides, special):
    if sides == 3 and special != 0:
        pytest.skip("3-side splits always use special side 0")
    tri = np.array([[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [1.0, 3.0, 0.0]])

    def area(t):
        return 0.5 * np.linalg.norm(np.cross(t[1] - t[0], t[2] - t[0]))

    kids = split_children(tri, sides, special)
    assert len(kids) == sides + 1
    fracs = [area(k) / area(tri) for k in kids]
    node = PaintNode(split_sides=sides, special_side=special,
                     children=[PaintNode(state=i + 1) for i in range(sides + 1)])
    assert leaf_areas(node) == pytest.approx({i + 1: f for i, f in enumerate(fracs)})
    # Children keep the parent's orientation (same normal direction).
    n = np.cross(tri[1] - tri[0], tri[2] - tri[0])
    for k in kids:
        assert np.dot(np.cross(k[1] - k[0], k[2] - k[0]), n) > 0
    leaves = leaf_triangles(node, tri)
    assert [s for _, s in leaves] == list(range(1, sides + 2))


def test_unpainted_parts_of_split_triangle():
    node = PaintNode(split_sides=1, special_side=0,
                     children=[PaintNode(state=0), PaintNode(state=4)])
    code = encode_tree(node)
    majority, is_split, areas = decode_summary(code)
    assert is_split
    assert dict(areas) == {0: 0.5, 4: 0.5}
    assert majority == 0  # tie breaks to the lower state; the reader maps 0 to the default


@pytest.mark.parametrize("bad", ["0c", "G", "C", "3", "0C0", "FFFFC"])
def test_invalid_codes_raise(bad):
    with pytest.raises(PaintCodeError):
        decode_tree(bad)
