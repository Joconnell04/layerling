# sam3 (p2p_sam3)

SAM 3 part labels for the paint stack: text prompts ("eye", "nose", "ear") on renders of the
mesh, masks back-projected to triangles through face-ID buffers and voted across views, then
coat markings split out of the body and head by texture color. Output is `face_regions.npy`
plus `p2p_contracts.Region` records.

Research, measurements and cost: `docs/research/sam3-runpod.md`.

## Interface

One call from rendered views to a region map, driven by `LabelRegionsIn`:

```python
from p2p_sam3.glb import read_glb, to_model_frame
from p2p_sam3.labeler import RenderedView, label_regions
from p2p_sam3.markings import face_colors_from_texture

mesh = to_model_frame(read_glb("model.glb"), height_mm=80)  # same face order as the GLB
views = [RenderedView("front", rgb, face_ids, camera_pos_mm=cam_pos), ...]  # from the renderer
res = label_regions(
    LabelRegionsIn(),  # prompts, views, min_vote, score_threshold, min_component_frac
    mesh.vertices,
    mesh.faces,
    views,
    subject_id="s1",
    prompt_map={"dog": ("s1", "body"), "base": (None, "base")},
    face_colors_lab=face_colors_from_texture(mesh),  # None skips markings
    budget_guard=guard,
    on_spend=ledger.append,
    job_id=job_id,
    stage=stage,
)
res.regions  # s1.head, s1.eye_left, ..., s1.marking_white_face, s1.marking_black_back
res.markings.region_colors  # region id -> {lab, hex, name} (not in the contract yet, see below)
res.save(attempt_dir, "s07_labels/a1", mesh_ref)  # face_regions.npy + region_map.json
```

`LabelRegionsIn.score_threshold` goes to the segmenter. `min_component_frac` drops 2D mask
components under that share of their mask, and face-label islands under that share of the
label's largest component. `views` filters the rendered views by name. `close_ups` and `replace`
are the caller's job (it renders close-ups as extra views; it merges into an existing map).

Lower level:

```python
from p2p_sam3.client import segment, get_segmenter
from p2p_sam3.backproject import ViewLabels, backproject, masks_from_result

res = segment(
    images,
    ["eye", "nose", "ear"],  # arrays (H, W, 3) uint8, PNG bytes, paths
    score_threshold=0.5,
    max_masks_per_prompt=10,
    budget_guard=guard,  # guard(est_usd): raise or return False to stop
    on_spend=ledger.append,
    job_id=job_id,
    stage=StageId.S07_BASELINE,
)
res.images[i].get("eye").decoded()  # list of (H, W) bool masks; .scores, .boxes (xyxy px)
res.spend  # list[SpendEvent] (Runpod only; mock emits a $0 test_mode one)
res.model_calls()  # list[ModelCall] for the stage manifest

views = [
    ViewLabels(face_ids=fid_v, masks=masks_from_result(res, v), camera_pos_mm=cam_v)
    for v, (fid_v, cam_v) in enumerate(buffers)
]
bp = backproject(vertices_mm, faces, views, min_vote=0.5)
bp.face_regions  # int32 (n_faces,), 0 = unlabeled
bp.regions  # list[Region]: eye_left, eye_right, nose, ...
bp.save(attempt_dir, "s07_baseline/a1", mesh_ref)  # writes face_regions.npy + region_map.json
```

Backends, chosen by `SAM3_BACKEND` (or `get_segmenter(name)`):

| Backend | What | Needs |
|---|---|---|
| `mock` (default) | Color and position heuristics on flat renders, or fixture masks | nothing |
| `local` | SAM 3 in-process on Apple MPS (float16), CUDA or CPU; one warm model per process | `--extra local`, weights at `SAM3_LOCAL_PATH` (default `~/models/sam3`) |
| `runpod` | Serverless endpoint built from `sam3/runpod/` | `RUNPOD_API_KEY`, `RUNPOD_SAM3_ENDPOINT_ID`; optional `SAM3_GPU_USD_PER_HOUR` (default 0.58), `SAM3_IDLE_TIMEOUT_S` (5) |

Local and Runpod run the same inference code (`sam3/runpod/handler.py`, which `p2p_sam3.local`
loads by path), so results match.

Runpod worker input: `{images: [base64 PNG], prompts: [str], score_threshold, max_masks_per_prompt,
mask_threshold?}`, at most 32 images and 32 prompts, sides 16 to 2048 px. Output per image and
prompt: `scores`, `boxes` (xyxy px), `areas`, `masks` (COCO RLE, pycocotools-compatible), plus
`timing`, `cold_start` and `model` (including the verified weights sha256). Errors return
`{"error": ...}` (a FAILED job).

Back-projection rules (details in the module docstring): votes are visible pixels weighted by
|cos| of the view angle; a label needs vote share >= `min_vote` on a face; specific parts
(pupil, eye, nose) beat broad ones (head, body) when their confidence is >= 0.35; unseen faces
and mask-edge speckle take the label of the nearest seen face over mesh adjacency, with
coincident vertices welded first (Meshy GLBs split vertices along every UV seam; without the weld
the beagle's base came out as 26 pieces). Per-subject rules follow (`rules.py`):

- Caps (`MAX_PER_SUBJECT`): 2 eyes and 2 ears, kept by confidence x area (drops the false "eye"
  on the beagle's back view, 0.60 and 0.57); 1 nose and 1 mouth, kept by facing front and
  closeness to the eyes (drops the Frenchie's back-view "nose" on the rear, which was bigger
  than the real one, and its extra mouths).
- Head fallback: body faces inside the hull of the face parts (eyes, nose, mouth, head; not
  ears), connected to them and above the collar, become head. SAM 3 scored "head" only from the
  sides on the proof-run Frenchie; on the beagle it moves the back of the head out of body.
- Feet: a paw piece that does not reach the ground (the Frenchie's hocks, 36 mm2, 3.7 mm above
  the hind paws) or a speck joins the nearest foot; pieces on the ground stay apart (Bronco's
  feet are 4 to 6 mm apart). Feet are named `paw_front_left`, `paw_front_right`,
  `paw_back_left`, `paw_back_right` for any count (extras get `_2`), front/back split by area.
- Sides: `_left` / `_right` are the subject's own sides. `subject_frame` takes forward from tail
  to nose; facing -Y that gives left = +X, the contract's convention, and a model sitting turned
  (Bronco, about 45 degrees) gets its own axes. The camera names in `VIEWS` use the viewer's
  sides instead, so the `left` camera sees `ear_right`.

`min_width_mm` is left None for the sim workstream.

Marking rules (`markings.py`): per-face CIELAB from the GLB base-color texture (median of 7
samples per triangle), edge-preserving smoothing, area-weighted k-means over all body and head
faces together, clusters merged within 12 CIEDE2000, clusters under 3% folded into the nearest.
A cluster whose color lies between two others, ramps from one to the other, and sits in a band
between them is baked shading and is split between them (the beagle's black-into-tan shoulders:
spread 0.45 of the black-to-tan line, band 8.4 mm). Pieces under 3% of their host fold into
their neighbors. A host keeps a color covering at least half of it; the other pieces become
`<subject>.marking_<color>_<position>` regions (`source="texture_cluster"`, `parent_ids=[host]`).
A host with no color on half of it (the beagle's tricolor body) retires into its markings; its
id is not reused. Positions come from the piece's mean normal: head uses face, crown, nape,
chin, cheek_left, cheek_right; body uses chest, back, belly, left, right, legs, sides. Color
names are distinct per subject (two grays become gray and silver). A piece that touches the
subject's collar in the collar's color (within 15 CIEDE2000) joins the collar, so a dark band
SAM 3's collar mask missed is not a marking; `collar` and `tag` are default prompts.

## Commands

```bash
uv run pytest sam3/tests                                   # 160 tests, no torch needed
uv run python -m p2p_sam3 --backend mock --images a.png --prompts eye nose ear --out /tmp/s

# Full label pass on a Meshy GLB: 4 p2p_sim texture renders, SAM 3, back-projection, markings,
# overlays. SAM 3 output is cached in --out/segments.json; --resegment reruns it.
# --renderer numpy uses the stand-in in tests/sam3_raster.py; --subject sets the whole-body prompt.
SAM3_BACKEND=local uv run --all-packages --all-extras python sam3/scripts/beagle_regions.py \
    --glb jobs/<job_id>/s05_mesh/a1/mesh_m1.glb --out sam3/_out/<name>

# Local SAM 3 (Apple MPS). First load about 10 s, then about 2.2 s per 1024 px image.
uv run --package p2p-sam3 --extra local python -m p2p_sam3 --backend local \
    --images a.png b.png c.png --prompts eye nose ear --out /tmp/s
uv run --package p2p-sam3 --extra local python sam3/scripts/local_bench.py --images a.png
SAM3_BACKEND=local uv run --package p2p-sam3 --extra local python sam3/scripts/chain_demo.py a.png

# Runpod endpoint (after deploy)
SAM3_BACKEND=runpod uv run python -m p2p_sam3 --images a.png b.png c.png --out /tmp/s
```

Measured here (Apple M5, MPS, float16): load 8 to 10 s, 0.95 s vision plus about 0.14 to 0.25 s
per prompt, 2.1 to 2.3 s per 1024 px image with 5 to 10 prompts. The chain demo on a flat
synthetic render returns ear_left, ear_right, eye_left, eye_right, head and nose regions.

On the Meshy beagle fixtures (concept at 1024 px, four mesh thumbnails at 512 px), eye, nose,
ear, paw, base and tail score 0.8 to 0.96 on both sizes; "black patch" and "white chest" work;
"white blaze", "black saddle", "white stripe" and "white muzzle" find nothing. Table in the
research doc; overlays in `sam3/_out/` (gitignored). Prompt with part nouns and "<color> patch",
not breed marking words. RGBA inputs are composited onto gray 235 (`SAM3_BACKGROUND`).

Full label pass on the Meshy beagle GLB (31,159 faces, 80 mm tall, 4 views at 1024 px, prompts
eye nose mouth ear paw tail head dog base; MEASURED with `scripts/beagle_regions.py`): SAM 3
11.7 s after an 8 s load, back-projection 0.2 s, markings 0.3 s. 21 regions: base; head, both
ears and the flanks tan; eye_left, eye_right, nose and saddle black; the four paws named front
and back; `marking_white_face` (blaze and muzzle, 931 mm2), `marking_white_chest` (2,316 mm2),
`marking_black_back` (the saddle, 2,712 mm2), `marking_tan_left`, `marking_tan_right`; three
mouth pieces and two tail pieces. Texture clusters: black, tan, white; the dark brown band where
the saddle fades into tan was dissolved. Overlays: `sam3/_out/beagle_regions/sheet.png` (region
colors and labels on top, each region in its texture color below, legend at right),
`regions_<view>.png`, `flat_<view>.png`. With the proof-run rules the beagle keeps one mouth
and its head grows from 1,146 to 1,396 mm2 (the back of the head).

Proof-run meshes (MEASURED, `p2p_sim` texture renders, prompts add collar and tag; outputs in
`sam3/_out/frenchie/` and `sam3/_out/bronco/`):

| | Frenchie (919,464 faces) | Bronco (532,360 faces) |
|---|---|---|
| SAM 3, 4 views | 10.6 s | 12.3 s |
| Back-projection, markings | 2.2 s, 7 s | 1.0 s, 4 s |
| Nose, mouth | 1, 1 (rear "nose" and 2 extra mouths dropped) | 1, 1 |
| Head | front view 0.76, plus 29,206 faces from the fallback | plus 11,425 faces; no longer retired |
| Paws | 6 pieces to 4 feet, front/back and left/right | 5 pieces to 4 feet in the dog's own frame |
| Collar, tag | collar 541 mm2 (0.88 to 0.93), tag 7.7 mm2 | none |
| Markings | black saddle, gray patches, silver face | tan and cream on the face, chest, belly |

## Deploy steps (need approval; nothing has been created)

Serverless endpoint:

1. Runpod console > Serverless > New Endpoint > Import Git Repository. Branch `main`,
   Dockerfile path `sam3/runpod/Dockerfile`. Base image `pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime`;
   the build installs `transformers==5.17.0` and `runpod==1.12.0` and checks `Sam3Model` imports.
2. Model (cached models): `facebook/sam3` with an HF token from Jackson's approved account.
3. GPUs: 16 GB (A4000 class, $0.58/hr) first, 24 GB ($0.69/hr) second. Allowed CUDA 12.8+.
4. Workers: active 0, max 1 (raise later), idle timeout 5 s, execution timeout 300 s,
   FlashBoot on. Env: none required. Optional `SAM3_DTYPE=bfloat16` if memory is tight;
   `HF_TOKEN={{ RUNPOD_SECRET_hf_token }}` only if the cached model is not set (then the worker
   downloads 3.44 GB at each cold start, billed).
5. Put the endpoint id in `.env` as `RUNPOD_SAM3_ENDPOINT_ID`, then run the Runpod command above
   on 3 Meshy renders (M2 verification 2) and check the overlays.

Quick GPU pod test instead of an endpoint (same code, no build):

```bash
# Pod: 16 or 24 GB GPU, image runpod/pytorch:1.3.2-cu1281-torch280-ubuntu2404, env HF_TOKEN set.
pip install -r sam3/runpod/requirements.txt
cd sam3/runpod && python smoke.py --images r1.png r2.png r3.png --prompts eye nose ear --out /workspace/smoke
```

`smoke.py` loads the model, runs the request twice (cold then warm), prints timing and writes
`result.json` plus one overlay PNG per image and prompt. Expected cost of either test: under $0.05.

## Known gaps

- The CUDA path has not run on a GPU. The same TorchSam3 code ran on MPS; CUDA differs only in
  device selection, TF32 flags and synchronize.
- The Docker image has not been built (no Docker here). Runpod's GitHub builder does not document
  its build context; the Dockerfile handles repo root or `sam3/runpod/`.
- The run script renders with `p2p_sim.render`; the tests still use the numpy stand-in in
  `tests/sam3_raster.py` so they need no GL.
- At 1024 px a 919k-face mesh has far more faces than pixels: 36% of the Frenchie's faces win
  a pixel in some view and the rest take the nearest seen face's label. Close-ups would help.
- The Frenchie's neck above the collar stays `body_2` (outside the face-part hull).
- Markings need a base-color texture. Vertex colors (`COLOR_0`) are not read; untextured
  materials use `baseColorFactor` only.
- A narrow stripe between two colors is treated as baked shading only when its colors also ramp
  from one to the other; a flat stripe stays a marking (tested). Real fur with a strong painted
  gradient between two coat colors would be split at the midpoint.
- GPU speed and cost per call are estimates (see research doc) until the endpoint runs.
- The ungated fallback (OWLv2 boxes plus SAM 2.1 masks) is researched but not built; access to
  facebook/sam3 was approved.
- The mock heuristics only work on flat renders with a plain background.
- Region boundaries follow triangle edges; no boundary smoothing or crease snapping (paint tools).

## Contract change requests

1. `regions.Region`: add `color_lab: Vec3 | None` (median texture color of the region's faces)
   so the paint agent and `suggest_palette` can match filaments without re-sampling the texture.
   Workaround: `MarkingResult.region_colors` in memory and `region_colors.json` next to the
   region map in the beagle script.
2. `mcp_tools.LabelRegionsIn`: add `markings: bool = True` to turn texture markings off for a
   single-color subject. Workaround: the caller passes `face_colors_lab=None`.
3. `regions.VIEWS` comment: say the camera names use the viewer's sides (the `left` camera sits
   at -X and sees the subject's right side), unlike region labels, which use the subject's.
