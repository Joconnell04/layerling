import numpy as np
import pytest
from p2p_sam3 import rle


def _masks():
    rng = np.random.default_rng(7)
    yield np.zeros((5, 7), bool)
    yield np.ones((5, 7), bool)
    m = np.zeros((4, 4), bool)
    m[0, 0] = True
    yield m
    m = np.zeros((6, 9), bool)
    m[2:5, 3:8] = True
    yield m
    for _ in range(50):
        h, w = rng.integers(1, 64, size=2)
        yield rng.random((h, w)) < rng.random()
    yield rng.random((1024, 1024)) < 0.2


@pytest.mark.parametrize("mask", list(_masks()))
def test_round_trip(mask):
    enc = rle.encode(mask)
    assert enc["size"] == list(mask.shape)
    assert isinstance(enc["counts"], str)
    dec = rle.decode(enc)
    assert dec.dtype == bool and dec.shape == mask.shape
    assert np.array_equal(dec, mask)
    assert rle.area(enc) == int(mask.sum())


def test_known_vector_column_major():
    # Column-major: column 0 is (0, 1), column 1 is (1, 1) -> runs 1 zero, 3 ones.
    m = np.array([[0, 1], [1, 1]], bool)
    assert list(rle.runs(m)) == [1, 3]
    # Starts with foreground -> leading zero-length run.
    assert list(rle.runs(np.ones((2, 2), bool))) == [0, 4]


def test_uncompressed_counts_accepted():
    m = np.array([[0, 1], [1, 1]], bool)
    assert np.array_equal(rle.decode({"size": [2, 2], "counts": [1, 3]}), m)


def test_matches_pycocotools_fixtures():
    # Strings produced by pycocotools 2.0.11 mask.encode for these masks.
    m = np.zeros((6, 9), bool)
    m[2:5, 3:8] = True
    assert rle.encode(m)["counts"] == "d03300000004"
    assert rle.string_to_counts("d03300000004") == [20, 3, 3, 3, 3, 3, 3, 3, 3, 3, 7]
    r = np.array(
        [
            [1, 1, 0, 0, 1, 1],
            [1, 1, 0, 1, 1, 0],
            [1, 0, 0, 0, 1, 0],
            [0, 1, 1, 0, 1, 1],
            [0, 0, 1, 0, 1, 0],
        ],
        bool,
    )
    assert rle.encode(r)["counts"] == "032OOO31MO25OKO"
    assert np.array_equal(rle.decode({"size": [5, 6], "counts": "032OOO31MO25OKO"}), r)


def test_bad_rle_rejected():
    with pytest.raises(ValueError):
        rle.decode({"size": [2, 2], "counts": [1, 2]})
    with pytest.raises(ValueError):
        rle.decode({"size": [2, 2], "counts": "\x7f"})


def test_bbox():
    m = np.zeros((10, 10), bool)
    m[2:4, 5:9] = True
    assert rle.bbox(m) == [5.0, 2.0, 9.0, 4.0]
    assert rle.bbox(np.zeros((3, 3), bool)) is None
