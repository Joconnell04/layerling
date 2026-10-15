"""Write JSON Schemas for every contract model to contracts/schema/. Run: uv run python -m p2p_contracts.export"""

import json
from pathlib import Path

from pydantic import BaseModel

from . import filaments, geometry, job, manifest, mcp_tools, payloads, regions, reports

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"

MODELS: dict[str, type[BaseModel]] = {
    "manifest": manifest.Manifest,
    "job": job.JobRecord,
    "spend_event": job.SpendEvent,
    "region_map": regions.RegionMap,
    "paint_state": regions.PaintState,
    "filament_library": filaments.FilamentLibrary,
    "sim_report": reports.SimReport,
    "slice_report": reports.SliceReport,
    "checklist": reports.Checklist,
    "intake_result": payloads.IntakeResult,
    "color_spec": payloads.ColorSpec,
    "product_spec": payloads.ProductSpec,
    "view_set": payloads.ViewSet,
    "paint_preview": payloads.PaintPreview,
    "geometry_result": geometry.GeometryResult,
    **{f"msg_{k}": m for k, (_, m) in payloads.MESSAGE_PAYLOADS.items() if m is not None},
}


def build() -> dict[str, dict]:
    out = {name: m.model_json_schema() for name, m in MODELS.items()}
    out["mcp_tools"] = {
        name: {
            "input": i.model_json_schema() if i else {"type": "object", "properties": {}},
            "output": o.model_json_schema(),
        }
        for name, (i, o) in mcp_tools.TOOLS.items()
    }
    return out


def main() -> None:
    SCHEMA_DIR.mkdir(exist_ok=True)
    for name, schema in build().items():
        (SCHEMA_DIR / f"{name}.schema.json").write_text(json.dumps(schema, indent=2) + "\n")
    print(f"wrote {len(build())} schemas to {SCHEMA_DIR}")


if __name__ == "__main__":
    main()
