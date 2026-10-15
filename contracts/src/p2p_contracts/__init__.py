"""photo2print shared contracts. Change these only through the orchestrator (see contracts/README.md)."""

from . import filaments, geometry, job, manifest, mcp_tools, payloads, regions, reports, stages
from .filaments import Filament, FilamentLibrary
from .job import JobRecord, PrintSettings, SpendEvent
from .manifest import Decision, FileRef, InputRef, Manifest, MeshyTask, ModelCall, StageError
from .regions import PROOF_VIEWS, VIEWS, CameraSpec, MeshRef, PaintState, Region, RegionMap
from .reports import Checklist, SimParams, SimReport, SliceReport
from .stages import STAGES, JobStatus, StageId, StageStatus

__all__ = [
    "PROOF_VIEWS",
    "STAGES",
    "VIEWS",
    "CameraSpec",
    "Checklist",
    "Decision",
    "Filament",
    "FilamentLibrary",
    "FileRef",
    "InputRef",
    "JobRecord",
    "JobStatus",
    "Manifest",
    "MeshRef",
    "MeshyTask",
    "ModelCall",
    "PaintState",
    "PrintSettings",
    "Region",
    "RegionMap",
    "SimParams",
    "SimReport",
    "SliceReport",
    "SpendEvent",
    "StageError",
    "StageId",
    "StageStatus",
    "filaments",
    "job",
    "manifest",
    "mcp_tools",
    "payloads",
    "regions",
    "reports",
    "stages",
]
