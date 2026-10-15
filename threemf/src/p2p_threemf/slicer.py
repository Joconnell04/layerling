"""Slicer gate: slice a 3MF headless and report time, grams per filament, and slots used.

Backends share one interface. Bambu Studio 2.08 is the primary gate. OrcaSlicer 2.4.2 is a
second, independent check on a patched copy (see OrcaSlicerBackend).

Bambu CLI facts used here (checked against 02.08.02.61):
- `--slice 1 --outputdir D --export-3mf OUT` slices plate 1 using the project's embedded config,
  writes D/result.json (return_code, error_string) and OUT, a .gcode.3mf whose
  Metadata/slice_info.config holds prediction (s), weight (g) and one <filament> per used slot.
- The CLI never opens a window. Paths must be absolute (it chdirs).
- "Invalid T command (T65279/T65535)" errors come from Bambu's own X2D end G-code (AMS retract
  commands) and are harmless.
- D/result.json splits every filament into model and total grams, gives seconds per feature and
  the AMS swap count (filament_change_times). slice_check keeps it next to the slicer log and
  fills the exact purge, tower, flush and time split from it plus the G-code.

SLICER_BIN overrides the Bambu Studio binary (absolute path or a name on PATH), for a Linux
build on a cloud VM. settings.slicer "p2p_slicer" slices with our own slicer (slicer/ in this
repo) when p2p_slicer.api.slice_project exists; its report is an estimate, never the ship gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from p2p_contracts import PrintSettings, SliceReport
from p2p_contracts.manifest import FileRef
from p2p_contracts.reports import SliceFilament

from .audit import audit_paint
from .gcode_stats import GcodeStats, invalid_tool_changes, parse_gcode
from .reader import read_3mf_full
from .verify import readback_check

BAMBU_BIN = Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")
NATIVE = "p2p_slicer"
ORCA_BIN = Path("/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer")
ORCA_PROFILES = Path("/Applications/OrcaSlicer.app/Contents/Resources/profiles")
DEFAULT_TIMEOUT_S = 900

# "Invalid T command (T65279/T65535)" comes from Bambu's own X2D machine end G-code (AMS unload
# commands) and appears for projects Bambu Studio saved itself. slice_check still verifies in
# the G-code that these are the only out-of-range tool commands and that they sit in the end
# G-code (gcode_stats.invalid_tool_changes); anything else fails the gate.
BENIGN_LOG_ERRORS = (
    re.compile(r"Invalid T command \(T(65279|65535)\)"),
)
# Warnings the Bambu CLI prints on every run of a valid project (checked against 02.08.02.61).
BENIGN_LOG_WARNINGS = (
    re.compile(r"^cli mode, Current BambuStudio Version"),
    re.compile(r"can not find system preset file: .*/(machine|process|filament)_full/"),
    re.compile(r"^no filament colors found in projects$"),
    re.compile(r"^load_nozzle_infos_with_compatibility: building nozzle list"),
    re.compile(r"^tree support default to organic support$"),
)


@dataclass
class RawSlice:
    ok: bool
    returncode: int | None
    result: dict = field(default_factory=dict)
    sliced_3mf: Path | None = None
    log_path: Path | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    timed_out: bool = False
    numbers: SliceNumbers | None = None  # set by backends that measure without a .gcode.3mf


@dataclass
class SliceNumbers:
    """What one slice measured, in SliceReport units (s, g, m). Bambu: slice_info.config,
    result.json and the G-code (bambu_numbers). p2p_slicer: its SliceResult (native_numbers)."""
    print_time_s: float | None = None
    model_time_s: float | None = None
    prepare_time_s: float | None = None
    filaments: list[SliceFilament] = field(default_factory=list)
    weight_g: float | None = None
    purge_grams: float | None = None
    tower_grams: float | None = None
    filament_changes: int | None = None
    nozzle_changes: int | None = None
    ams_swaps: int | None = None
    feature_time_s: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class SlicerBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    def version(self) -> str: ...

    def slice(self, model: Path, workdir: Path, timeout_s: float) -> RawSlice: ...


def _app_version(binary: Path) -> str:
    plist = binary.parent.parent / "Info.plist"
    if plist.exists():
        import plistlib

        with plist.open("rb") as f:
            return str(plistlib.load(f).get("CFBundleShortVersionString", "unknown"))
    return "unknown"


def _run(cmd: list[str], log_path: Path, timeout_s: float, cwd: Path) -> tuple[int | None, bool]:
    """Run without a shell, output to a log file, kill the process group on timeout."""
    with log_path.open("wb") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=cwd,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            return proc.wait(timeout=timeout_s), False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return None, True


def _log_lines(log_path: Path) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    if not log_path.exists():
        return errors, warnings
    for line in log_path.read_text(errors="replace").splitlines():
        m = re.search(r"\[(error|warning)\]\s+(.*)$", line)
        if not m:
            continue
        msg = m.group(2).strip()
        benign = BENIGN_LOG_ERRORS if m.group(1) == "error" else BENIGN_LOG_WARNINGS
        if any(p.search(msg) for p in benign):
            continue
        (errors if m.group(1) == "error" else warnings).append(msg)
    return errors, warnings


def bambu_binary() -> Path:
    """SLICER_BIN when set (an absolute path, or a name found on PATH), else the macOS app."""
    env = (os.environ.get("SLICER_BIN") or "").strip()
    if not env:
        return BAMBU_BIN
    p = Path(env).expanduser()
    if not p.is_absolute() and os.sep not in env:
        found = shutil.which(env)
        return Path(found) if found else p
    return p


def _log_version(log_path: Path | None) -> str | None:
    """Bambu logs "cli mode, Current BambuStudio Version 02.08.02.61" on every run; the only
    version source for a Linux build without an Info.plist."""
    if log_path is None or not log_path.exists():
        return None
    m = re.search(r"Current BambuStudio Version (\S+)", log_path.read_text(errors="replace"))
    return m.group(1) if m else None


class BambuStudioBackend:
    name = "bambu_studio"
    resaves_model = True  # the .gcode.3mf carries Bambu's re-saved model (read-back check)

    def __init__(self, binary: Path | None = None):
        self.binary = Path(binary) if binary is not None else bambu_binary()

    def available(self) -> bool:
        return self.binary.is_file()

    def version(self) -> str:
        return _app_version(self.binary)

    def slice(self, model: Path, workdir: Path, timeout_s: float) -> RawSlice:
        workdir = workdir.resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        out_3mf = workdir / "sliced.gcode.3mf"
        log_path = workdir / "slicer.log"
        cmd = [str(self.binary), "--debug", "2", "--slice", "1", "--outputdir", str(workdir),
               "--export-3mf", out_3mf.name, str(model.resolve())]
        t0 = time.monotonic()
        rc, timed_out = _run(cmd, log_path, timeout_s, workdir)
        raw = RawSlice(ok=False, returncode=rc, log_path=log_path, timed_out=timed_out,
                       duration_s=round(time.monotonic() - t0, 2))
        rj = workdir / "result.json"
        if rj.exists():
            try:
                raw.result = json.loads(rj.read_text())
            except json.JSONDecodeError:
                raw.errors.append("result.json is not valid JSON")
        raw.errors, raw.warnings = _log_lines(log_path)
        if timed_out:
            raw.errors.insert(0, f"slicer timed out after {timeout_s:.0f} s")
        if raw.result.get("return_code", rc) not in (0, None) or rc not in (0,):
            msg = raw.result.get("error_string") or f"slicer exit code {rc}"
            if msg not in raw.errors:
                raw.errors.insert(0, msg)
        if out_3mf.exists():
            raw.sliced_3mf = out_3mf
        raw.ok = (rc == 0 and not timed_out and raw.sliced_3mf is not None
                  and raw.result.get("return_code", 0) == 0)
        return raw

    def load_and_save(self, model: Path, workdir: Path, timeout_s: float = 300) -> RawSlice:
        """Load the project and save it again without slicing (what "open" does to it)."""
        workdir = workdir.resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        out_3mf = workdir / "resaved.3mf"
        log_path = workdir / "resave.log"
        cmd = [str(self.binary), "--debug", "2", "--outputdir", str(workdir),
               "--export-3mf", out_3mf.name, str(model.resolve())]
        rc, timed_out = _run(cmd, log_path, timeout_s, workdir)
        raw = RawSlice(ok=rc == 0 and out_3mf.exists(), returncode=rc, log_path=log_path,
                       timed_out=timed_out, sliced_3mf=out_3mf if out_3mf.exists() else None)
        raw.errors, raw.warnings = _log_lines(log_path)
        return raw


class OrcaSlicerBackend:
    """Second backend: OrcaSlicer 2.4.2 (ships X2D profiles, profile set 02.01.00.19).

    Orca cannot take a Bambu 2.08 project as is, so it slices a patched copy:
    - Bambu 2.08's X2D machine G-code uses placeholders Orca's template engine does not know
      (initial_no_support_filament_id), so every *_gcode key is replaced by Orca's own X2D
      machine preset for the same printer name.
    - Orca validates ranges more strictly (Bambu writes -1 for "auto" in
      raft_first_layer_expansion and tree_support_wall_count); out-of-range values reported by
      Orca are clamped and the slice retried.
    - Bambu's file version (02.08) is newer than Orca's numbering, hence --allow-newer-file.
    Paint, geometry, filaments and process values stay as written, so this is an independent
    check that the model and paint slice, not a check of Bambu's G-code."""

    name = "orcaslicer"
    resaves_model = True
    MAX_RETRIES = 3

    def __init__(self, binary: Path = ORCA_BIN, profiles: Path = ORCA_PROFILES):
        self.binary = Path(binary)
        self.profiles = Path(profiles)

    def available(self) -> bool:
        return self.binary.exists()

    def version(self) -> str:
        return _app_version(self.binary)

    def has_machine(self, name: str) -> bool:
        return (self.profiles / "BBL" / "machine" / f"{name}.json").exists()

    def _machine_gcode(self, printer: str) -> dict[str, str]:
        from .profiles import AppProfiles

        cfg = AppProfiles(self.profiles).resolve(printer).config
        return {k: v for k, v in cfg.items() if k.endswith("_gcode") and isinstance(v, str)}

    def slice(self, model: Path, workdir: Path, timeout_s: float) -> RawSlice:
        printer = _project_printer(model)
        if not printer:
            return RawSlice(ok=False, returncode=None, errors=["no printer_settings_id in project"])
        if not self.has_machine(printer):
            return RawSlice(ok=False, returncode=None, errors=[
                f"OrcaSlicer {self.version()} has no machine profile {printer!r}"])
        workdir = workdir.resolve()
        patches: dict[str, object] = dict(self._machine_gcode(printer))
        notes = [f"orcaslicer: sliced a copy with Orca's own {printer} G-code templates"]
        deadline = time.monotonic() + timeout_s
        raw = RawSlice(ok=False, returncode=None)
        for _attempt in range(self.MAX_RETRIES):
            patched = workdir / "orca_input.3mf"
            _patch_project(model, patched, patches)
            out_3mf = workdir / "sliced.gcode.3mf"
            log_path = workdir / "slicer.log"
            cmd = [str(self.binary), "--debug", "2", "--allow-newer-file", "--slice", "1",
                   "--outputdir", str(workdir), "--export-3mf", out_3mf.name, str(patched)]
            t0 = time.monotonic()
            rc, timed_out = _run(cmd, log_path, max(1.0, deadline - t0), workdir)
            raw = RawSlice(ok=False, returncode=rc, log_path=log_path, timed_out=timed_out,
                           duration_s=round(time.monotonic() - t0, 2))
            raw.errors, raw.warnings = _log_lines(log_path)
            if out_3mf.exists():
                raw.sliced_3mf = out_3mf
            raw.ok = rc == 0 and not timed_out and raw.sliced_3mf is not None
            if raw.ok or timed_out:
                break
            clamps = _range_errors(log_path)
            if not clamps:
                break
            for key, (old, new) in clamps.items():
                patches[key] = new
                notes.append(f"orcaslicer: clamped {key} {old} -> {new} for Orca's range check")
        if not raw.ok:
            text = log_path.read_text(errors="replace") if raw.log_path else ""
            tail = [ln for ln in text.splitlines() if ln.strip() and "[trace]" not in ln][-3:]
            raw.errors.extend(tail or [f"slicer exit code {raw.returncode}"])
        raw.warnings = notes + raw.warnings
        return raw


_RANGE_RE = re.compile(r"^(\w+): (-?[\d.]+) not in range \[(-?[\d.]+),(-?[\d.e+]+)\]")


def _range_errors(log_path: Path) -> dict[str, tuple[str, str]]:
    """Orca prints 'key: value not in range [lo,hi]' when it rejects a project value."""
    out = {}
    if not log_path.exists():
        return out
    for line in log_path.read_text(errors="replace").splitlines():
        m = _RANGE_RE.match(line.strip())
        if m:
            key, val, lo, hi = m.group(1), float(m.group(2)), float(m.group(3)), float(m.group(4))
            new = min(max(val, lo), hi)
            out[key] = (m.group(2), f"{new:g}")
    return out


def _patch_project(src: Path, dst: Path, patches: dict[str, object]) -> None:
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
        for info in zi.infolist():
            data = zi.read(info.filename)
            if info.filename == "Metadata/project_settings.config":
                cfg = json.loads(data)
                cfg.update(patches)
                data = json.dumps(cfg, indent=4).encode()
            zo.writestr(info, data)


def _native_api():
    """p2p_slicer.api, once the slicer core has landed slice_project; else None. Imported
    lazily: p2p_slicer depends on p2p_threemf, never the other way round."""
    try:
        from p2p_slicer import api  # type: ignore[import-not-found]
    except ImportError:
        return None
    return api if callable(getattr(api, "slice_project", None)) else None


class NativeSlicerBackend:
    """p2p_slicer, our own slicer (slicer/ in this repo). Its numbers are an estimate: it
    re-saves no model, so there is no read-back, and Bambu Studio stays the ship gate. Runs in
    this process, so timeout_s is not enforced (the slicer's budget is seconds)."""

    name = NATIVE
    resaves_model = False

    def available(self) -> bool:
        return _native_api() is not None

    def version(self) -> str:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as pkg_version

        try:
            return pkg_version("p2p-slicer")
        except PackageNotFoundError:
            return "unknown"

    def slice(self, model: Path, workdir: Path, timeout_s: float) -> RawSlice:
        api = _native_api()
        if api is None:
            return RawSlice(ok=False, returncode=None,
                            errors=["p2p_slicer has no slice_project yet"])
        workdir = workdir.resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        log_path = workdir / "slicer.log"
        t0 = time.monotonic()

        def log(level: str, msg: str) -> None:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with log_path.open("a") as f:
                f.write(f"[{stamp}] [{level}] {msg}\n")

        log("info", f"p2p_slicer {self.version()} slicing {model.name}")
        try:
            res = api.slice_project(model.resolve(), out_dir=workdir, gcode=True)
        except Exception as e:  # noqa: BLE001 a crash in the slicer is a failed slice
            log("error", f"{type(e).__name__}: {e}")
            return RawSlice(ok=False, returncode=1, log_path=log_path,
                            errors=[f"p2p_slicer failed: {type(e).__name__}: {e}"],
                            duration_s=round(time.monotonic() - t0, 2))
        numbers = native_numbers(res)
        result: dict = {}
        report_mod = _native_report()
        analysis = _field(res, "analysis")
        if analysis is not None and callable(getattr(report_mod, "cli_result_json", None)):
            try:  # Bambu's result.json shape, kept next to the log like Bambu's own
                result = dict(report_mod.cli_result_json(analysis))
            except Exception as e:  # noqa: BLE001 the copy is optional
                log("warning", f"cli_result_json failed: {type(e).__name__}: {e}")
        sliced = sorted(workdir.glob("*.gcode.3mf"))
        for w in numbers.warnings:
            log("warning", w)
        log("info", f"done: {numbers.print_time_s} s, {len(numbers.filaments)} filaments")
        return RawSlice(ok=True, returncode=0, result=result,
                        sliced_3mf=sliced[0] if sliced else None, log_path=log_path,
                        warnings=list(numbers.warnings), numbers=numbers,
                        duration_s=round(time.monotonic() - t0, 2))


def _native_report():
    try:
        from p2p_slicer import report  # type: ignore[import-not-found]
    except ImportError:
        return None
    return report


def _field(obj, *names, default=None):
    """First present attribute or key among names (SliceResult fields per the spec,
    docs/research/native-slicer.md 13.4)."""
    for n in names:
        if isinstance(obj, dict) and n in obj:
            return obj[n]
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


def _usage_value(slot_usage, kind: str, unit: str) -> float | None:
    """Usage.per_slot[slot]: a FilamentUse (grams(bucket), metres(bucket); total = no bucket),
    or a dict flat ("model_g") or nested ({"model": {"g": ...}})."""
    if slot_usage is None:
        return None
    fn = getattr(slot_usage, {"g": "grams", "m": "metres"}.get(unit, ""), None)
    if callable(fn):
        return float(fn() if kind == "total" else fn(kind))
    v = _field(slot_usage, f"{kind}_{unit}")
    if v is None:
        inner = _field(slot_usage, kind)
        v = _field(inner, unit) if inner is not None else None
    return float(v) if v is not None else None


def native_numbers(res) -> SliceNumbers:
    """SliceNumbers from a p2p_slicer result: its SliceReport (`report`) when it has one, else
    TimeResult and Usage (`time`, `usage`, directly or under `analysis`)."""
    rep = _field(res, "report")
    if isinstance(rep, SliceReport):
        n = SliceNumbers(print_time_s=rep.print_time_s, model_time_s=rep.model_time_s,
                         prepare_time_s=rep.prepare_time_s,
                         filaments=[f for f in rep.filaments if f.grams > 0],
                         weight_g=rep.total_grams, purge_grams=rep.purge_grams,
                         tower_grams=rep.tower_grams, filament_changes=rep.filament_changes,
                         nozzle_changes=rep.nozzle_changes, ams_swaps=rep.ams_swaps,
                         warnings=list(rep.warnings))
        t = _field(_field(res, "analysis"), "time")
        if t is not None:
            n.feature_time_s = {str(k): round(float(v), 2)
                                for k, v in dict(_field(t, "per_role_s") or {}).items()}
        return n
    src = _field(res, "analysis") if _field(res, "time") is None else res
    t = _field(src, "time")
    u = _field(src, "usage")
    total = _field(t, "total_s")
    prepare = _field(t, "prepare_s")
    n = SliceNumbers(print_time_s=round(float(total), 1) if total is not None else None)
    if total is not None and prepare is not None:
        n.prepare_time_s = round(float(prepare), 1)
        n.model_time_s = round(float(total) - float(prepare), 1)
    per_role = _field(t, "per_role_s") or {}
    n.feature_time_s = {str(k): round(float(v), 2) for k, v in dict(per_role).items()}
    per_slot = dict(_field(u, "per_slot") or {})
    tower = flush = 0.0
    for slot in sorted(per_slot, key=int):
        su = per_slot[slot]
        grams = _usage_value(su, "total", "g") or 0.0
        if grams <= 0:
            continue
        model = _usage_value(su, "model", "g")
        support = _usage_value(su, "support", "g") or 0.0
        tw = _usage_value(su, "tower", "g")
        fl = _usage_value(su, "flush", "g")
        tower += tw or 0.0
        flush += fl or 0.0
        n.filaments.append(SliceFilament(
            slot=int(slot), grams=round(grams, 2),
            meters=round(m, 2) if (m := _usage_value(su, "total", "m")) is not None else None,
            model_grams=round(model + support, 3) if model is not None else None,
            flush_grams=round(fl, 3) if fl is not None else None,
            tower_grams=round(tw, 3) if tw is not None else None))
    n.weight_g = round(sum(f.grams for f in n.filaments), 2) if n.filaments else None
    n.tower_grams = round(tower, 2) if n.filaments else None
    n.purge_grams = round(tower + flush, 2) if n.filaments else None
    for key, attr in (("filament_changes", "filament_changes"), ("ams_swaps", "ams_swaps"),
                      ("nozzle_switches", "nozzle_changes")):
        v = _field(u, key)
        if v is not None:
            setattr(n, attr, int(v))
    n.warnings = [str(_field(w, "text", "message", default=w)) for w in
                  (_field(res, "warnings") or [])]
    return n


BACKENDS: dict[str, type] = {"bambu_studio": BambuStudioBackend, "orcaslicer": OrcaSlicerBackend,
                             NATIVE: NativeSlicerBackend}


def get_backend(name: str) -> SlicerBackend:
    """The backend for PrintSettings.slicer. KeyError for an unknown name."""
    return BACKENDS[name]()


def backend_available(name: str) -> bool:
    """True when that slicer can run here (binary installed, or p2p_slicer's core present)."""
    try:
        return bool(get_backend(name).available())
    except KeyError:
        return False


def _project_printer(model: Path) -> str | None:
    try:
        with zipfile.ZipFile(model) as z:
            cfg = json.loads(z.read("Metadata/project_settings.config"))
            return cfg.get("printer_settings_id")
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return None


def _project_config(model: Path) -> dict:
    try:
        with zipfile.ZipFile(model) as z:
            return json.loads(z.read("Metadata/project_settings.config"))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return {}


@dataclass
class SliceInfo:
    print_time_s: float | None = None
    weight_g: float | None = None
    filaments: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    printer_model_id: str | None = None
    nozzle_diameters: str | None = None
    filament_maps: list[int] = field(default_factory=list)  # 1-based nozzle per project slot


def parse_slice_info(sliced_3mf: Path, plate: int = 1) -> SliceInfo:
    """Read Metadata/slice_info.config from a sliced .gcode.3mf."""
    with zipfile.ZipFile(sliced_3mf) as z:
        root = ET.fromstring(z.read("Metadata/slice_info.config"))
    info = SliceInfo()
    for p in root.iter("plate"):
        meta = {m.get("key"): m.get("value") for m in p.findall("metadata")}
        if meta.get("index") not in (None, str(plate)):
            continue
        if meta.get("prediction"):
            info.print_time_s = float(meta["prediction"])
        if meta.get("weight"):
            info.weight_g = float(meta["weight"])
        info.printer_model_id = meta.get("printer_model_id")
        info.nozzle_diameters = meta.get("nozzle_diameters")
        info.filament_maps = [int(x) for x in (meta.get("filament_maps") or "").split()
                              if x.isdigit()]
        for fil in p.findall("filament"):
            info.filaments.append(dict(fil.attrib))
        for w in p.findall("warning"):
            info.warnings.append(w.get("msg") or json.dumps(w.attrib))
    return info


def result_plate(result: dict, plate: int = 1) -> dict:
    """The plate entry of Bambu's result.json (`sliced_plates`), or {}."""
    plates = result.get("sliced_plates") if isinstance(result, dict) else None
    for p in plates or []:
        if isinstance(p, dict) and p.get("id", plate) == plate:
            return p
    return {}


def bambu_numbers(info: SliceInfo, result: dict, stats: GcodeStats | None) -> SliceNumbers:
    """Numbers from a Bambu-style slice. Exact where Bambu gives them (SOURCE: the truth set,
    docs/research/native-slicer-integration.md): grams and time from slice_info.config; model
    grams per filament and the AMS swap count from result.json; tower grams (extrude moves under
    "Prime tower") and the header's model printing time from the G-code. Flush per filament =
    Bambu's total minus model minus tower, so it follows Bambu's own billing. Without
    result.json the purge falls back to the G-code estimate and flush to the G-code's ";VG1"
    lines by the tool active at the time."""
    plate = result_plate(result)
    rj = {int(f["id"]): f for f in plate.get("filaments") or [] if "id" in f}
    n = SliceNumbers(print_time_s=info.print_time_s, weight_g=info.weight_g,
                     warnings=list(info.warnings))
    for fil in info.filaments:
        slot = int(fil.get("id", 0))
        grams = float(fil.get("used_g", 0) or 0)
        meters = float(fil["used_m"]) if fil.get("used_m") else None
        r = rj.get(slot)
        total = float(r["total_used_g"]) if r and "total_used_g" in r else grams
        model = float(r["main_used_g"]) if r and "main_used_g" in r else None
        tower = stats.tower_g_slot.get(slot, 0.0) if stats is not None else None
        if model is not None and tower is not None:
            flush = max(0.0, total - model - tower)
        elif stats is not None:
            flush = stats.flush_g_slot.get(slot, 0.0)
        else:
            flush = None
        n.filaments.append(SliceFilament(
            slot=slot, grams=grams, meters=meters,
            model_grams=round(model, 3) if model is not None else None,
            flush_grams=round(flush, 3) if flush is not None else None,
            tower_grams=round(tower, 3) if tower is not None else None))
    n.filaments.sort(key=lambda f: f.slot)
    if rj:
        n.purge_grams = round(sum(max(0.0, float(f.get("total_used_g", 0))
                                      - float(f.get("main_used_g", 0))) for f in rj.values()), 2)
    elif stats is not None and stats.total_g:
        n.purge_grams = stats.purge_total
    if stats is not None:
        n.tower_grams = round(sum(stats.tower_g_slot.values()), 2)
        n.filament_changes = stats.filament_changes
        n.nozzle_changes = stats.nozzle_switches or stats.nozzle_changes
        n.ams_swaps = stats.flushes
        if stats.model_time_s is not None and n.print_time_s is not None:
            n.model_time_s = min(float(stats.model_time_s), n.print_time_s)
            n.prepare_time_s = round(n.print_time_s - n.model_time_s, 1)
    if "filament_change_times" in plate:
        n.ams_swaps = int(plate["filament_change_times"])
    n.feature_time_s = {str(k): round(float(v), 2)
                        for k, v in (plate.get("feature_type_times") or {}).items()}
    return n


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def slice_check(
    path: str | Path,
    settings: PrintSettings,
    expected_slots: int | None,
    filament_ids: list[str] | None = None,
    keep_gcode: bool = False,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    out_dir: str | Path | None = None,
    job_root: str | Path | None = None,
    backend: SlicerBackend | None = None,
    verify_readback: bool = True,
    min_paint_width_mm: float | None = None,
) -> SliceReport:
    """Slice plate 1 headless. Pass = sliced without error, number of slots with filament use
    equals `expected_slots` (when given), and print time plus grams are present.

    keep_gcode: keep the .gcode.3mf (in out_dir, or next to the input) and report it.
    filament_ids: palette filament ids by slot (1-based index + 1) for the report.
    job_root: when set, FileRef paths are made relative to it (job-relative POSIX).
    verify_readback: read Bambu's re-saved copy of the model inside the sliced file and fail if
    any triangle's filament, the presets, colours or the default filament changed (skipped for
    backends that re-save no model, such as p2p_slicer).
    min_paint_width_mm: painted regions narrower than this are reported as warnings
    (default 2 x nozzle; 0 disables the audit).

    Bambu's result.json is kept next to the slicer log as <stem>.slice_result.json (seconds per
    feature have no SliceReport field yet)."""
    model = Path(path).resolve()
    if backend is None:
        backend = get_backend(settings.slicer)
    cfg = _project_config(model)
    printer_profile = cfg.get("printer_settings_id") or f"{settings.printer} {settings.nozzle_mm:g} nozzle"
    process_profile = cfg.get("print_settings_id") or ""
    base = SliceReport(ok=False, slicer=backend.name, slicer_version="unknown",
                       printer_profile=printer_profile, process_profile=process_profile,
                       expected_slots=expected_slots)
    if not model.exists():
        base.errors.append(f"file not found: {model}")
        return base
    if not backend.available():
        base.errors.append(f"{backend.name} is not installed")
        return base
    base.slicer_version = backend.version()

    keep_dir = Path(out_dir).resolve() if out_dir else model.parent
    tmp = Path(tempfile.mkdtemp(prefix="p2p_slice_"))
    try:
        raw = backend.slice(model, tmp, timeout_s)
        if base.slicer_version == "unknown":
            base.slicer_version = _log_version(raw.log_path) or "unknown"
        errors = list(raw.errors)
        warnings = list(raw.warnings)
        gcode = _sliced_gcode(raw.sliced_3mf) if raw.sliced_3mf is not None else None
        numbers = raw.numbers
        if numbers is None and raw.sliced_3mf is not None:
            info = SliceInfo()
            try:
                info = parse_slice_info(raw.sliced_3mf)
            except (KeyError, ET.ParseError, zipfile.BadZipFile) as e:
                errors.append(f"cannot read slice_info.config: {e}")
            totals = {int(f.get("id", 0)): float(f.get("used_g", 0) or 0) for f in info.filaments}
            stats = parse_gcode(gcode, totals, info.filament_maps or None) if gcode else None
            numbers = bambu_numbers(info, raw.result, stats)
        if numbers is not None:
            for w in numbers.warnings:
                if w not in warnings:
                    warnings.append(w)
        filaments: list[SliceFilament] = []
        for f in numbers.filaments if numbers is not None else []:
            fid = filament_ids[f.slot - 1] if filament_ids and 1 <= f.slot <= len(filament_ids) \
                else f.filament_id
            filaments.append(f.model_copy(update={"filament_id": fid}))
        filaments.sort(key=lambda f: f.slot)
        used = [f for f in filaments if f.grams > 0]
        slots_used = len(used) if numbers is not None else None
        total = (sum(f.grams for f in filaments) if filaments
                 else (numbers.weight_g if numbers is not None else None))

        ok = raw.ok
        min_w = 2 * settings.nozzle_mm if min_paint_width_mm is None else min_paint_width_mm
        thin_slots: list[int] = []
        thin_paint: list[dict] = []
        readback_ok: bool | None = None
        readback_slots: int | None = None
        if min_w > 0:
            try:
                src = read_3mf_full(model)
                audit = audit_paint(src.vertices, src.faces, src.face_slots, src.face_part,
                                    min_width_mm=min_w)
                warnings.extend(audit.warnings())
                thin_slots = audit.slots_only_thin
                for r in audit.thin:
                    fid = (filament_ids[r.slot - 1]
                           if filament_ids and 1 <= r.slot <= len(filament_ids) else None)
                    thin_paint.append({"slot": r.slot, "filament_id": fid,
                                       "area_mm2": r.area_mm2, "min_width_mm": r.width_mm,
                                       "part": r.part, "centroid": list(r.centroid)})
            except (ValueError, KeyError, zipfile.BadZipFile) as e:
                warnings.append(f"paint audit skipped: {e}")
        resaves = getattr(backend, "resaves_model", True)
        if verify_readback and resaves and raw.sliced_3mf is not None:
            try:
                rb = readback_check(model, raw.sliced_3mf)
            except (ValueError, KeyError, zipfile.BadZipFile) as e:
                errors.append(f"read-back failed: {e}")
                ok = False
            else:
                errors.extend(rb.errors)
                warnings.extend(rb.warnings)
                ok = ok and rb.ok
                readback_ok = rb.ok
                readback_slots = len(rb.slot_counts_bambu)
        if gcode:
            n_fil = len(cfg.get("filament_colour") or []) or 32
            bad = [b for b in invalid_tool_changes(gcode, n_fil) if b[2] != "end_gcode"]
            if bad:
                ok = False
                errors.append("invalid tool change in the print: " + ", ".join(
                    f"T{t} at G-code line {line}" for line, t, _ in bad[:5]))
        if ok and (numbers is None or numbers.print_time_s is None or not filaments):
            ok = False
            errors.append("slice output has no print time or filament usage")
        if ok and expected_slots is not None and slots_used != expected_slots:
            ok = False
            msg = f"slots used {slots_used} != expected {expected_slots}"
            missing = [s for s in thin_slots if s not in {f.slot for f in used}]
            if missing:
                msg += f"; slot(s) {missing} only had paint under {min_w:g} mm and did not print"
            errors.append(msg)

        log_ref = None
        output = None

        def ref(p: Path, kind: str, role: str) -> FileRef:
            rel = str(p)
            if job_root:
                try:
                    rel = p.resolve().relative_to(Path(job_root).resolve()).as_posix()
                except ValueError:
                    rel = str(p)
            return FileRef(path=rel, sha256=_sha256(p), bytes=p.stat().st_size, kind=kind,
                           role=role)

        keep_dir.mkdir(parents=True, exist_ok=True)
        stem = model.name.removesuffix(".3mf")
        if raw.log_path and raw.log_path.exists():
            dst_log = keep_dir / f"{stem}.slice.log"
            shutil.copyfile(raw.log_path, dst_log)
            log_ref = ref(dst_log, "log", "slicer_log").path
        if raw.result:
            (keep_dir / f"{stem}.slice_result.json").write_text(
                json.dumps(raw.result, indent=1, sort_keys=True))
        if keep_gcode and raw.sliced_3mf is not None:
            dst = keep_dir / f"{stem}.gcode.3mf"
            shutil.copyfile(raw.sliced_3mf, dst)
            output = ref(dst, "gcode.3mf", "sliced_3mf")

        n = numbers or SliceNumbers()
        return SliceReport(
            ok=ok,
            slicer=backend.name,
            slicer_version=base.slicer_version,
            printer_profile=printer_profile,
            process_profile=process_profile,
            errors=errors,
            warnings=warnings,
            print_time_s=n.print_time_s,
            filaments=filaments,
            total_grams=round(total, 2) if total is not None else None,
            purge_grams=n.purge_grams,
            slots_used=slots_used,
            expected_slots=expected_slots,
            output=output,
            log=log_ref,
            filament_changes=n.filament_changes,
            nozzle_changes=n.nozzle_changes,
            ams_swaps=n.ams_swaps,
            model_time_s=n.model_time_s,
            prepare_time_s=n.prepare_time_s,
            tower_grams=n.tower_grams,
            readback_ok=readback_ok,
            readback_slots=readback_slots,
            thin_paint=thin_paint,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _sliced_gcode(sliced_3mf: Path) -> str | None:
    try:
        with zipfile.ZipFile(sliced_3mf) as z:
            name = next((n for n in z.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", n)),
                        None)
            return z.read(name).decode("utf-8", "replace") if name else None
    except (OSError, zipfile.BadZipFile):
        return None

