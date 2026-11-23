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
    stl = out_dir / "body.stl"
    vsp.ExportFile(str(stl), vsp.SET_SHOWN, vsp.EXPORT_STL)
    tris = stl_triangles(stl)
    ref["n_triangles"] = len(tris)
    ref["stl_area"] = tri_area(tris)
    ref["stl_bbox"] = [tris.reshape(-1, 3).min(0).tolist(), tris.reshape(-1, 3).max(0).tolist()]
    ref["openvsp_version"] = vsp.GetVSPVersion()
    return ref


def stl_triangles(path: Path) -> np.ndarray:
    """(N,3,3) vertices from an ASCII STL."""
    verts = [ln.split()[1:4] for ln in Path(path).read_text().splitlines()
             if ln.lstrip().startswith("vertex")]
    return np.asarray(verts, dtype=float).reshape(-1, 3, 3)


def tri_area(tris: np.ndarray) -> float:
    return float(0.5 * np.linalg.norm(
        np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1).sum())
