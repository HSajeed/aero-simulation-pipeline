"""Render OpenFOAM case directories from the Jinja templates.

Mesh domain (half model, y >= 0), all in root chords c from the body bbox:
  x: [xmin - upstream*c, xmax + downstream*c]
  y: [0, ymax + lateral*c]
  z: [zmin - vertical*c, zmax + vertical*c]
extents rounded up to whole background cells. Refinement boxes:
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

    def axis(lo, hi):
        n = math.ceil((hi - lo) / h)
        return _r(lo), _r(lo + n * h), n

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
