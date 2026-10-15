import json
from datetime import UTC, datetime

from p2p_contracts import Manifest, StageId, StageStatus, export
from p2p_contracts.payloads import MESSAGE_PAYLOADS
from p2p_contracts.stages import STAGES


def test_schemas_up_to_date():
    for name, schema in export.build().items():
        path = export.SCHEMA_DIR / f"{name}.schema.json"
        assert path.exists(), f"missing {path}; run python -m p2p_contracts.export"
        assert json.loads(path.read_text()) == schema, f"{name} stale; re-export"


def test_manifest_roundtrip():
    m = Manifest(job_id="j", stage=StageId.S03_CONCEPT, attempt=1, status=StageStatus.RUNNING,
                 started_at=datetime.now(UTC))
    assert Manifest.model_validate_json(m.model_dump_json()) == m


def test_stage_graph_is_acyclic_and_ordered():
    seen = set()
    for s in STAGES:
        assert set(s.requires) <= seen
        seen.add(s.id)


def test_sql_enums_match():
    mig = export.SCHEMA_DIR.parents[1] / "supabase/migrations"
    sql = "\n".join(p.read_text() for p in sorted(mig.glob("*.sql")))
    for sid in StageId:
        assert f"'{sid.value}'" in sql
    for st in StageStatus:
        assert f"'{st.value}'" in sql
    for kind in MESSAGE_PAYLOADS:
        assert f"'{kind}'" in sql
