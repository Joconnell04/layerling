"""The product the customer approved, as s06 and later stages need it: form (full 3D figure or
flat charm), plate shape, add-on color picks and the palette source.

The front half (fidelity/front, merged) sets the form at intake: `JobRecord.form` and s01's
product.json (payloads.ProductSpec). read_spec reads those first and records where each value
came from (the s06 output product_resolved.json), with a clear default:

- form: `JobRecord.form`, then s01 product.json `form`, then the older defensive reads
  (job.json, input/request.json `form` / `product_form` / `keychain_form` /
  `product_spec.form`, the answers' form questions). Default: none here, so p2p_geometry
  applies its product default (the full figure; flat only for magnets).
- plate_shape: the nameplate add-on's params.shape (set by the plate_shape answer; also in
  product.json's add-ons), then product.json picks `plate_shape`, then job.json /
  request.json `plate_shape`. Default rounded (geometry).
- colors (role -> hex or filament id; roles plate, text, loop, back, base): job.json /
  request.json `addon_colors` {role: value} or `<role>_color` (`name_color` is text), then the
  answers (question ids `<role>_color`, `name_color`; an option id or the free text).
  Values: #RRGGBB, a filament id, or a plain color name. Default: none (the concept read,
  then the figure's own colors, see p2p_geometry.addon_color).
  TODO(front): intake has no plate color or name color question yet; when one lands, add its
  question id here (the answers are already read by `<role>_color`).
- palette_source: job.json, request.json, then the jobs row. Default owned.
  TODO(fidelity/front): read JobRecord.palette_source (worker README contract request 6).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from p2p_contracts import StageId

ROLES = ("plate", "text", "loop", "back", "base")
FORM_KEYS = ("form", "product_form", "keychain_form", "magnet_form")
FORM_QUESTIONS = ("form", "product_form", "keychain_form", "magnet_form")
# Plain color names an answer may carry (ASSUMED hexes, close to Bambu PLA Basic and Matte).
NAMED = {
    "white": "#FFFFFF", "ivory": "#F4F0E6", "cream": "#EDE3CC", "bone": "#CBC6B8",
    "beige": "#F7E6DE", "tan": "#AE835B", "caramel": "#AE835B", "brown": "#7C4B00",
    "chocolate": "#4D3324", "dark brown": "#4D3324", "black": "#161616", "gray": "#8E9089",
    "grey": "#8E9089", "light gray": "#D1D3D5", "dark gray": "#545454", "pink": "#E8AFCF",
    "red": "#C12E1F", "orange": "#FF6A13", "yellow": "#F4EE2A", "gold": "#E4BD68",
    "green": "#00AE42", "blue": "#0A2989", "light blue": "#8FBBE6", "navy": "#0C2340",
    "purple": "#5E43B7", "silver": "#A6A9AA",
}


@dataclass
class ProductSpec:
    form: str | None = None
    form_source: str = "not set (geometry default)"
    plate_shape: str | None = None
    plate_shape_source: str = "not set (rounded)"
    colors: dict[str, str] = field(default_factory=dict)  # role -> #hex or filament id
    color_sources: dict[str, str] = field(default_factory=dict)
    palette_source: str = "owned"
    palette_source_from: str = "default"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _read(p: Path) -> dict:
    try:
        d = json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _nested(d: dict, key: str):
    if key in d:
        return d[key]
    for box in ("product_spec", "spec", "product"):
        inner = d.get(box)
        if isinstance(inner, dict) and key in inner:
            return inner[key]
    return None


def product_json(job_dir: Path) -> dict:
    """s01's product.json (payloads.ProductSpec) of the newest attempt that has one."""
    d = Path(job_dir) / StageId.S01_INTAKE.value
    atts = sorted((p for p in d.glob("a*") if p.name[1:].isdigit()),
                  key=lambda p: int(p.name[1:]), reverse=True) if d.is_dir() else []
    for a in atts:
        pj = _read(a / "product.json")
        if pj:
            return pj
    return {}


def latest_answers(job_dir: Path) -> list[dict]:
    """The s01 answers of the newest attempt that has them."""
    d = Path(job_dir) / StageId.S01_INTAKE.value
    atts = sorted((p for p in d.glob("a*") if p.name[1:].isdigit()),
                  key=lambda p: int(p.name[1:]), reverse=True) if d.is_dir() else []
    for a in atts:
        ans = _read(a / "answers.json").get("answers")
        if ans:
            return list(ans)
    return []


def _answer(answers: list[dict], ids: tuple[str, ...]) -> tuple[str | None, str | None]:
    for a in answers:
        if a.get("question_id") in ids:
            opt = (a.get("option_ids") or [None])[0]
            return (opt or a.get("text")), a.get("question_id")
    return None, None


def color_value(value, known_ids: set[str] | None = None) -> str | None:
    """#RRGGBB, a known filament id, or a named color's hex; None for anything else."""
    if value is None:
        return None
    v = str(value).strip()
    from .concept_read import normalize_hex

    hx = normalize_hex(v)
    if hx:
        return hx
    if known_ids and v in known_ids:
        return v
    name = v.lower().replace("_", " ").replace("-", " ")
    return NAMED.get(name)


def read_spec(job_dir: Path, job, store=None, known_ids: set[str] | None = None) -> ProductSpec:
    """Everything s06 needs about the approved product (module doc). `job` is the JobRecord."""
    from p2p_geometry.product import normalize_form

    job_dir = Path(job_dir)
    jj = _read(job_dir / "job.json")
    req = _read(job_dir / "input" / "request.json")
    answers = latest_answers(job_dir)
    pj = product_json(job_dir)
    spec = ProductSpec()

    f = normalize_form(getattr(job, "form", None))
    if f:
        spec.form, spec.form_source = f, "job form"
    f = normalize_form(pj.get("form"))
    if f and spec.form is None:
        who = (pj.get("decided_by") or {}).get("form")
        spec.form, spec.form_source = f, "s01 product.json" + (f" ({who})" if who else "")
    for src, d in (("job.json", jj), ("input/request.json", req)):
        for k in FORM_KEYS:
            f = normalize_form(_nested(d, k))
            if f and spec.form is None:
                spec.form, spec.form_source = f, f"{src} {k}"
    if spec.form is None:
        val, qid = _answer(answers, FORM_QUESTIONS)
        f = normalize_form(val)
        if f:
            spec.form, spec.form_source = f, f"answer {qid}={val}"

    for a in getattr(job, "addons", []) or []:
        shape = (a.params or {}).get("shape")
        if shape and spec.plate_shape is None:
            spec.plate_shape, spec.plate_shape_source = str(shape), f"{a.kind.value} params.shape"
    for a in pj.get("addons") or []:
        shape = (a.get("params") or {}).get("shape")
        if shape and spec.plate_shape is None:
            spec.plate_shape, spec.plate_shape_source = str(shape), "s01 product.json add-on"
    pick = ((pj.get("picks") or {}).get("plate_shape") or [None])[0]
    if pick and spec.plate_shape is None:
        spec.plate_shape, spec.plate_shape_source = str(pick), "s01 product.json picks"
    for src, d in (("job.json", jj), ("input/request.json", req)):
        v = _nested(d, "plate_shape")
        if v and spec.plate_shape is None:
            spec.plate_shape, spec.plate_shape_source = str(v), f"{src} plate_shape"
    if spec.plate_shape is None:
        val, qid = _answer(answers, ("plate_shape",))
        if val:
            spec.plate_shape, spec.plate_shape_source = str(val), f"answer {qid}"

    for role in ROLES:
        keys = [f"{role}_color"] + (["name_color"] if role == "text" else [])
        for src, d in (("job.json", jj), ("input/request.json", req)):
            box = _nested(d, "addon_colors")
            vals = [box.get(role)] if isinstance(box, dict) else []
            vals += [_nested(d, k) for k in keys]
            for v in vals:
                c = color_value(v, known_ids)
                if c and role not in spec.colors:
                    spec.colors[role], spec.color_sources[role] = c, f"{src}"
        if role not in spec.colors:
            val, qid = _answer(answers, tuple(keys))
            c = color_value(val, known_ids)
            if c:
                spec.colors[role], spec.color_sources[role] = c, f"answer {qid}"

    for src, d in (("job.json", jj), ("input/request.json", req)):
        v = _nested(d, "palette_source")
        if v in ("owned", "any"):
            spec.palette_source, spec.palette_source_from = v, src
            break
    else:
        row = None
        if store is not None:
            try:
                row = store.job_row(job.id) or {}
            except Exception:  # noqa: BLE001 no row: owned
                row = None
        if row and row.get("palette_source") in ("owned", "any"):
            spec.palette_source, spec.palette_source_from = row["palette_source"], "jobs row"
    return spec


# The intake loop answer (question_bank.yaml `loop`) is an add-on position; the concept read's
# loop_spot maps to the same positions. p2p_geometry.product.loop_area turns a position into
# the area the loop must stay in (top: the head, back, side, any).
LOOP_KINDS = ("keyring_loop", "hanging_loop")
SPOT_POSITION = {"head": "top", "back": "back", "side": "left"}
POSITION_AREA = {"top": "head", "head": "head", "back": "back", "left": "side", "right": "side",
                 "side": "side", "any": "any", "auto": "any", "": "any"}


def resolve_loop(addons: list, read: dict | None) -> tuple[list, dict | None]:
    """The loop add-on with the area the customer chose: the intake answer (the add-on's
    position), else where the approved concept draws the loop, else any. Returns (addons,
    info) with info = {area, position, source, concept_spot, conflict} or None without a
    loop. The answer wins a disagreement with the concept (the concept is drawn from it; a
    mismatch is a warning, never a silent move)."""
    idx = next((i for i, a in enumerate(addons) if a.kind.value in LOOP_KINDS), None)
    if idx is None:
        return addons, None
    a = addons[idx]
    spot = (read or {}).get("loop_spot") if (read or {}).get("loop", {}).get("shown", True) \
        else None
    concept_pos = SPOT_POSITION.get(spot or "")
    pos = str((a.params or {}).get("position", "")).strip().lower()
    info = {"concept_spot": spot, "conflict": None}
    if pos and pos not in ("auto",):
        info.update(position=pos, source="intake answer")
        if concept_pos and POSITION_AREA.get(pos) not in (POSITION_AREA[concept_pos], "any"):
            info["conflict"] = (f"The approved concept shows the loop on the {spot}; the "
                                f"answer was {pos}, which wins")
    elif concept_pos:
        info.update(position=concept_pos, source="approved concept")
        a = a.model_copy(update={"params": {**(a.params or {}), "position": concept_pos}})
        addons = addons[:idx] + [a] + addons[idx + 1:]
    else:
        info.update(position="any", source="not asked (anywhere it hangs best)")
    info["area"] = POSITION_AREA.get(info["position"], "any")
    return addons, info


def spec_of(job_dir: Path) -> dict | None:
    """The resolved spec s06 wrote (newest succeeded attempt), for later stages."""
    d = Path(job_dir) / StageId.S06_PRINTABILITY.value
    atts = sorted((p for p in d.glob("a*") if p.name[1:].isdigit()),
                  key=lambda p: int(p.name[1:]), reverse=True) if d.is_dir() else []
    for a in atts:
        if _read(a / "manifest.json").get("status") == "succeeded":
            s = _read(a / "product_resolved.json") or _read(a / "product_spec.json")
            return s or None
    return None
