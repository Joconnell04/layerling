"""The stored jobs: real runs whose paid Meshy outputs the fixture store keeps."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parents[2]
REPO = EVALS_DIR.parent
FIXTURES = EVALS_DIR / "fixtures"  # manifest.json (committed) and store/ (gitignored)
REPORTS = EVALS_DIR / "reports"
DEFAULT_JOBS_DIR = REPO / "jobs"


@dataclass(frozen=True)
class StoredJob:
    label: str
    job_id: str
    what: str

    @property
    def short(self) -> str:
        return self.job_id[:8]


STORED_JOBS: tuple[StoredJob, ...] = (
    StoredJob("sombrero", "77032daf-2325-4061-ba6f-7703e1b0a303",
              "live run 2: golden in a sombrero, figurine on a round base"),
    StoredJob("catfish", "f67c6a54-82d8-4d2f-a2ae-acba2e9a8261",
              "live run 2: cat holding a fish, flat charm keychain"),
    StoredJob("bronco", "eb8f1de4-0f0b-41f9-a43f-d5017c3ea9eb",
              "live run 2: Bronco nameplate"),
    StoredJob("frenchie", "819cc491-c341-470a-bb97-63a194fa8dca",
              "M3: Frenchie keychain, thin slice"),
    StoredJob("bronco_m3", "1785a480-84b8-41f9-8f97-1005b6d7d0a6",
              "M3: Bronco nameplate, thin slice"),
)


def pick(names: list[str] | None) -> list[StoredJob]:
    """Stored jobs by label, id or id prefix; all of them when names is empty."""
    if not names:
        return list(STORED_JOBS)
    out = []
    for n in names:
        hit = [j for j in STORED_JOBS if n in (j.label, j.job_id) or j.job_id.startswith(n)]
        if not hit:
            raise SystemExit(f"unknown stored job {n!r}; known: "
                             f"{', '.join(j.label for j in STORED_JOBS)}")
        out += [j for j in hit if j not in out]
    return out
