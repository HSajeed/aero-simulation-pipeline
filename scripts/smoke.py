"""Phase 0 smoke test: OpenVSP wing+fuselage -> CompGeom -> STL -> OpenFOAM
surfaceCheck -> blockMesh (half box) + surfaceFeatureExtract + snappyHexMesh
-> checkMesh. Records cell count, mesh wall time, snapped vs STL wetted area.

Spike code: dictionaries are inline here on purpose; Phase 3 moves them to
Jinja templates. Output: runs/_smoke/<timestamp>/ (smoke.log, smoke.json).

Usage: .venv/bin/python scripts/smoke.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aero.foam.container import IMAGE, run_script  # noqa: E402

OUT = ROOT / "runs" / "_smoke" / datetime.now().strftime("%Y%m%d-%H%M%S")
LOG_LINES: list[str] = []


def log(msg: str = "") -> None:
    print(msg, flush=True)
    LOG_LINES.append(msg)


# --------------------------------------------------------------------- geometry
def build_geometry(stl_path: Path) -> dict:
    import openvsp as vsp

    vsp.VSPRenew()
    fus = vsp.AddGeom("FUSELAGE")
    vsp.SetParmVal(fus, "Length", "Design", 10.0)
    vsp.SetParmVal(fus, "Tess_U", "Shape", 33)
    vsp.SetParmVal(fus, "Tess_W", "Shape", 33)

    wing = vsp.AddGeom("WING", fus)
    vsp.SetParmVal(wing, "X_Rel_Location", "XForm", 3.5)
    vsp.SetParmVal(wing, "Z_Rel_Location", "XForm", -0.3)
    vsp.SetDriverGroup(wing, 1, vsp.SPAN_WSECT_DRIVER, vsp.ROOTC_WSECT_DRIVER,
                       vsp.TIPC_WSECT_DRIVER)
    for name, val in [("Span", 5.0), ("Root_Chord", 2.0), ("Tip_Chord", 1.0),
                      ("Sweep", 10.0), ("SectTess_U", 25)]:
        vsp.SetParmVal(wing, name, "XSec_1", val)
    vsp.SetParmVal(wing, "Tess_W", "Shape", 41)
    vsp.Update()

    vsp.ComputeCompGeom(vsp.SET_ALL, False, 0)
    res = vsp.FindLatestResultsID("Comp_Geom")
    names = list(vsp.GetStringResults(res, "Comp_Name"))
    wet = list(vsp.GetDoubleResults(res, "Wet_Area"))
    total_wet = vsp.GetDoubleResults(res, "Total_Wet_Area")[0]
    vsp.ExportFile(str(stl_path), vsp.SET_SHOWN, vsp.EXPORT_STL)
    vsp.WriteVSPFile(str(stl_path.with_suffix(".vsp3")))
    return {"openvsp_version": vsp.GetVSPVersion(),
            "compgeom_wet_area": dict(zip(names, wet)),
            "compgeom_total_wet_area": total_wet}


def stl_triangles(path: Path) -> np.ndarray:
    """(N,3,3) vertices from an ASCII STL."""
    verts = [list(map(float, ln.split()[1:4]))
             for ln in path.read_text().splitlines() if ln.strip().startswith("vertex")]
    return np.asarray(verts).reshape(-1, 3, 3)


def tri_area(tris: np.ndarray) -> float:
    return float(0.5 * np.linalg.norm(
        np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1).sum())


# ------------------------------------------------------------- OpenFOAM dicts
HEADER = """FoamFile {{ version 2.0; format ascii; class dictionary; object {obj}; }}
"""

# Half-model box, y >= 0. Body: fuselage x 0..10, half span 5.
X0, X1, Y0, Y1, Z0, Z1 = -25.0, 45.0, 0.0, 30.0, -30.0, 30.0
BASE = 2.5  # m, background cell size
NX, NY, NZ = (round((X1 - X0) / BASE), round((Y1 - Y0) / BASE), round((Z1 - Z0) / BASE))

DICTS = {
    "system/controlDict": """
application snappyHexMesh; startFrom startTime; startTime 0; stopAt endTime;
endTime 0; deltaT 1; writeControl timeStep; writeInterval 1;
writeFormat binary; writePrecision 8; runTimeModifiable false;
""",
    "system/fvSchemes": """
ddtSchemes { default steadyState; } gradSchemes { default Gauss linear; }
divSchemes { default none; } laplacianSchemes { default Gauss linear corrected; }
interpolationSchemes { default linear; } snGradSchemes { default corrected; }
""",
    "system/fvSolution": "\n",
    "system/blockMeshDict": f"""
scale 1;
vertices (
 ({X0} {Y0} {Z0}) ({X1} {Y0} {Z0}) ({X1} {Y1} {Z0}) ({X0} {Y1} {Z0})
 ({X0} {Y0} {Z1}) ({X1} {Y0} {Z1}) ({X1} {Y1} {Z1}) ({X0} {Y1} {Z1})
);
blocks ( hex (0 1 2 3 4 5 6 7) ({NX} {NY} {NZ}) simpleGrading (1 1 1) );
boundary (
 symmetry {{ type symmetryPlane; faces ((0 1 5 4)); }}
 farfield {{ type patch; faces ((3 7 6 2) (0 4 7 3) (1 2 6 5) (0 3 2 1) (4 5 6 7)); }}
);
""",
    "system/surfaceFeatureExtractDict": """
body.stl { extractionMethod extractFromSurface; includedAngle 150;
           subsetFeatures { nonManifoldEdges no; openEdges yes; } writeObj no; }
""",
    "system/snappyHexMeshDict": """
castellatedMesh true; snap true; addLayers false;
geometry {
  body.stl { type triSurfaceMesh; name body; }
  nearBody { type searchableBox; min (-2 0 -3); max (18 8 3); }
}
castellatedMeshControls {
  maxLocalCells 2000000; maxGlobalCells 4000000; minRefinementCells 10;
  maxLoadUnbalance 0.10; nCellsBetweenLevels 3;
  features ( { file "body.eMesh"; level 6; } );
  refinementSurfaces { body { level (5 6); patchInfo { type wall; } } }
  resolveFeatureAngle 30;
  refinementRegions { nearBody { mode inside; levels ((1E15 3)); } }
  locationInMesh (-20.13 20.17 20.11);
  allowFreeStandingZoneFaces true;
}
snapControls {
  nSmoothPatch 3; tolerance 2.0; nSolveIter 50; nRelaxIter 5;
  nFeatureSnapIter 10; implicitFeatureSnap false; explicitFeatureSnap true;
  multiRegionFeatureSnap false;
}
addLayersControls {
  relativeSizes true; layers {}; expansionRatio 1.0; finalLayerThickness 0.3;
  minThickness 0.1; nGrow 0; featureAngle 60; nRelaxIter 3;
  nSmoothSurfaceNormals 1; nSmoothNormals 3; nSmoothThickness 10;
  maxFaceThicknessRatio 0.5; maxThicknessToMedialRatio 0.3;
  minMedialAxisAngle 90; nBufferCellsNoExtrude 0; nLayerIter 50;
}
meshQualityControls {
  #includeEtc "caseDicts/mesh/generation/meshQualityDict.cfg"
}
mergeTolerance 1e-6;
""",
}


def write_case(case: Path, stl: Path) -> None:
    for rel, body in DICTS.items():
        p = case / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(HEADER.format(obj=p.name) + body)
    tri = case / "constant" / "triSurface"
    tri.mkdir(parents=True, exist_ok=True)
    shutil.copy(stl, tri / "body.stl")


# ---------------------------------------------------------------------- stages
STAGES = [
    ("surfaceCheck", "surfaceCheck -checkSelfIntersection constant/triSurface/body.stl"),
    ("blockMesh", "blockMesh"),
    ("surfaceFeatureExtract", "surfaceFeatureExtract"),
    ("snappyHexMesh", "snappyHexMesh -overwrite"),
    ("checkMesh", "checkMesh"),
    ("surfaceMeshExtract",
     "surfaceMeshExtract -patches '(body)' constant/triSurface/snapped_body.stl"),
]


def grep(text: str, pattern: str, default=None, cast=str):
    m = re.search(pattern, text, re.M)
    return cast(m.group(1)) if m else default


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    geom_dir, case = OUT / "geom", OUT / "mesh"
    geom_dir.mkdir()
    report: dict = {"image": IMAGE, "out": str(OUT), "stages": {}}

    log(f"== smoke test -> {OUT}")
    t0 = time.monotonic()
    stl = geom_dir / "body.stl"
    report["geometry"] = build_geometry(stl)
    tris = stl_triangles(stl)
    stl_area = tri_area(tris)
    report["geometry"].update(stl_triangles=len(tris), stl_area_full=stl_area,
                              stl_bbox=[tris.reshape(-1, 3).min(0).tolist(),
                                        tris.reshape(-1, 3).max(0).tolist()],
                              wall_time_s=time.monotonic() - t0)
    log(f"[geom] {report['geometry']['openvsp_version']}: {len(tris)} triangles, "
        f"STL area {stl_area:.3f} m2 (CompGeom total wet "
        f"{report['geometry']['compgeom_total_wet_area']:.3f} m2)")

    write_case(case, stl)
    mesh_time = 0.0
    for name, cmd in STAGES:
        r = run_script(case, cmd, name)
        report["stages"][name] = {"rc": r.returncode, "wall_time_s": round(r.wall_time_s, 2),
                                  "log": str(r.log_path.relative_to(OUT))}
        if name in ("blockMesh", "surfaceFeatureExtract", "snappyHexMesh"):
            mesh_time += r.wall_time_s
        log(f"[{name}] rc={r.returncode} {r.wall_time_s:.1f}s")
        if r.returncode != 0:
            log(f"   FAILED, tail of {r.log_path}:")
            for ln in r.log_path.read_text().splitlines()[-25:]:
                log("   | " + ln)
            report["ok"] = False
            break
    else:
        report["ok"] = True

    # --- extract numbers
    def text(name):
        p = case / f"log.{name}"
        return p.read_text() if p.exists() else ""

    sc = text("surfaceCheck")
    if sc:
        report["surfaceCheck"] = {
            "closed": "Surface is closed" in sc,
            "self_intersecting": None if "self-intersect" not in sc.lower()
            else "Surface is self-intersecting" in sc,
            "key_lines": [ln.strip() for ln in sc.splitlines() if re.search(
                r"closed|intersect|illegal|Triangles|Edges|Bounding|quality|connected|zone|region",
                ln, re.I)][:25],
        }
        log("[surfaceCheck] " + "\n               ".join(report["surfaceCheck"]["key_lines"]))
    cm = text("checkMesh")
    if cm:
        report["checkMesh"] = {
            "mesh_ok": "Mesh OK." in cm,
            "cells": grep(cm, r"^\s*cells:\s+(\d+)", cast=int),
            "max_non_orth": grep(cm, r"Mesh non-orthogonality Max:\s+([\d.eE+-]+)", cast=float),
            "max_skewness": grep(cm, r"Max skewness = ([\d.eE+-]+)", cast=float),
            "max_aspect_ratio": grep(cm, r"Max aspect ratio = ([\d.eE+-]+)", cast=float),
            "failed_checks": grep(cm, r"Failed (\d+) mesh checks", 0, int),
            "patches": re.findall(r"^\s+(\w+)\s+(\d+)\s+\d+\s+(ok|.+)$",
                                  cm.split("Checking patch topology")[-1].split("Checking")[0],
                                  re.M),
        }
        log(f"[checkMesh] {json.dumps({k: v for k, v in report['checkMesh'].items() if k != 'patches'})}")
    snapped = case / "constant" / "triSurface" / "snapped_body.stl"
    if snapped.exists():
        a_snap = tri_area(stl_triangles(snapped))
        a_ref = stl_area / 2.0  # body is symmetric about y=0; half model keeps y>=0
        report["area"] = {"snapped_half": a_snap, "stl_half": a_ref, "ratio": a_snap / a_ref}
        log(f"[area] snapped {a_snap:.3f} m2 vs STL/2 {a_ref:.3f} m2 -> ratio {a_snap / a_ref:.4f}")

    report["mesh_wall_time_s"] = round(mesh_time, 1)
    report["total_wall_time_s"] = round(time.monotonic() - t0, 1)
    st = (case / "constant" / "polyMesh" / "owner").stat() if (case / "constant/polyMesh/owner").exists() else None
    report["host_ownership_ok"] = bool(st and st.st_uid == os.getuid())
    log(f"[summary] ok={report['ok']} mesh_wall_time={report['mesh_wall_time_s']}s "
        f"total={report['total_wall_time_s']}s host_owned={report['host_ownership_ok']}")

    (OUT / "smoke.json").write_text(json.dumps(report, indent=2))
    (OUT / "smoke.log").write_text("\n".join(LOG_LINES) + "\n")
    return 0 if report["ok"] and report.get("checkMesh", {}).get("mesh_ok") else 1


if __name__ == "__main__":
    sys.exit(main())
