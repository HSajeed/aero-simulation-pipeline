"""`aero doctor`: check the environment. Returns (name, ok, detail) rows."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from aero.foam.container import IMAGE


def _check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:  # report, don't crash
        ok, detail = False, f"{type(e).__name__}: {e}"
    return name, ok, detail


def run_checks(engine: str = "podman", image: str = IMAGE,
               runs_dir: Path = Path("runs")) -> list[tuple[str, bool, str]]:
    def python():
        v = sys.version_info
        return v >= (3, 14), f"{sys.executable} {v.major}.{v.minor}.{v.micro}"

    def openvsp():
        import openvsp as vsp
        return True, vsp.GetVSPVersion()

    def vspaero():
        p = shutil.which("vspaero")
        return p is not None, p or "not on PATH"

    def engine_bin():
        p = shutil.which(engine)
        if p is None:
            return False, f"{engine} not on PATH"
        v = subprocess.run([engine, "--version"], capture_output=True, text=True, timeout=30)
        return v.returncode == 0, v.stdout.strip()

    def image_present():
        r = subprocess.run([engine, "image", "inspect", image, "--format", "{{.Id}}"],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0, (r.stdout.strip()[:12] + "  " + image) if r.returncode == 0 \
            else f"missing: {engine} pull {image}"

    def runs_writable():
        runs_dir.mkdir(parents=True, exist_ok=True)
        return os.access(runs_dir, os.W_OK), str(runs_dir.resolve())

    return [_check("python", python), _check("import openvsp", openvsp),
            _check("vspaero", vspaero), _check(f"engine ({engine})", engine_bin),
            _check("openfoam image", image_present), _check("runs dir", runs_writable)]
