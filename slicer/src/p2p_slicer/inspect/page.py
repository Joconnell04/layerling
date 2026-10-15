"""index.html: one static page (no script, no network) that shows everything in inspection.json.
Light theme, labels of three words or fewer."""

from __future__ import annotations

import re
from html import escape
from pathlib import Path
from typing import Any

from .chart import fmt_s, legend_html

CSS = """
:root{--bg:#F5F6F8;--card:#FFFFFF;--line:#E4E7EB;--text:#15181D;--muted:#6B7280;
--soft:#F0F2F5;--accent:#B4412F;--ok:#1F7A4D;--warn:#A15C00;--bad:#B42318}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.bar{max-width:1240px;margin:0 auto;padding:12px 20px;display:flex;align-items:center;gap:16px;
flex-wrap:wrap}
.mark{width:12px;height:12px;border-radius:3px;background:var(--accent);flex:none}
h1{font-size:16px;margin:0;font-weight:600;word-break:break-all}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{background:var(--soft);border:1px solid var(--line);border-radius:999px;padding:2px 9px;
font-size:12px;color:var(--muted);white-space:nowrap}
nav{margin-left:auto;display:flex;gap:14px;flex-wrap:wrap}
nav a{color:var(--muted);text-decoration:none;font-size:13px}nav a:hover{color:var(--text)}
main{max-width:1240px;margin:0 auto;padding:18px 20px 60px}
section{margin-top:22px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
margin:0 0 10px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stat .k{font-size:12px;color:var(--muted)}
.stat .v{font-size:22px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.stat .s{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.grid{display:grid;gap:10px;align-items:start}
.g2{grid-template-columns:repeat(auto-fit,minmax(360px,1fr))}
.g4{grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}
.gp{grid-template-columns:minmax(0,1.75fr) minmax(0,1fr)}
.gt{grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(0,.75fr)}
.gl{grid-template-columns:repeat(2,minmax(0,1fr))}
@media (max-width:900px){.gp,.gt,.gl{grid-template-columns:1fr}}
figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:10px;
overflow:hidden}
figure img{display:block;width:100%;height:auto;background:#E7E9EC}
figcaption{font-size:12px;color:var(--muted);padding:6px 10px;border-top:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:500}
th:first-child,td:first-child,.l{text-align:left}
tr:last-child td{border-bottom:0}
.tw{overflow-x:auto}
.sw{display:inline-block;width:12px;height:12px;border-radius:50%;
border:1px solid rgba(0,0,0,.18);vertical-align:-2px;margin-right:6px}
.sq{border-radius:3px}
.muted{color:var(--muted)}
.pos{color:var(--warn)}.neg{color:var(--ok)}
.lvl{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;
padding:1px 6px;border-radius:4px}
.lvl-error{background:#FDECEA;color:var(--bad)}.lvl-warning{background:#FFF4E0;color:var(--warn)}
.lvl-info{background:var(--soft);color:var(--muted)}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;
vertical-align:-1px}.legend i.line{height:2px;width:14px;vertical-align:3px}
details summary{cursor:pointer;color:var(--muted);font-size:13px}
.chart{width:100%;height:auto;display:block}
.pill{display:inline-flex;align-items:center;gap:4px;background:var(--soft);
border:1px solid var(--line);border-radius:999px;padding:2px 8px;margin:2px 4px 2px 0;
font-size:12px}
.empty{color:var(--muted);font-size:13px}
"""


def _e(v: Any) -> str:
    return escape("" if v is None else str(v))


def _g(v, nd: int = 2) -> str:
    return "" if v is None else f"{float(v):.{nd}f}"


def short_preset(p: str | None) -> str:
    return re.sub(r"\s*@BBL.*$", "", p or "").strip()


def _swatch(colour: str | None, square: bool = False) -> str:
    return (f'<span class="sw{" sq" if square else ""}" '
            f'style="background:{_e(colour or "#999")}"></span>')


def _slot(insp: dict[str, Any], slot: int | None) -> str:
    if slot is None:
        return ""
    f = next((x for x in insp["filaments"] if x["slot"] == slot), None)
    return f'<span class="pill">{_swatch(f["colour"] if f else None)}{slot}</span>'


def _delta(ours, ref) -> str:
    if ours is None or ref is None:
        return ""
    d = float(ours) - float(ref)
    if not float(ref):
        return "same" if abs(d) < 1e-9 else f"{d:+.2f}"
    pct = 100 * d / float(ref)
    return "same" if abs(pct) < 0.05 else f"{pct:+.1f}%"


def _stat(label: str, value: str, sub: str = "") -> str:
    return (f'<div class="stat"><div class="k">{_e(label)}</div><div class="v">{value}</div>'
            f'<div class="s">{sub}</div></div>')


def _fig(src: str | None, caption: str) -> str:
    if not src:
        return ""
    return (f'<figure><img src="{_e(src)}" alt="{_e(caption)}" loading="lazy">'
            f"<figcaption>{_e(caption)}</figcaption></figure>")


def _summary(insp: dict[str, Any]) -> str:
    t = insp["totals"]
    ref = t.get("reference") or {}
    by = insp["source"]["sliced_by"]["slicer"]
    ours_label = "Ours" if ref else ""

    def pair(key: str, ref_key: str, time: bool = False, nd: int = 2):
        o, r = t.get(key), ref.get(ref_key)
        main = r if r is not None else o
        if main is None:
            return "", ""
        v = fmt_s(main) if time else (f"{main:.{nd}f}" if isinstance(main, float) else str(main))
        sub = ""
        if r is not None and o is not None:
            ov = fmt_s(o) if time else (f"{o:.{nd}f}" if isinstance(o, float) else str(o))
            sub = f"{ours_label} {ov}, {_delta(o, r)}"
        elif r is not None:
            sub = "Bambu"
        return v, sub

    cards = []
    for label, key, rkey, time in (("Print time", "time_s", "time_s", True),
                                   ("Model time", "model_s", "model_s", True),
                                   ("Prepare time", "prepare_s", "prepare_s", True)):
        v, sub = pair(key, rkey, time)
        if v:
            cards.append(_stat(label, v, sub))
    v, sub = pair("grams", "grams")
    if v:
        cards.append(_stat("Total grams", v + " g", sub))
    for label, key in (("Flush grams", "flush_g"), ("Tower grams", "tower_g")):
        v, sub = pair(key, key)
        if v:
            cards.append(_stat(label, v + " g", sub))
    for label, key in (("Filament changes", "filament_changes"), ("AMS swaps", "ams_swaps"),
                       ("Nozzle switches", "nozzle_switches"), ("Layers", "layers")):
        v, sub = pair(key, key)
        if v:
            cards.append(_stat(label, v, sub))
    n_err = sum(1 for w in insp["warnings"] if w["level"] == "error")
    n_w = sum(1 for w in insp["warnings"] if w["level"] == "warning")
    cards.append(_stat("Warnings", str(n_err + n_w),
                       f"{n_err} errors" if n_err else ("none" if not n_w else "")))
    if not by:
        cards.insert(0, _stat("Sliced", "No", "model only"))
    return f'<div class="stats">{"".join(cards)}</div>'


def _filaments(insp: dict[str, Any]) -> str:
    rows = []
    has_ref = any(f.get("reference") for f in insp["filaments"])
    for f in insp["filaments"]:
        u = f.get("usage") or {}
        r = f.get("reference") or {}
        if not (f["used"] or f["faces"] or u.get("total_g") or r):
            cls = ' class="muted"'
        else:
            cls = ""
        noz = {0: "Left", 1: "Right"}.get(f.get("nozzle"), "")
        cells = [f'<td class="l">{_swatch(f["colour"])}{f["slot"]}</td>',
                 f'<td class="l">{_e(short_preset(f["preset"]))}</td>',
                 f'<td class="l">{_e(f["type"])}</td><td class="l">{noz}</td>',
                 f"<td>{f['faces'] or ''}</td>",
                 f"<td>{_g(u.get('model_g'), 3)}</td><td>{_g(u.get('flush_g'), 3)}</td>",
                 f"<td>{_g(u.get('tower_g'), 3)}</td><td><b>{_g(u.get('total_g'), 2)}</b></td>",
                 f"<td>{_g(u.get('total_m'), 2)}</td>",
                 f"<td>{fmt_s(f.get('load_s')) if f.get('load_s') else ''}</td>"]
        if has_ref:
            cells.append(f"<td>{_g(r.get('total_g'), 2)}</td><td>{_g(r.get('model_g'), 3)}</td>"
                         f"<td>{_g(r.get('flush_g'), 3)}</td><td>{_g(r.get('tower_g'), 3)}</td>")
        rows.append(f"<tr{cls}>{''.join(cells)}</tr>")
    head = ('<tr><th>Slot</th><th class="l">Filament</th><th class="l">Type</th>'
            '<th class="l">Nozzle</th><th>Faces</th>'
            "<th>Model g</th><th>Flush g</th><th>Tower g</th><th>Total g</th><th>Meters</th>"
            "<th>Load time</th>")
    if has_ref:
        head += "<th>Bambu g</th><th>Bambu model</th><th>Bambu flush</th><th>Bambu tower</th>"
    return f'<div class="card tw"><table>{head}</tr>{"".join(rows)}</table></div>'


def _objects(insp: dict[str, Any]) -> str:
    rows = []
    for o in insp["objects"]:
        for p in o["parts"]:
            fpf = " ".join(f'{_slot(insp, int(k))}<span class="muted">{v}</span>'
                           for k, v in p["faces_per_filament"].items())
            rows.append(f'<tr><td class="l">{_e(o["name"])}</td><td class="l">{_e(p["name"])}'
                        f'</td><td class="l">{_e(p["subtype"].replace("_", " "))}</td>'
                        f'<td class="l">{_slot(insp, p["filament"])}</td>'
                        f'<td>{p["faces"] or ""}</td><td class="l">{fpf}</td></tr>')
    if not rows:
        return '<div class="empty">No objects</div>'
    return ('<div class="card tw"><table><tr><th>Object</th><th class="l">Part</th>'
            '<th class="l">Kind</th><th class="l">Filament</th><th>Faces</th>'
            '<th class="l">Faces by filament</th></tr>'
            f'{"".join(rows)}</table></div>')


def _roles(insp: dict[str, Any]) -> str:
    rows = []
    for r in insp["roles"]:
        rows.append(f'<tr><td class="l">{_swatch(r["colour"], True)}{_e(r["role"])}</td>'
                    f"<td>{fmt_s(r['time_s'])}</td><td>{_g(r['percent'], 1)}</td>"
                    f"<td>{_g(r['m'], 2)}</td><td>{_g(r['g'], 2)}</td>"
                    f"<td>{fmt_s(r['reference_time_s'])}</td></tr>")
    tr = insp.get("travel") or {}
    if tr.get("time_s") or tr.get("reference_time_s"):
        rows.append(f'<tr><td class="l">{_swatch("#9AA1AC", True)}Travel</td>'
                    f"<td>{fmt_s(tr.get('time_s'))}</td><td></td>"
                    f"<td>{_g((tr.get('mm') or 0) / 1000, 2)}</td><td></td>"
                    f"<td>{fmt_s(tr.get('reference_time_s'))}</td></tr>")
    if not rows:
        return '<div class="empty">Not sliced</div>'
    return ('<div class="card tw"><table><tr><th>Feature</th><th>Time</th><th>Percent</th>'
            '<th>Meters</th><th>Grams</th><th>Bambu time</th></tr>'
            f'{"".join(rows)}</table></div>')


def _changes(insp: dict[str, Any]) -> str:
    sw = insp["switches"]
    if not sw:
        return '<div class="empty">No filament changes</div>'
    rows = []
    for s in sw:
        billed = " ".join(f'{_slot(insp, int(k))}<span class="muted">{v:.3f} g</span>'
                          for k, v in s["billed_g"].items())
        rows.append(f"<tr><td>{s['layer'] + 1}</td><td>{_g(s['z'], 2)}</td>"
                    f'<td class="l">{_slot(insp, s["from"])} to {_slot(insp, s["to"])}</td>'
                    f'<td class="l">{"Left" if s["nozzle"] == 0 else "Right"}</td>'
                    f'<td class="l">{"Yes" if s["nozzle_switch"] else ""}</td>'
                    f'<td class="l">{"Yes" if s["ams_swap"] else ""}</td>'
                    f"<td>{_g(s['flush_g'], 3) if s['flush_g'] else ''}</td>"
                    f'<td class="l">{billed}</td></tr>')
    return (f'<details class="card"{" open" if len(sw) <= 40 else ""}><summary>'
            f"{len(sw)} changes</summary><div class=\"tw\"><table><tr><th>Layer</th>"
            '<th>Z mm</th><th class="l">Change</th><th class="l">Nozzle</th>'
            '<th class="l">Nozzle switch</th><th class="l">AMS swap</th>'
            f'<th>Flush g</th><th class="l">Billed to</th></tr>{"".join(rows)}</table></div>'
            "</details>")


def _layers_table(insp: dict[str, Any]) -> str:
    rows = []
    for r in insp["layers"]:
        fl = " ".join(_slot(insp, s) for s in r["filaments"])
        planned = r.get("planned")
        mismatch = planned is not None and sorted(planned) != sorted(r["filaments"])
        rows.append(f"<tr><td>{r['index'] + 1}</td><td>{_g(r['z'], 2)}</td>"
                    f"<td>{_g(r['height'], 2)}</td><td>{fmt_s(r['time_s'])}</td>"
                    f"<td>{fmt_s(r['elapsed_s'])}</td><td class=\"l\">{fl}"
                    f'{" <span class=pos>planned differs</span>" if mismatch else ""}</td>'
                    f"<td>{r['changes'] or ''}</td><td>{r['ams_swaps'] or ''}</td></tr>")
    if not rows:
        return ""
    return ('<details class="card"><summary>All layers</summary><div class="tw"><table><tr>'
            "<th>Layer</th><th>Z mm</th><th>Height</th><th>Time</th><th>Elapsed</th>"
            '<th class="l">Filaments</th><th>Changes</th><th>AMS swaps</th></tr>'
            f'{"".join(rows)}</table></div></details>')


def _warnings(insp: dict[str, Any]) -> str:
    if not insp["warnings"]:
        return '<div class="empty">None</div>'
    order = {"error": 0, "warning": 1, "info": 2}
    items = sorted(insp["warnings"], key=lambda w: order.get(w["level"], 3))
    rows = "".join(f'<tr><td class="l"><span class="lvl lvl-{_e(w["level"])}">{_e(w["level"])}'
                   f'</span></td><td class="l" style="white-space:normal">{_e(w["text"])}</td>'
                   f'<td class="l muted">{_e(w["source"])}</td></tr>' for w in items)
    return f'<div class="card tw"><table>{rows}</table></div>'


def _parity(insp: dict[str, Any]) -> str:
    if not insp["parity"]:
        return ""
    rows = []
    for p in insp["parity"]:
        time = p["unit"] == "s"
        d = p["delta_pct"]
        cls = "" if d is None or abs(d) < 1 else (" class=pos" if abs(d) >= 5 else "")
        rows.append(f'<tr><td class="l">{_e(p["quantity"])}</td>'
                    f"<td>{fmt_s(p['ours']) if time else _e(p['ours'])}</td>"
                    f"<td>{fmt_s(p['bambu']) if time else _e(p['bambu'])}</td>"
                    f"<td{cls}>{_g(d, 2).replace('-0.00', '0.00') if d is not None else ''}"
                    "</td></tr>")
    return ('<section id="parity"><h2>Ours vs Bambu</h2><div class="card tw"><table><tr>'
            "<th>Quantity</th><th>Ours</th><th>Bambu</th><th>Delta %</th></tr>"
            f'{"".join(rows)}</table></div></section>')


def _grouping(insp: dict[str, Any]) -> str:
    g = insp["grouping"]

    def side(slots):
        return " ".join(_slot(insp, s) for s in slots) or '<span class="muted">none</span>'

    return (f'<div class="card"><table><tr><td class="l">Left nozzle</td><td class="l">'
            f'{side(g["left"])}</td></tr><tr><td class="l">Right nozzle</td><td class="l">'
            f'{side(g["right"])}</td></tr><tr><td class="l">Mode</td><td class="l">'
            f'{_e(g["mode"])}</td></tr></table></div>')


def _package(insp: dict[str, Any]) -> str:
    p = insp["package"]
    md5 = {True: "match", False: "mismatch", None: "none"}[p.get("gcode_md5_ok")]
    missing = ", ".join(p.get("missing") or []) or "none"
    return (f'<div class="card"><table><tr><td class="l">Kind</td><td class="l">'
            f'{_e(p["kind"])}</td></tr><tr><td class="l">Entries</td><td class="l">'
            f'{p["entries"]}</td></tr><tr><td class="l">G-code checksum</td><td class="l">'
            f'{md5}</td></tr><tr><td class="l">Missing</td><td class="l">{_e(missing)}</td></tr>'
            f'<tr><td class="l">Written by</td><td class="l">{_e(p.get("application"))}</td>'
            "</tr></table></div>")


def render_page(insp: dict[str, Any]) -> str:
    src = insp["source"]
    path = Path(src["path"])
    name = f"{path.parent.name}/{path.name}" if path.parent.name else path.name
    by = src["sliced_by"]
    chips = []
    if by["slicer"]:
        chips.append({"bambu_studio": "Bambu Studio", "p2p_slicer": "p2p_slicer"}.get(
            by["slicer"], by["slicer"]) + (f" {by['version']}" if by.get("version") else ""))
    for c in (insp["printer"].get("preset"), insp["process"].get("preset"),
              insp["plate"].get("bed_type")):
        if c:
            chips.append(str(c))
    r = insp.get("renders") or {}
    plate = r.get("plate") or {}
    model = r.get("model") or {}
    bambu = r.get("bambu") or {}
    tp = r.get("toolpaths") or {}
    nav = [("plate", "Plate"), ("model", "Model"), ("filaments", "Filaments"),
           ("features", "Features"), ("toolpaths", "Toolpaths"), ("layers", "Layers"),
           ("changes", "Changes"), ("parity", "Parity"), ("warnings", "Warnings")]
    if not insp["parity"]:
        nav = [n for n in nav if n[0] != "parity"]
    if not tp:
        nav = [n for n in nav if n[0] not in ("toolpaths",)]
    out = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width,initial-scale=1">',
        f"<title>{_e(name)}</title><style>{CSS}</style></head><body>",
        ('<header><div class="bar"><span class="mark"></span>'
         f'<h1>{_e(name)}</h1><div class="chips">'
         f'{"".join(f"<span class=chip>{_e(c)}</span>" for c in chips)}</div><nav>'
         f'{"".join(f"<a href=#{k}>{v}</a>" for k, v in nav)}</nav></div></header><main>'),
        f"<section>{_summary(insp)}</section>",
        '<section id="plate"><h2>Plate</h2><div class="grid gp">',
        _fig(plate.get("plate_iso"), "Plate"), _fig(plate.get("plate_top"), "Plate top"),
        "</div>",
    ]
    if plate.get("model_iso") or bambu.get("plate_1"):
        out.append('<div class="grid g4" style="margin-top:10px">')
        out.append(_fig(plate.get("model_iso"), "Our render"))
        out.append(_fig(bambu.get("plate_1"), "Bambu thumbnail"))
        out.append(_fig(bambu.get("plate_no_light_1"), "Bambu flat"))
        out.append(_fig(bambu.get("top_1"), "Bambu top"))
        out.append("</div>")
    out.append("</section>")
    if model:
        labels = {"front": "Front", "front_3q": "Front 3/4", "right": "Right",
                  "back_3q": "Back 3/4", "back": "Back", "left": "Left", "top": "Top"}
        out.append('<section id="model"><h2>Model</h2><div class="grid g4">')
        out.extend(_fig(model[k], labels.get(k, k)) for k in labels if k in model)
        out.append("</div>")
        out.append(f'<div style="margin-top:10px">{_objects(insp)}</div></section>')
    out.append(f'<section id="filaments"><h2>Filaments</h2>{_filaments(insp)}'
               f'<div style="margin-top:10px">{_grouping(insp)}</div></section>')
    out.append(f'<section id="features"><h2>Features</h2>{_roles(insp)}</section>')
    if tp:
        chips_roles = "".join(f'<span>{_swatch(x["colour"], True)}{_e(x["role"])}</span>'
                              for x in insp["roles"] if x.get("g"))
        out.append('<section id="toolpaths"><h2>Toolpaths</h2>'
                   f'<div class="grid {"gt" if tp.get("paths_tower") else "g2"}">'
                   f'{_fig(tp.get("paths_filament"), "By filament")}'
                   f'{_fig(tp.get("paths_feature"), "By feature")}'
                   f'{_fig(tp.get("paths_tower"), "Prime tower")}</div>'
                   f'<div class="legend">{chips_roles}</div></section>')
    out.append('<section id="layers"><h2>Layers</h2>')
    if r.get("layer_time"):
        svg = Path(insp["_out_dir"], r["layer_time"]).read_text() if insp.get("_out_dir") \
            else ""
        out.append(f'<div class="card">{svg}{legend_html()}</div>')
    views = r.get("layers") or []
    if views:
        out.append('<div class="grid gl" style="margin-top:10px">')
        out.extend(_fig(v["path"], f"Layer {v['index'] + 1} · {_g(v['z'], 2)} mm")
                   for v in views)
        out.append("</div>")
    lt = _layers_table(insp)
    out.append(f'<div style="margin-top:10px">{lt}</div>' if lt else
               '<div class="empty">Not sliced</div>')
    out.append("</section>")
    out.append(f'<section id="changes"><h2>Changes</h2>{_changes(insp)}</section>')
    out.append(_parity(insp))
    out.append(f'<section id="warnings"><h2>Warnings</h2>{_warnings(insp)}</section>')
    out.append(f'<section id="package"><h2>Package</h2>{_package(insp)}</section>')
    out.append("</main></body></html>")
    return "".join(out)


def write_page(insp: dict[str, Any], path: Path) -> Path:
    insp = {**insp, "_out_dir": str(path.parent)}
    path.write_text(render_page(insp))
    return path
