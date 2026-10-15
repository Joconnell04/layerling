# evals (p2p_evals)

Two things live here: the golden set (`golden/`, 20 casual requests with photos, for live
runs) and the replay suite, which reruns jobs Jackson already paid for at 0 credits.

## Fixture store

Every Meshy call the stored jobs paid for, kept forever:

| Job | Id | Meshy tasks | Credits live (SOURCE: manifests) |
|---|---|---|---|
| sombrero (live run 2, figurine) | 77032daf | 9 | 96 |
| catfish (live run 2, flat charm) | f67c6a54 | 8 | 73 |
| bronco (live run 2, nameplate) | eb8f1de4 | 14 | 159 |
| frenchie (M3, thin slice keychain) | 819cc491 | 5 | 66 |
| bronco_m3 (M3, thin slice nameplate) | 1785a480 | 5 | 66 |

- `fixtures/manifest.json` (committed): per job, each Meshy task: stage, attempt, step key,
  endpoint, purpose, status, live credits, the request as the manifest kept it, the request
  hash the replay matches on, and every file downloaded from it (the task field it came from,
  job path, sha256). Then the job's seed files with sha256.
- `fixtures/store/<job_id>/` (gitignored, 348 MB): `meshy/<task_id>.json` (the full task
  object) and `files/<job path>` (the Meshy outputs, plus the seed: job.json, photos and
  request, the accepted s01 to s05 attempts, and the stored run's small s06 to s10 outputs
  and call traces).

`uv run python -m p2p_evals fixtures build` rebuilds it from `jobs/` (read only);
`fixtures verify` re-hashes it. Job folders are never written.

## Replay Meshy backend (MESHY_MODE=replay)

`worker/src/p2p_worker/fakes/replay.py`. An httpx transport that answers every Meshy call
from the store, costs 0 credits and never reaches the network. A create matches, in order:

1. the input hash: endpoint plus body, with every image or model reference replaced by the
   sha256 of the job file behind it (so a data URI, a signed Storage URL and a `storage:` ref
   of one file hash the same);
2. the same source job, stage, step key (read from the attempt's `meshy_pending`) and
   endpoint, when the inputs changed (a new prompt): the answer lists the fields that differ;
3. an exact input hash already served in this run (a retry attempt).

Anything else raises `ReplayMiss` (VendorError, code validation, not retryable): the stage
fails and names the call. Answers keep the recorded task id (chained `input_task_id`s line
up), report 0 consumed credits, are stamped as just finished, and serve the stored files.
`P2P_REPLAY_STORE` points at another fixtures folder; `P2P_REPLAY_JOBS=new=src,...` maps
running job ids to stored ones.

Hook in the worker (kept small, beside robust/credit-guard's Meshy client work):
`config.py` accepts `MESHY_MODE=replay`; `runtime.py` builds `ReplayMeshy` and a
`MeshyClient(test_mode=True)` on its transport. `clients/meshy.py` is untouched; if its
constructor changes, the replay branch in `runtime.py` needs the same change.

## Replay suite

```bash
uv run python -m p2p_evals replay                      # all five, from s06, about 30 min
uv run python -m p2p_evals replay --from s01 --job sombrero --job catfish --job bronco
uv run python -m p2p_evals replay --from s01 --claude login   # real Haiku and Sonnet calls
uv run python -m p2p_evals bench-s06 --job sombrero    # s06 build before and after the speedup
```

Run long ones with nohup; the report is rewritten after every job.

- `--from s06` (default): a scratch copy of the job with its stored s01 to s05; s06 onward
  run fresh (s06, s07 on a thin slice; s06 to s10 on full).
- `--from s01`: a new job from the same photos, request and order form; gates get the stored
  run's own replies (answers, concept pick, mesh decision), adjusted and noted when a
  question or candidate is new.
- `--claude recorded` (default, for CI): `recorded_claude.py`, the Claude Code client with
  its subprocess replaced by the stored run's replies from the trace bodies, so parsing and
  finalize code run for real; a call with no recording fails loudly. `login`: the Claude
  Code login (API keys are stripped from the child; the turnkey key is never read). `mock`:
  MockClaude.
- `--paint replay` (default): `paint_replay.py` writes the paint the stored run shipped
  (same faces, so the same region ids) and s10 writes and gates it, so time and grams compare
  with the stored run. `stub`: s08's first pass. `agent`: the real paint agent (slow).
- The Bambu Studio CLI gates headless, one job at a time; tests stub it.

Output: `reports/replay-from-<s06|s01>.json` and `.md`. Per job: status; Bambu gate (or
p2p_slicer where Bambu is missing); form (job, concept, concept read, build, stored build);
concept check (the approved candidate; stored when s03 did not rerun); final-vs-concept check
(the paint agent's, or the stored verdict when the paint is replayed); slots, print time and
grams against the stored result; s06 output against the stored s06 (identical files, within
1 micron, or differs); wall time per stage against the stored stage; every Meshy and Claude
answer with how it matched; credits booked (0).

## Limits

- Only paid calls replay. A flow that now asks for something the stored run never bought
  misses: the M3 jobs predate s04 in the thin slice and the add-on edits in s03, so they
  replay from s06 only.
- Stored s01 to s05 wall times include customer gate waits and Meshy queues.
- The golden set has no recordings; it needs live Meshy.
