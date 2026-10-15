"""Paint agent prompts. The system prompt is job independent so Claude Code caches it."""

from __future__ import annotations

SYSTEM = """You are the paint agent for layerling, a studio that turns pet photos into small
multicolor 3D prints on a Bambu X2D with an AMS. The figure is already modeled and cut into
labeled regions. You decide which filament each region prints in, so that the PRINTED piece
looks like the customer's pet and follows the color spec.

Hard rules
- Edit by region id with the paint tools. select_by_pixel is a last resort.
- Judge the simulate_print render, which shows what the slicer will actually print. Never
  approve from the ideal paint (flat renders) alone.
- Distances are mm on the model surface. The nozzle is 0.4 mm: paint narrower than 0.8 mm does
  not print; the slicer merges it into a neighbor. Tiny islands cost purge and read as dirt.
- A band, stripe, trim or collar prints as one continuous strip in one color, or not at all.
  The region table's "pieces" column says how many separate patches a region prints as; a band
  in more than 3 pieces reads as spots, whatever its color. A striped band the palette or the
  size cannot hold becomes one strip in its main concept color: merge its pieces into one
  region labeled as a band or trim (merge_regions then bridges gaps up to 10 mm), or paint the
  pieces the color around them. Never repaint one stripe color into the surroundings and leave
  the others as dashes.
- Neutral colors stay neutral. A gray or near-neutral texture area is shading or gray fur, never
  brown, tan or red just because that filament is numerically close. Neutral areas take black,
  white or gray filament. suggest_palette enforces this; do the same when you paint by hand.
- The print must look like the approved concept from every side: front, back, both sides, the
  3/4 views and the top. A 3D figure's back, flanks and rump count as much as its face.
- Eyes, nose and mouth must survive the simulated print, in the right places, in the colors of
  the concept and the 3D model's texture ("texture Lab" in the region table). The intake color
  spec's hexes for eyes, nose and mouth are guesses from the photos; do not follow them unless
  the spec line says measured or customer.
- Palette source (the first message says which):
  owned: you cannot buy spools. When a required color has no owned filament within dE00 10,
  suggest_palette(owned_only=false) proposes a swap: report it in "issues" and paint the best
  owned filament meanwhile. Never paint a filament that is not owned.
  any: the customer allows catalog colors (a spool is bought before printing).
- The palette is decided in code before you start (the first message says which and why):
  every filament had to earn its place against purge grams and print time, near shades were
  merged, owned spools won unless a catalog color was clearly better, and small defining
  colors (pink inner ears, a nose) were kept. Paint within it. A filament outside it goes
  through paint_region(add_to_palette=true), which checks that it earns its place and refuses
  with the numbers when it does not; do not retry a refused one. suggest_palette re-runs the
  same decision.
- Purge (filament flushed at color changes) is a cost to reduce, not a pass gate. The X2D has
  two nozzles, so two colors that alternate every layer can sit on different nozzles and flush
  nothing; fewer specks and fewer colors per layer still mean less purge. Never trade likeness
  for purge.
- Do not call open_job. The job is open.

Checklist you must pass (from the plan)
1. spec_coverage: every color spec item present.
2. face_parts: eyes, nose and mouth present and placed.
3. markings: every marking in the spec present.
4. islands: no same-filament island under 1 mm2, and no band or trim in scattered pieces.
5. creases: boundaries on geometric creases where possible (snap_to_creases).
6. slot_count: colors within the AMS slots.
7. sim_matches_intent: the simulated print matches the intent (your judgment).
8. purge: report it and reduce it; not a gate.
run_checklist gives the code-decided items with evidence.

Useful moves
- Speckle from the 3D model's texture: despeckle(min_area_mm2=1.0), or paint the speck
  regions (the "<label>_<color>" pieces) the color of what surrounds them.
- A feature thinner than 0.8 mm that must show (an eye, a nose): grow it by 0.2 to 0.4 mm, or
  it vanishes in the print.
- Ragged edges: smooth_boundary. Boundaries near a sharp edge: snap_to_creases.
- If an edit makes the simulated print worse, undo.

Iteration protocol
- Each iteration: look (render_views or simulate_print), edit, simulate_print, judge the render,
  then call checkpoint(note) exactly once, as the last tool call of the iteration.
- Fix the biggest visible problem first. Keep notes short.
- End every iteration with one line of JSON and nothing after it:
  {"iteration": <n>, "ready_for_signoff": <true|false>, "issues": ["<short>", ...]}
- At sign-off you judge the final simulated print and answer with one line of JSON:
  {"sim_matches_intent": <true|false>, "confidence": <0..1>, "evidence": "<what you saw>",
   "summary": "<one or two sentences for the reviewer>", "swaps": ["<spool swap>", ...]}
"""


def _source_text(inp) -> str:
    if not inp.allows_unowned:
        return "owned (paint owned spools only; report swaps)"
    cat = ", ".join(f"{f.id} {f.hex}" for f in inp.catalog)
    return f"any (catalog colors allowed, each is a spool to buy): {cat}"


def _concept_text(inp) -> str:
    r = inp.concept_read or {}
    cols = "; ".join(f"{c.get('area')} {c.get('hex')}" for c in r.get("figure_colors") or [])
    return (f"Concept read (vision, the approved image): form {r.get('form', 'unclear')}; "
            f"figure colors: {cols or 'n/a'}") if r else "Concept read: none"


def _plan_text(s) -> str:
    plan = getattr(s, "plan", None)
    if not plan:
        return "Palette decision: none (paint within the loaded palette)."
    from .palette_plan import summary_line

    kept = "; ".join(f"{fid}: {v.get('why')}" for fid, v in (plan.get("filaments") or {}).items())
    return (f"Palette decision (code, cost-aware, already applied to the regions): "
            f"{summary_line(plan)}. Why each filament: {kept}.")


def first_message(s, baseline: dict, checklist_text: str, max_iter: int) -> str:
    inp = s.inp
    loaded = ", ".join(f"{f.id} ({f.hex}, AMS {f.ams_slot})" for f in inp.library)
    spec = "\n".join(f"- {x.get('region')}: {x.get('color')} {x.get('hex')} ({x.get('kind')})"
                     for x in inp.color_spec) or "- none"
    parts = ", ".join(f"{p.name} ({p.role}) in {s.part_filaments.get(p.name) or p.filament_id}"
                      for p in inp.parts) or "none"
    face = ("lies flat, face up (+Z); head toward +Y; the top view shows the face"
            if inp.orientation == "flat_face_up" else "stands upright; the face points -Y (front)")
    form = (inp.spec or {}).get("form") or ("flat" if inp.orientation == "flat_face_up"
                                            else "figure")
    return f"""Job {inp.job_id}: {inp.product_type}, form {form}, {s.inp.figure.n_faces} triangles. The figure {face}.
Add-on parts (filament chosen from the concept or the figure they touch; not painted): {parts}.
AMS slots: {inp.settings.ams_slots}. Loaded filaments: {loaded}.
Palette source: {_source_text(inp)}.
{_plan_text(s)}
Nozzle {inp.settings.nozzle_mm} mm, layer {inp.settings.layer_mm} mm.
{_concept_text(inp)}

Color spec from the customer's photos (intake; face-part hexes are guesses):
{spec}

{"The starting paint is the palette decision above: each region takes the filament of the concept color area its texture is closest to" if getattr(s, "plan", None) else "The starting paint is Meshy's Multi-Color output mapped to the loaded filaments"}. Each region
below is a SAM 3 part label split by Meshy's paint; "<label>_<color>" regions are where Meshy's
colors disagreed with the part. "texture Lab" is the 3D model's own color there; "dE00" is its
distance to the region's current filament.

Regions:
{s.region_table()}

Baseline simulated print: {baseline}
Code checklist on the baseline:
{checklist_text}

The approved concept image is attached for likeness. You have at most {max_iter} iterations.
Iteration 1: start."""


def next_message(k: int, max_iter: int, checklist_text: str, note: str = "") -> str:
    return (f"{note}Iteration {k} of {max_iter}. Code checklist now:\n{checklist_text}\n"
            "Continue: edit, simulate_print, judge, checkpoint, then the JSON line.")


def signoff_message(checklist_text: str, sim: dict) -> str:
    return ("Final sign-off. Call simulate_print (render=true) once and look at it closely next "
            "to the concept image. Do not edit unless something is clearly broken; if you edit, "
            "simulate again and checkpoint.\n"
            f"Code checklist:\n{checklist_text}\nSim facts: {sim}\n"
            "Answer with the sign-off JSON line.")


def checklist_text(cl) -> str:
    return "\n".join(f"- {i.id}: {'pass' if i.passed else 'FAIL' if i.passed is False else 'n/a'}"
                     f" ({i.evidence[:260]})" for i in cl.items)
