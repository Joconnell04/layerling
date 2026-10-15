"""Single-color slice check with the Bambu Studio CLI (dev tool; the threemf workstream owns the
real slicer gate). Flattens the system X2D profiles because the CLI does not resolve `inherits`
for files passed on the command line (filament density comes back 0 otherwise).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

BAMBU = Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")
PROFILES = Path("/Applications/BambuStudio.app/Contents/Resources/profiles/BBL")
MACHINE = "Bambu Lab X2D 0.4 nozzle"
PROCESS = "0.20mm Standard @BBL X2D"
FILAMENT = "Generic PLA @BBL X2D 0.4 nozzle"


def available() -> bool:
    return BAMBU.exists() and PROFILES.exists()


def _index(kind: str) -> dict[str, Path]:
    out = {}
    for p in (PROFILES / kind).glob("*.json"):
        try:
            name = json.loads(p.read_text()).get("name")
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if name:
            out[name] = p
    return out


def flatten(kind: str, name: str, _idx: dict | None = None) -> dict:
    idx = _idx or _index(kind)
    data = json.loads(idx[name].read_text())
    parent = data.get("inherits")
    if parent and parent in idx:
        merged = flatten(kind, parent, idx)
        merged.update(data)
        data = merged
    data.pop("inherits", None)
    data["name"] = name
    return data


def bed_center() -> tuple[float, float]:
    m = flatten("machine", MACHINE)
    pts = [tuple(map(float, p.split("x"))) for p in m.get("printable_area", ["0x0", "256x256"])]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


@dataclass
class SliceResult:
    ok: bool
    returncode: int
    seconds: float
    print_time: str | None = None
    print_time_s: float | None = None
    grams: float | None = None
    filament_mm: float | None = None
    layers: int | None = None
    support_lines: int = 0
    errors: list[str] = field(default_factory=list)
    log: str | None = None


def _hms_to_s(s: str) -> float:
    total = 0.0
    for val, unit in re.findall(r"(\d+)([dhms])", s):
        total += int(val) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total


def slice_file(model: Path | str, out_dir: Path | str, supports: bool = True,
               timeout_s: int = 540) -> SliceResult:
    """Slice one 3MF/STL with the X2D 0.4 profiles; parse time, grams and support extrusion."""
    model = Path(model).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    # A private datadir keeps concurrent Bambu Studio runs (other workstreams) from blocking
    # each other on the shared app-support folder.
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as datadir:
        td = Path(td)
        mach = flatten("machine", MACHINE)
        proc = flatten("process", PROCESS)
        fil = flatten("filament", FILAMENT)
        proc["enable_support"] = "1" if supports else "0"
        proc["support_type"] = "tree(auto)"
        for d, n in ((mach, "machine"), (proc, "process"), (fil, "filament")):
            (td / f"{n}.json").write_text(json.dumps(d))
        gcode_3mf = out_dir / f"{model.stem}.gcode.3mf"
        cmd = [
            str(BAMBU), "--debug", "2", "--datadir", datadir,
            "--load-settings", f"{td / 'machine.json'};{td / 'process.json'}",
            "--load-filaments", str(td / "filament.json"),
            "--slice", "0", "--outputdir", str(out_dir), str(model),
        ]
        t = time.time()
        try:
            pr = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s,
                                cwd=str(out_dir))
            rc, log = pr.returncode, pr.stdout + pr.stderr
        except subprocess.TimeoutExpired as e:
            rc, log = -9, f"timeout after {timeout_s} s\n{e.stdout or ''}"
        secs = time.time() - t
    log_path = out_dir / f"{model.stem}.slice.log"
    log_path.write_text(log)
    res = SliceResult(ok=False, returncode=rc, seconds=secs, log=str(log_path))
    res.errors = [ln for ln in log.splitlines() if "[error]" in ln][-10:]
    gcode = _read_gcode(out_dir, gcode_3mf)
    if gcode:
        head = gcode[:20000]
        m = re.search(r"model printing time: ([^;\n]+)", head)
        if m:
            res.print_time = m.group(1).strip()
            res.print_time_s = _hms_to_s(res.print_time)
        m = re.search(r"total filament weight \[g\] : ([\d.]+)", head)
        res.grams = float(m.group(1)) if m else None
        m = re.search(r"total filament length \[mm\] : ([\d.]+)", head)
        res.filament_mm = float(m.group(1)) if m else None
        m = re.search(r"total layer number: (\d+)", head)
        res.layers = int(m.group(1)) if m else None
        res.support_lines = len(re.findall(r"^; FEATURE: Support", gcode, flags=re.MULTILINE))
    res.ok = rc == 0 and gcode is not None and (res.grams or 0) > 0
    return res


def _read_gcode(out_dir: Path, gcode_3mf: Path) -> str | None:
    plain = out_dir / "plate_1.gcode"
    if plain.exists():
        text = plain.read_text(errors="replace")
        plain.replace(out_dir / f"{gcode_3mf.name.removesuffix('.gcode.3mf')}.gcode")
        return text
    if gcode_3mf.exists():
        with zipfile.ZipFile(gcode_3mf) as z:
            names = [n for n in z.namelist() if n.endswith(".gcode")]
            if names:
                return z.read(names[0]).decode(errors="replace")
    return None
