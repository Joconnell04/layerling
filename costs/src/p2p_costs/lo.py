"""Evaluate workbook formulas with LibreOffice headless.

A throwaway LibreOffice profile gets a one-line Basic macro that recalculates every formula and
saves the file in place, so the saved .xlsx carries computed values next to its formulas.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

MACRO = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE script:module PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "module.dtd">
<script:module xmlns:script="http://openoffice.org/2000/script" script:name="Module1"
 script:language="StarBasic">
Sub RecalculateAndSave()
  ThisComponent.calculateAll()
  ThisComponent.store()
  ThisComponent.close(True)
End Sub
</script:module>"""

CANDIDATES = ["soffice", "/opt/homebrew/bin/soffice",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice"]


class LibreOfficeMissing(RuntimeError):
    pass


def soffice() -> str:
    for c in CANDIDATES:
        p = shutil.which(c) or (c if Path(c).is_file() else None)
        if p:
            return p
    raise LibreOfficeMissing(
        "LibreOffice (soffice) is needed to evaluate the workbook formulas: brew install "
        "--cask libreoffice")


def available() -> bool:
    try:
        soffice()
        return True
    except LibreOfficeMissing:
        return False


def recalc_in_place(path: Path, timeout: int = 120) -> None:
    exe = soffice()
    env = dict(os.environ, SAL_USE_VCLPLUGIN="svp")
    with tempfile.TemporaryDirectory(prefix="p2p-costs-lo-") as prof:
        url = Path(prof).as_uri()
        subprocess.run([exe, "--headless", "--terminate_after_init",
                        f"-env:UserInstallation={url}"], capture_output=True, timeout=timeout,
                       env=env, check=False)
        macro_dir = Path(prof) / "user" / "basic" / "Standard"
        if not macro_dir.is_dir():
            raise RuntimeError("LibreOffice did not create a profile; formulas not evaluated")
        (macro_dir / "Module1.xba").write_text(MACRO)
        before = path.stat().st_mtime_ns
        res = subprocess.run(
            [exe, "--headless", "--norestore", f"-env:UserInstallation={url}",
             ("vnd.sun.star.script:Standard.Module1.RecalculateAndSave?language=Basic"
              "&location=application"), str(path.resolve())],
            capture_output=True, text=True, timeout=timeout, env=env, check=False)
        if res.returncode != 0 or path.stat().st_mtime_ns == before:
            raise RuntimeError(
                "LibreOffice did not recalculate the workbook (is another LibreOffice window "
                f"open?): {res.stderr.strip()[:300]}")
