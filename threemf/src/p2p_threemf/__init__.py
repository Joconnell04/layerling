"""p2p_threemf: Bambu-compatible multicolor 3MF writer, independent reader, and slicer gate."""

__version__ = "0.1.0"

from .encoding import decode_summary, decode_tree, encode_state, encode_tree
from .reader import read_3mf, read_3mf_full
from .retarget import retarget_3mf
from .slicer import slice_check
from .writer import write_3mf

__all__ = [
    "__version__",
    "decode_summary",
    "decode_tree",
    "encode_state",
    "encode_tree",
    "read_3mf",
    "read_3mf_full",
    "retarget_3mf",
    "slice_check",
    "write_3mf",
]
