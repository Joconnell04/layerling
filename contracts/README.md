# contracts

Source of truth for every interface between workstreams. Pydantic v2 models in
`src/p2p_contracts/`; JSON Schemas generated into `schema/` for the TypeScript side.

| Module | Defines |
|---|---|
| stages.py | StageId, StageStatus, JobStatus, the stage graph, pipeline profiles |
| manifest.py | Manifest and its parts (FileRef, ModelCall, MeshyTask, Decision, StageError) |
| job.py | JobRecord, PrintSettings, Budget, SpendEvent, caps, job-folder path helpers |
| payloads.py | Stage payloads (IntakeResult, ColorSpec, concepts, mesh review, JobResult) and the `messages.kind` registry |
| regions.py | RegionMap, Region, PaintState, geometry frame, fixed cameras |
| filaments.py | FilamentLibrary, Filament |
| reports.py | SimReport, SliceReport, Checklist |
| mcp_tools.py | Paint MCP tool inputs and outputs (TOOLS registry) |

Supabase schema: `supabase/migrations/20260923000001_init.sql`. A test checks its enums match.

## Change process

Only the orchestrator edits this package or the migration. After a change:

```bash
uv run python -m p2p_contracts.export && uv run pytest contracts/tests
```

Workstreams: if you need a change, add it to "Contract change requests" in your own README with
the reason, and work around it until it lands.
