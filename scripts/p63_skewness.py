"""P6-3 experiment (Phase 7): which mesh settings fix SKEWNESS_HIGH?

Re-meshes the harsh-study runs that ended UNRECOVERABLE with SKEWNESS_HIGH,
from their *initial* settings (frozen config, no corrections) plus one variant
of mesh settings each, and evaluates Gate 1 on the result. Uses each run's own
STL and geometry record; writes only to --out (never to runs/<study>/ or the
run DB, decision E5). Results: <out>/results.json (+ copy with --save).

  .venv/bin/python scripts/p63_skewness.py [--variants a,b] [--workers 4]
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from aero import db
from aero.config import StudyConfig, load_gates
from aero.foam.case import write_mesh_case
from aero.foam.container import run_script
from aero.foam.parse import parse_check_mesh, parse_snappy
from aero.gates import gate1
from aero.geom.openvsp_build import stl_triangles, tri_area

VARIANTS: dict[str, dict] = {
    "base": {},                                           # = attempt 0 of the original run
    "smooth5": {"n_smooth_patch": 5},                     # current step 1
    "ncbl4": {"n_cells_between_levels": 4},               # current step 2
    "relax10": {"n_relax_iter": 10},                      # current step 3
    "bskew4": {"max_boundary_skewness": 4.0},             # sHM boundary limit = Gate 1 limit
    "fsnap20": {"n_feature_snap_iter": 20},
    "tol1": {"snap_tolerance": 1.0},
    "bskew4_smooth5": {"max_boundary_skewness": 4.0, "n_smooth_patch": 5},
    "bskew3": {"max_boundary_skewness": 3.0},
}


def mesh_one(runs_dir: Path, out: Path, run_id: str, variant: str, g1: dict) -> dict:
    con = db.connect(runs_dir / "runs.db")
    study = db.get_run(con, run_id)["study"]
    rdir = runs_dir / study / run_id
    geom = json.loads(con.execute("SELECT data FROM geometry WHERE run=?", (run_id,)).fetchone()[0])
    frozen = yaml.safe_load((rdir / "config.yaml").read_text())
    s = StudyConfig.model_validate(frozen["study"]).model_dump(mode="json")
    mesh = {**s["mesh"], **VARIANTS[variant]}
    case = out / run_id / variant
    case.mkdir(parents=True, exist_ok=True)
    stl = rdir / "geom" / "body.stl"
    layout = write_mesh_case(case, stl, geom, mesh, list(s["alphas"]), "x")
    r = run_script(case, layout["script"], "mesh.x", engine=s["container_engine"],
                   image=s["image"], timeout=3600)
    read = lambda n: (case / f"log.{n}.x").read_text(errors="replace") \
        if (case / f"log.{n}.x").exists() else ""
    shm, cm = parse_snappy(read("snappyHexMesh")), parse_check_mesh(read("checkMesh"))
    if r.returncode != 0:
        shm["finished"] = False
    try:
        ratio = sum(tri_area(stl_triangles(case / f"snapped_{k}.stl"))
                    for k in ("wing", "fuselage")) / (geom["stl_area"] / 2)
    except (OSError, ValueError):
        ratio = None
    g = gate1.evaluate(shm, cm, ratio, g1)
    return {"run": run_id, "variant": variant, "settings": VARIANTS[variant],
            "cells": cm.get("cells"), "max_skewness": cm.get("max_skewness"),
            "max_non_orth": cm.get("max_non_orth"), "area_ratio": ratio,
            "gate1_pass": g.passed, "failure_code": g.failure_code,
            "wall_time_s": r.wall_time_s}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", type=Path, default=Path("runs"))
    ap.add_argument("--out", type=Path, default=Path("runs/_experiments/p63"))
    ap.add_argument("--study", default="harsh")
    ap.add_argument("--status", default="UNRECOVERABLE:SKEWNESS_HIGH",
                    help="STATUS[:FAILURE_CODE] of the runs to re-mesh")
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--save", type=Path, help="also write the results JSON here")
    a = ap.parse_args()
    con = db.connect(a.runs_dir / "runs.db")
    st, _, code = a.status.partition(":")
    ids = [r["id"] for st_ in [st] for study in a.study.split(",")
           for r in db.list_runs(con, study)
           if r["status"] == st_ and (not code or r["failure_code"] == code)]
    variants = a.variants.split(",")
    g1 = load_gates("configs/gates.yaml").model_dump(mode="json")["gate1"]
    jobs = [(rid, v) for v in variants for rid in ids]
    a.out.mkdir(parents=True, exist_ok=True)
    res_path = a.out / "results.json"
    done = json.loads(res_path.read_text()) if res_path.exists() else []
    have = {(d["run"], d["variant"]) for d in done}
    todo = [j for j in jobs if j not in have]
    print(f"{len(ids)} runs x {len(variants)} variants; {len(todo)} to mesh", flush=True)
    with ThreadPoolExecutor(a.workers) as pool:
        for d in pool.map(lambda j: mesh_one(a.runs_dir, a.out, *j, g1), todo):
            done.append(d)
            res_path.write_text(json.dumps(done, indent=1))
            print(f"{d['run']} {d['variant']:<15} cells={d['cells']} skew={d['max_skewness']} "
                  f"ratio={d['area_ratio'] and round(d['area_ratio'], 4)} "
                  f"{'PASS' if d['gate1_pass'] else d['failure_code']}", flush=True)
    if a.save:
        a.save.parent.mkdir(parents=True, exist_ok=True)
        a.save.write_text(json.dumps(done, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
