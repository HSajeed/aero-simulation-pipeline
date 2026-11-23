"""Design parameters -> OpenVSP wing+fuselage -> CompGeom -> STL + .vsp3.

Parameter mapping (see configs/study_baseline.yaml):
  span              full span b; OpenVSP XSec_1 Span = b/2 (symmetric wing)
  sweep_le          XSec_1 Sweep, Sweep_Location 0 (leading edge)
  taper             tip chord = taper * root_chord
  twist_tip         washout: XSec_1 Twist = -twist_tip (about 0.25c), root twist 0
  fuselage_fineness circular fuselage, diameter = fuselage_length / fineness

Fixed layout: wing root LE at x = 0.35 L, wing z = -0.2 D (low-mid wing),
NACA 4-series default airfoil (sharp TE). Reference values: trapezoidal
planform from the centerline, moment reference at 0.25 MAC.

STL: geometry = OpenVSP's single-solid CompGeom export (closed). Its tagged
exports (multi-solid STL, Cart3D .tri) drop tiny junction slivers (~1e-7 m2)
and leave the surface open, so they are used only to LABEL triangles: each
single-solid triangle takes the solid (`wing`/`fuselage`) of the identical
tagged triangle; unmatched slivers take the label of an edge-sharing
neighbour (count recorded as stl_relabelled_triangles).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

WING_X_FRAC = 0.35
WING_Z_FRAC = -0.2


def planform(params: dict, root_chord: float, fuselage_length: float) -> dict:
    """Analytic reference values for the trapezoidal wing (both sides)."""
    b, lam, sweep = params["span"], params["taper"], params["sweep_le"]
    ct = lam * root_chord
    sref = b * root_chord * (1 + lam) / 2
    mac = 2 / 3 * root_chord * (1 + lam + lam**2) / (1 + lam)
    y_mac = b / 6 * (1 + 2 * lam) / (1 + lam)
    x_le_root = WING_X_FRAC * fuselage_length
    x_ref = x_le_root + y_mac * math.tan(math.radians(sweep)) + 0.25 * mac
    return {"span": b, "sref_full": sref, "sref_half": sref / 2, "mac": mac,
            "tip_chord": ct, "root_chord": root_chord, "aspect_ratio": b * b / sref,
            "x_le_root": x_le_root, "y_mac": y_mac, "x_ref": x_ref,
            "fuselage_length": fuselage_length,
            "fuselage_diameter": fuselage_length / params["fuselage_fineness"]}


def build(params: dict, settings: dict, out_dir: Path) -> dict:
    """Build the model, write body.stl and model.vsp3 into out_dir."""
    import openvsp as vsp

    geo, tess = settings["geometry"], settings["tessellation"]
    ref = planform(params, geo["root_chord"], geo["fuselage_length"])
    L, D = ref["fuselage_length"], ref["fuselage_diameter"]

    vsp.VSPRenew()
    vsp.ClearVSPModel()
    fus = vsp.AddGeom("FUSELAGE")
    vsp.SetParmVal(fus, "Length", "Design", L)
    vsp.SetParmVal(fus, "Tess_U", "Shape", tess["fus_tess_u"])
    vsp.SetParmVal(fus, "Tess_W", "Shape", tess["fus_tess_w"])
    xs = vsp.GetXSecSurf(fus, 0)
    for i in range(1, vsp.GetNumXSec(xs) - 1):          # interior ellipses -> circles of D
        x = vsp.GetXSec(xs, i)
        vsp.SetParmVal(vsp.GetXSecParm(x, "Ellipse_Width"), D)
        vsp.SetParmVal(vsp.GetXSecParm(x, "Ellipse_Height"), D)

    wing = vsp.AddGeom("WING", fus)
    vsp.SetParmVal(wing, "X_Rel_Location", "XForm", ref["x_le_root"])
    vsp.SetParmVal(wing, "Z_Rel_Location", "XForm", WING_Z_FRAC * D)
    vsp.SetDriverGroup(wing, 1, vsp.SPAN_WSECT_DRIVER, vsp.ROOTC_WSECT_DRIVER,
                       vsp.TIPC_WSECT_DRIVER)
    for name, val in [("Span", ref["span"] / 2), ("Root_Chord", ref["root_chord"]),
                      ("Tip_Chord", ref["tip_chord"]), ("Sweep", params["sweep_le"]),
                      ("Sweep_Location", 0.0), ("Twist", -params["twist_tip"]),
                      ("SectTess_U", tess["wing_sect_tess_u"])]:
        vsp.SetParmVal(wing, name, "XSec_1", val)
    vsp.SetParmVal(wing, "Tess_W", "Shape", tess["wing_tess_w"])
    vsp.Update()

    ref["vsp_wing_total_area"] = vsp.GetParmVal(wing, "TotalArea", "WingGeom")
    ref["vsp_wing_mac"] = vsp.GetParmVal(wing, "MAC", "WingGeom")
    ref["vsp_wing_span"] = vsp.GetParmVal(wing, "TotalSpan", "WingGeom")

    out_dir.mkdir(parents=True, exist_ok=True)
    vsp3 = (out_dir / "model.vsp3").resolve()
    vsp.SetVSP3FileName(str(vsp3))
    vsp.WriteVSPFile(str(vsp3))

    vsp.ComputeCompGeom(vsp.SET_ALL, False, 0)
    res = vsp.FindLatestResultsID("Comp_Geom")
    ref["compgeom_wet_area"] = dict(zip(vsp.GetStringResults(res, "Comp_Name"),
                                        vsp.GetDoubleResults(res, "Wet_Area")))
    ref["compgeom_total_wet_area"] = vsp.GetDoubleResults(res, "Total_Wet_Area")[0]
    veh = vsp.FindContainer("Vehicle", 0)
    multi = vsp.FindParm(veh, "MultiSolid", "STLSettings")
    single_stl, tagged_stl = out_dir / "body_vsp_single.stl", out_dir / "body_vsp_tagged.stl"
    vsp.SetParmVal(multi, 0.0)
    vsp.ExportFile(str(single_stl), vsp.SET_SHOWN, vsp.EXPORT_STL)
    vsp.SetParmVal(multi, 1.0)
    vsp.ExportFile(str(tagged_stl), vsp.SET_SHOWN, vsp.EXPORT_STL)
    vsp.SetParmVal(multi, 0.0)
    stl = out_dir / "body.stl"
    solids, relabelled = label_solids(single_stl, tagged_stl, stl)
    ref["stl_relabelled_triangles"] = relabelled
    tris = stl_triangles(stl)
    ref["n_triangles"] = len(tris)
    ref["stl_area"] = tri_area(tris)
    ref["stl_solid_area"] = {k: tri_area(stl_triangles(stl, k)) for k in solids}
    ref["stl_bbox"] = [tris.reshape(-1, 3).min(0).tolist(), tris.reshape(-1, 3).max(0).tolist()]
    ref["openvsp_version"] = vsp.GetVSPVersion()
    return ref


def _solid_name(vsp_name: str) -> str:
    if "WingGeom" in vsp_name:
        return "wing"
    if "FuselageGeom" in vsp_name:
        return "fuselage"
    raise ValueError(f"unexpected STL solid {vsp_name!r}")


def _vkey(v) -> tuple:
    return tuple(round(float(x), 9) for x in v)


def label_solids(single: Path, tagged: Path, dst: Path, max_relabel: int = 50
                 ) -> tuple[list[str], int]:
    """Write dst = triangles of `single`, grouped into solids wing/fuselage
    using the labels of `tagged`. Returns (solids, n_relabelled)."""
    tri_label: dict[frozenset, str] = {}
    edge_label: dict[frozenset, str] = {}
    cur = None
    for t, name in _iter_tris(tagged):
        lab = _solid_name(name)
        vs = [_vkey(v) for v in t]
        tri_label[frozenset(vs)] = lab
        for i in range(3):
            edge_label.setdefault(frozenset((vs[i], vs[(i + 1) % 3])), lab)
    groups: dict[str, list[np.ndarray]] = {"wing": [], "fuselage": []}
    pending = []
    for t, _ in _iter_tris(single):
        lab = tri_label.get(frozenset(_vkey(v) for v in t))
        (groups[lab].append(t) if lab else pending.append(t))
    for t in pending:                      # slivers missing from the tagged export
        vs = [_vkey(v) for v in t]
        labs = [edge_label.get(frozenset((vs[i], vs[(i + 1) % 3]))) for i in range(3)]
        lab = next((x for x in labs if x), None)
        if lab is None:
            raise ValueError("untagged triangle with no tagged neighbour")
        groups[lab].append(t)
    if len(pending) > max_relabel:
        raise ValueError(f"{len(pending)} triangles missing from tagged export (> {max_relabel})")
    if not groups["wing"] or not groups["fuselage"]:
        raise ValueError("expected both wing and fuselage triangles")
    with open(dst, "w") as fh:
        for k in ("wing", "fuselage"):
            fh.write(f"solid {k}\n")
            for t in groups[k]:
                n = np.cross(t[1] - t[0], t[2] - t[0])
                nn = np.linalg.norm(n)
                n = n / nn if nn > 0 else n
                fh.write(f" facet normal {n[0]:.10e} {n[1]:.10e} {n[2]:.10e}\n  outer loop\n")
                for v in t:
                    fh.write(f"   vertex {v[0]:.10e} {v[1]:.10e} {v[2]:.10e}\n")
                fh.write("  endloop\n endfacet\n")
            fh.write(f"endsolid {k}\n")
    return ["wing", "fuselage"], len(pending)


def _iter_tris(path: Path):
    """Yield (3x3 vertex array, solid name) from an ASCII STL."""
    cur, verts = "", []
    for ln in Path(path).read_text().splitlines():
        s = ln.strip()
        if s.startswith("solid"):
            cur = s[5:].strip()
        elif s.startswith("vertex"):
            verts.append([float(x) for x in s.split()[1:4]])
            if len(verts) == 3:
                yield np.asarray(verts), cur
                verts = []


def stl_triangles(path: Path, solid: str | None = None) -> np.ndarray:
    """(N,3,3) vertices from an ASCII STL, optionally one named solid only."""
    verts, cur = [], None
    for ln in Path(path).read_text().splitlines():
        s = ln.lstrip()
        if s.startswith("solid"):
            cur = s[5:].strip()
        elif s.startswith("vertex") and (solid is None or cur == solid):
            verts.append(s.split()[1:4])
    return np.asarray(verts, dtype=float).reshape(-1, 3, 3)


def tri_area(tris: np.ndarray) -> float:
    return float(0.5 * np.linalg.norm(
        np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1).sum())
