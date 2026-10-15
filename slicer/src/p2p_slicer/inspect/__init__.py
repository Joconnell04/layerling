"""Headless inspector (docs/research/native-slicer.md section 15): inspection.json, renders and a
static index.html for a project 3MF, a sliced .gcode.3mf (Bambu's or ours) or a .gcode file.

    from p2p_slicer.inspect import inspect_path
    insp = inspect_path("final.3mf", "out/")  # slices with p2p_slicer, else the Bambu CLI
"""

from .build import inspect_path

__all__ = ["inspect_path"]
