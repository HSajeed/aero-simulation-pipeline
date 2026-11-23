"""VSPAERO (vortex lattice) reference results: the cheap oracle for Gate 3.

Runs on the full (symmetric) model with full-model reference area; CL/CDi/Cm
are directly comparable to half-model CFD with half reference area.
Mach is set from U_inf (incompressible regime, a = 340.3 m/s).
Geometry: WING ONLY (human decision 2026-11-23). Wing+fuselage produced
invalid panels at the junction in 8/10 baseline runs and unusable CL in 3/10;
wing-only had none. The reference therefore excludes fuselage effects.
A VSPAERO run is a failure (no reference stored; Gate 3 flags the run) if its
log reports invalid panels or any result is non-finite.
"""

from __future__ import annotations

import ctypes
import math
import os
from contextlib import contextmanager
from pathlib import Path

SPEED_OF_SOUND = 340.3
PANEL_ERROR_MARKERS = ("Faces have errors", "PGFace Invalid")


def check_log(text: str) -> None:
    for marker in PANEL_ERROR_MARKERS:
        if marker in text:
            raise ValueError(f"VSPAERO geometry error in log: {marker!r}")


@contextmanager
def redirect_fds(log_path: Path):
    """VSPAERO writes to the process stdout/stderr at fd level; send it to a log."""
    libc = ctypes.CDLL(None)
    with open(log_path, "w") as log:
        libc.fflush(None)
        saved = os.dup(1), os.dup(2)
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        try:
            yield
        finally:
            libc.fflush(None)     # C stdio buffers must land in the log, not later on the tty
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])


def run(vsp3: Path, alphas: list[float], ref: dict, freestream: dict, log_path: Path) -> list[dict]:
    """Reloads the .vsp3 written by openvsp_build.build (saved before CompGeom,
    which hides the components) into a clean model."""
    import openvsp as vsp

    vsp.VSPRenew()
    vsp.ClearVSPModel()
    vsp.ReadVSPFile(str(Path(vsp3).resolve()))
    vsp.SetVSP3FileName(str(Path(vsp3).resolve()))
    g, s = "VSPAEROComputeGeometry", "VSPAEROSweep"
    vsp.SetAnalysisInputDefaults(g)
    vsp.SetAnalysisInputDefaults(s)
    wings = [x for x in vsp.FindGeoms() if vsp.GetGeomTypeName(x) == "Wing"]
    if len(wings) != 1:
        raise ValueError(f"expected exactly one wing, found {len(wings)}")
    for x in vsp.FindGeoms():
        vsp.SetSetFlag(x, vsp.SET_FIRST_USER, x == wings[0])
    vsp.SetIntAnalysisInput(g, "ThinGeomSet", [vsp.SET_FIRST_USER])   # VLM: wing only
    vsp.SetIntAnalysisInput(g, "GeomSet", [vsp.SET_NONE])             # no panel bodies
    inputs = {
        "AlphaStart": [float(min(alphas))], "AlphaEnd": [float(max(alphas))],
        "MachStart": [freestream["U_inf"] / SPEED_OF_SOUND],
        "Vinf": [freestream["U_inf"]], "Rho": [freestream["rho_inf"]],
        "ReCref": [freestream["U_inf"] * ref["mac"] / freestream["nu"]],
        "Sref": [ref["sref_full"]], "bref": [ref["span"]], "cref": [ref["mac"]],
        "Xcg": [ref["x_ref"]], "Ycg": [0.0], "Zcg": [0.0],
    }
    for k, v in inputs.items():
        vsp.SetDoubleAnalysisInput(s, k, v)
    for k, v in {"AlphaNpts": [len(alphas)], "MachNpts": [1], "NCPU": [1], "RefFlag": [0]}.items():
        vsp.SetIntAnalysisInput(s, k, v)
    with redirect_fds(log_path):
        vsp.ExecAnalysis(g)
        vsp.ExecAnalysis(s)
    check_log(Path(log_path).read_text(errors="replace"))
    polar = Path(vsp3).with_suffix(".polar")
    return parse_polar(polar.read_text())


def parse_polar(text: str) -> list[dict]:
    """VSPAERO .polar -> [{alpha, CL, CDi, Cm}] using CLtot, CDi, CMytot."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    hdr_i = next(i for i, ln in enumerate(lines) if ln.split()[:3] == ["Beta", "Mach", "AoA"])
    hdr = lines[hdr_i].split()
    rows = []
    for ln in lines[hdr_i + 1:]:
        d = dict(zip(hdr, ln.split()))
        row = {"alpha": float(d["AoA"]), "CL": float(d["CLtot"]),
               "CDi": float(d["CDi"]), "Cm": float(d["CMytot"])}
        if not all(math.isfinite(v) for v in row.values()):
            raise ValueError(f"VSPAERO non-finite result at alpha={row['alpha']}: {row}")
        rows.append(row)
    if not rows:
        raise ValueError("VSPAERO polar has no result rows")
    return rows
