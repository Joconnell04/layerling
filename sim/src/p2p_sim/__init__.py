"""p2p_sim: offscreen renderer, face-ID buffer, region geometry, and simulate_print.

Frame: contract frame (mm, Z up, front faces -Y, base on z = 0). Cameras: p2p_contracts.VIEWS.
"""

from .camera import Camera, camera_for_points, camera_matrices
from .mesh import Mesh, load_glb, load_mesh, load_ply, save_glb, save_ply, to_contract_frame
from .raster import Raster, get_backend, rasterize
from .regions import RegionGeometry, fill_regions, min_widths, region_geometry, views_seen
from .render import (
    RenderResult,
    best_region_camera,
    render,
    render_crops,
    render_sheet,
    render_views,
)
from .simulate import SimResult, simulate_print, simulate_print_detailed

__all__ = [
    "Camera",
    "Mesh",
    "Raster",
    "RegionGeometry",
    "RenderResult",
    "SimResult",
    "best_region_camera",
    "camera_for_points",
    "camera_matrices",
    "fill_regions",
    "get_backend",
    "load_glb",
    "load_mesh",
    "load_ply",
    "min_widths",
    "rasterize",
    "region_geometry",
    "render",
    "render_crops",
    "render_sheet",
    "render_views",
    "save_glb",
    "save_ply",
    "simulate_print",
    "simulate_print_detailed",
    "to_contract_frame",
    "views_seen",
]
