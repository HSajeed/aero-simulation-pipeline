"""Render OpenFOAM case directories from the Jinja templates.

Mesh domain (half model, y >= 0), all in root chords c from the body bbox:
  x: [xmin - upstream*c, xmax + downstream*c]
  y: [0, ymax + lateral*c]
  z: [zmin - vertical*c, zmax + vertical*c]
extents rounded up to whole background cells (of domain_cell_size if set, so
meshes of different base size share one domain). Refinement boxes:
  nearBody: body bbox (y >= 0) grown by 0.5 c
  wake:     wing root TE -> xmax + wake_length*c, y to tip + 0.25 c,
            z around the wing, opened upward by tan(max alpha) (wake follows U)
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import jinja2

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "templates" / "openfoam"
MESH_FILES = {
    "system/controlDict": "controlDict.j2",
    "system/fvSchemes": "fvSchemes.j2",
    "system/fvSolution": "fvSolution.j2",
    "system/blockMeshDict": "blockMeshDict.j2",
    "system/surfaceFeatureExtractDict": "surfaceFeatureExtractDict.j2",
    "system/snappyHexMeshDict": "snappyHexMeshDict.j2",
}


def _env(sub: str) -> jinja2.Environment:
    return jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES / sub),
                              undefined=jinja2.StrictUndefined, keep_trailing_newline=True)


def _r(v: float) -> float:
    return round(float(v), 6)


def mesh_layout(geom: dict, mesh: dict, alphas: list[float]) -> dict:
    """Domain, refinement boxes and locationInMesh from geometry data."""
    (bx0, _, bz0), (bx1, by1, bz1) = geom["stl_bbox"]
    c, h = geom["root_chord"], mesh["base_cell_size"]

    ref = mesh.get("domain_cell_size") or h

    def axis(lo, hi):
        # extent in whole `ref` cells; with domain_cell_size set, meshes of any
        # base size share this domain (cells may deviate from cubes by < 1/(2n))
        length = math.ceil((hi - lo) / ref) * ref
        n = max(1, round(length / h))
        return _r(lo), _r(lo + length), n

    x0, x1, nx = axis(bx0 - mesh["upstream_chords"] * c, bx1 + mesh["downstream_chords"] * c)
    y0, y1, ny = axis(0.0, by1 + mesh["lateral_chords"] * c)
    z0, z1, nz = axis(bz0 - mesh["vertical_chords"] * c, bz1 + mesh["vertical_chords"] * c)

    g = 0.5 * c
    near = {"min": [_r(bx0 - g), 0.0, _r(bz0 - g)], "max": [_r(bx1 + g), _r(by1 + g), _r(bz1 + g)]}

    zw = -0.2 * geom["fuselage_diameter"]            # wing z (openvsp_build.WING_Z_FRAC)
    x_te = geom["x_le_root"] + c
    x_end = bx1 + mesh["wake_length_chords"] * c
    rise = (x_end - x_te) * math.tan(math.radians(max(max(alphas), 0.0)))
    wake = {"min": [_r(x_te), 0.0, _r(zw - 0.5 * c)],
            "max": [_r(x_end), _r(geom["span"] / 2 + 0.25 * c), _r(zw + 0.5 * c + rise)]}

    # inside the fluid, near a far corner, off any cell face
    loc = [_r(x0 + 1.37 * h), _r(y1 - 1.41 * h), _r(z1 - 1.29 * h)]
    return {"x0": x0, "x1": x1, "nx": nx, "y0": y0, "y1": y1, "ny": ny,
            "z0": z0, "z1": z1, "nz": nz, "nb": near, "wk": wake, "loc": loc,
            "background_cells": nx * ny * nz}


def write_mesh_case(case: Path, stl: Path, geom: dict, mesh: dict, alphas: list[float],
                    attempt_tag: str) -> dict:
    layout = mesh_layout(geom, mesh, alphas)
    env = _env("mesh")
    for rel, tpl in MESH_FILES.items():
        p = case / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(env.get_template(tpl).render(object=p.name, m=mesh, **layout))
    tri = case / "constant" / "triSurface"
    tri.mkdir(parents=True, exist_ok=True)
    shutil.copy(stl, tri / "body.stl")
    layout["script"] = env.get_template("mesh.sh.j2").render(attempt=attempt_tag)
    return layout


SOLVE_FILES = {
    "0/U": "U.j2", "0/p": "p.j2",
    "constant/transportProperties": "transportProperties.j2",
    "constant/turbulenceProperties": "turbulenceProperties.j2",
    "system/controlDict": "controlDict.j2",
    "system/fvSchemes": "fvSchemes.j2",
    "system/fvSolution": "fvSolution.j2",
}
DIV = {"linearUpwind": "linearUpwind grad(U)", "upwind": "upwind"}


def freestream_dirs(alpha_deg: float) -> tuple[list[float], list[float]]:
    """(drag_dir, lift_dir) for flow in the x-z plane at angle alpha."""
    a = math.radians(alpha_deg)
    return [_r(math.cos(a)), 0.0, _r(math.sin(a))], [_r(-math.sin(a)), 0.0, _r(math.cos(a))]


def write_solve_system(case: Path, geom: dict, settings: dict, alpha: float, end_time: int,
                       residual_control: bool, change_alpha: bool, tag: str) -> str:
    """Re-render system/ for a production leg (alpha, endTime, residualControl,
    final div scheme) and return its script. With change_alpha, the latest time's
    farfield freestreamValue is switched to the new alpha (changeDictionary works
    on binary fields); the internal field is the warm start."""
    fs, s = settings["freestream"], settings["solver"]
    drag, lift = freestream_dirs(alpha)
    U = [_r(fs["U_inf"] * drag[0]), 0.0, _r(fs["U_inf"] * drag[2])]
    ctx = {"U": U, "rho_inf": fs["rho_inf"], "U_inf": fs["U_inf"],
           "cofr": [_r(geom["x_ref"]), 0.0, 0.0], "lift_dir": lift, "drag_dir": drag,
           "l_ref": _r(geom["mac"]), "a_ref": _r(geom["sref_half"]),
           "div_u": DIV[s["div_scheme_U"]], "end_time": end_time,
           "residual_control": residual_control, "s": s}
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES / "solve"),
                             undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    for rel in ("system/controlDict", "system/fvSchemes", "system/fvSolution"):
        p = case / rel
        p.write_text(env.get_template(SOLVE_FILES[rel]).render(object=p.name, **ctx))
    lines = [f"# production leg (tag {tag}): alpha = {alpha} deg, endTime {end_time}"]
    if change_alpha:
        (case / "system" / "changeDictionaryDict").write_text(
            "FoamFile { version 2.0; format ascii; class dictionary; object changeDictionaryDict; }\n"
            f"U {{ boundaryField {{ farfield {{ freestreamValue uniform ({' '.join(map(str, U))}); }} }} }}\n")
        lines.append(f"changeDictionary -latestTime > log.changeDictionary.{tag} 2>&1")
    lines.append(f"simpleFoam > log.simpleFoam.{tag} 2>&1")
    return "\n".join(lines) + "\n"


def write_solve_case(case: Path, mesh_case: Path, geom: dict, settings: dict, alpha: float,
                     tag: str, end_time: int, residual_control: bool,
                     templates: Path | None = None) -> str:
    """Write 0/, constant/, system/ for simpleFoam at `alpha`; copy the mesh.
    Physics comes only from settings['physics'] (v1, Literal-locked) and the
    templates; numerics from settings['solver']. Returns the pilot script."""
    fs, s = settings["freestream"], settings["solver"]
    drag, lift = freestream_dirs(alpha)
    U = [_r(fs["U_inf"] * drag[0]), 0.0, _r(fs["U_inf"] * drag[2])]
    first_div = "upwind" if s["upwind_startup_iters"] > 0 else s["div_scheme_U"]
    ctx = {"U": U, "nu": fs["nu"], "rho_inf": fs["rho_inf"], "U_inf": fs["U_inf"],
           "cofr": [_r(geom["x_ref"]), 0.0, 0.0], "lift_dir": lift, "drag_dir": drag,
           "l_ref": _r(geom["mac"]), "a_ref": _r(geom["sref_half"]),
           "div_u": DIV[first_div], "final_div": DIV[s["div_scheme_U"]],
           "end_time": end_time, "residual_control": residual_control, "s": s,
           "alpha": alpha, "tag": tag}
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(templates or TEMPLATES / "solve"),
                             undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    for rel, tpl in SOLVE_FILES.items():
        p = case / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(env.get_template(tpl).render(object=p.name, **ctx))
    shutil.copytree(mesh_case / "constant" / "polyMesh", case / "constant" / "polyMesh",
                    dirs_exist_ok=True)
    return env.get_template("pilot.sh.j2").render(**ctx)
