"""One self-contained HTML report per run (images embedded as base64 PNG).

Every number comes from the run DB (CLAUDE.md rule 5). The report is derived
data: regenerating it from an unchanged DB gives the same content, apart from
the generation timestamp.
"""

from __future__ import annotations

import base64
import json
import math
import re
from collections import defaultdict
from pathlib import Path

import jinja2
import yaml

from aero import db
from aero.report import plots
from aero.review import queue
from aero.runner import run_dir

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "templates" / "report"
GATE_ORDER = ["gate0", "gate1", "gate2", "prod", "gate3"]
GATE_TITLES = {"gate0": "Gate 0: geometry", "gate1": "Gate 1: mesh", "gate2": "Gate 2: pilot",
               "prod": "Production", "gate3": "Gate 3: plausibility"}


LABELS = {  # metric -> readable name (alpha suffix added by label())
    "cl_std": "CL steadiness (std over last window)", "mass_imbalance": "mass imbalance",
    "cl_vs_vspaero": "CL vs VSPAERO (relative difference)", "cd": "CD", "cm": "Cm",
    "converged": "converged", "done": "finished", "iterations": "iterations",
    "wall_time": "wall time [s]", "cl_alpha_slope": "CL-α slope [/rad]",
    "cm_alpha_slope": "Cm-α slope [/rad]", "cm_alpha_slope_vspaero": "Cm-α slope, VSPAERO [/rad]",
    "surfacecheck_finished": "surfaceCheck finished", "closed": "surface closed",
    "illegal_triangles": "illegal triangles", "self_intersections": "self-intersections",
    "min_triangle_quality": "min triangle quality", "tip_chord_cells": "cells across tip chord",
    "n_parts": "parts", "n_zones": "zones", "n_triangles": "triangles", "vspaero_ok": "VSPAERO ran",
    "shm_finished": "snappyHexMesh finished", "checkmesh_parsed": "checkMesh read",
    "checkmesh_ok": "checkMesh OK", "negative_volumes": "negative volumes",
    "closed_boundary": "boundary closed", "cells_max": "cells (max limit)",
    "cells_min": "cells (min limit)", "max_non_orth": "max non-orthogonality [°]",
    "max_skewness": "max skewness", "wetted_area_ratio": "wetted area ratio (mesh / STL)",
    "max_aspect_ratio": "max aspect ratio", "shm_time_s": "snappyHexMesh time [s]",
    "mesh_wall_time_s": "mesh wall time [s]", "snapped_area_wing": "snapped wing area [m²]",
    "snapped_area_fuselage": "snapped fuselage area [m²]", "case_setup_ok": "case set up",
    "potential_init_ok": "potentialFoam init", "solver_completed": "solver finished",
    "finite_values": "no NaN/inf", "max_abs_coeff": "max |coefficient|",
    "residual_drop_orders": "residual drop [orders]", "cl_change": "CL change over pilot window",
    "pilot_Cl": "pilot CL", "pilot_Cd": "pilot CD", "pilot_Cm": "pilot Cm",
    "s_per_100_iter": "s per 100 iterations", "pilot_wall_time_s": "pilot wall time [s]",
}


def label(metric: str) -> str:
    m = re.fullmatch(r"alpha_(-?[\d.]+)_(.+)", metric) or re.fullmatch(r"(.+)_a(-?[\d.]+)", metric)
    if m:
        a, base = (m[1], m[2]) if metric.startswith("alpha_") else (m[2], m[1])
        return f"{LABELS.get(base, base.replace('_', ' '))}, α = {a}°"
    if metric.startswith("residual_drop_"):
        return f"residual drop {metric[14:]} [orders]" if metric != "residual_drop_orders" \
            else LABELS[metric]
    return LABELS.get(metric, metric.replace("_", " "))


def limit(m: dict, g3: dict) -> str:
    """Threshold as shown: the slope check is a range."""
    if m["metric"] == "cl_alpha_slope" and g3.get("cl_alpha_slope_range"):
        lo, hi = g3["cl_alpha_slope_range"]
        return f"{fmt(lo)} – {fmt(hi)}"
    return fmt(m["threshold"])


def fmt(v, digits: int = 4) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return str(v)
        if v == int(v) and abs(v) < 1e7:
            return str(int(v))
        return f"{v:.{digits}g}"
    return str(v)


def collect(con, run_id: str) -> dict:
    """Everything the report shows, read from the DB."""
    run = dict(db.get_run(con, run_id))
    run["params"] = json.loads(run["params"])
    study_yaml = con.execute("SELECT config_yaml FROM studies WHERE name=?",
                             (run["study"],)).fetchone()[0]
    geo = con.execute("SELECT * FROM geometry WHERE run=?", (run_id,)).fetchone()

    batches: dict[str, list[dict]] = defaultdict(list)   # gate -> list of batches (old -> new)
    for r in con.execute("SELECT * FROM gate_results WHERE run=? ORDER BY id", (run_id,)):
        r = dict(r)
        gb = batches[r["gate"]]
        key = (r["attempt"], r["created_at"])
        if not gb or gb[-1]["key"] != key:
            gb.append({"key": key, "attempt": r["attempt"], "time": r["created_at"], "metrics": []})
        gb[-1]["metrics"].append(r)
    gates = [{"gate": g, "title": GATE_TITLES.get(g, g), "latest": batches[g][-1],
              "earlier": batches[g][:-1]} for g in GATE_ORDER if g in batches]
    gates += [{"gate": g, "title": g, "latest": b[-1], "earlier": b[:-1]}
              for g, b in batches.items() if g not in GATE_ORDER]

    results = {a: dict(r) for a, r in db.get_results(con, run_id).items()}
    refs = {r["alpha"]: dict(r) for r in con.execute(
        "SELECT * FROM reference_results WHERE run=? AND source='vspaero'", (run_id,))}
    history = [dict(r) for r in con.execute(
        "SELECT * FROM history WHERE run=? ORDER BY iteration", (run_id,))]
    return {
        "run": run,
        "study_cfg": yaml.safe_load(study_yaml),
        "geometry": dict(geo) if geo else None,
        "gates": gates,
        "corrections": [dict(c) for c in db.corrections_for(con, run_id)],
        "results": results,
        "refs": refs,
        "history": history,
        "reviews": queue.reviews_for(con, run_id),
    }


def _plane_figs(d: dict, run_dir: Path) -> list[tuple[float, str]]:
    """Cutting-plane contour figures per alpha, from the sampled VTK files (solver
    output; written by `aero sample`). Empty when not sampled."""
    from aero.foam.parse import parse_vtk_polydata
    from aero.foam.sample import plane_files, plane_positions
    geo = d["geometry"]
    if not geo:
        return []
    gdata = json.loads(geo["data"])
    U_inf = d["study_cfg"]["freestream"]["U_inf"]
    ys = dict(plane_positions(gdata["span"]))
    out = []
    for a, r in sorted(d["results"].items()):
        if r.get("end_iter") is None:
            continue
        files = plane_files(run_dir / "case", int(r["end_iter"]))
        data = [(n, ys[n], parse_vtk_polydata(f.read_text())) for n, f in sorted(files.items())]
        png = plots.planes(a, U_inf, data, d["run"]["params"], gdata)
        if png:
            out.append((a, base64.b64encode(png).decode()))
    return out


def _b64(png: bytes | None) -> str | None:
    return base64.b64encode(png).decode() if png else None


def render(con, run_id: str, gates_cfg: dict, db_file: str = "runs/runs.db",
           run_path: Path | None = None) -> str:
    d = collect(con, run_id)
    g3 = gates_cfg.get("gate3", {})
    latest = [m for g in d["gates"] for m in g["latest"]["metrics"]]
    figs = {
        "residuals": _b64(plots.residuals(d["history"])),
        "coefficients": _b64(plots.coefficients(d["history"], d["results"],
                                                g3.get("coeff_std_window"))),
        "cfd_vs_ref": _b64(plots.cfd_vs_reference(d["results"], d["refs"])),
        "planes": _plane_figs(d, run_path) if run_path else [],
    }
    failed = [m for m in latest if m["severity"] == "gate" and not m["pass"]]
    for m in failed:
        m["exceedance"] = (queue.exceedance(m["metric"], m["value"], m["threshold"], g3)
                           if m["gate"] == "gate3" else None)
    rows = []
    for a in sorted(set(d["results"]) | set(d["refs"])):
        r, ref = d["results"].get(a, {}), d["refs"].get(a, {})
        diff = None
        if r.get("CL") is not None and ref.get("CL") is not None and g3.get("cl_vspaero_floor"):
            diff = abs(r["CL"] - ref["CL"]) / max(abs(ref["CL"]), g3["cl_vspaero_floor"])
        rows.append({"alpha": a, "r": r, "ref": ref, "diff": diff})
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES),
                             autoescape=jinja2.select_autoescape(["html", "j2"]),
                             undefined=jinja2.StrictUndefined)
    env.filters["fmt"] = fmt
    env.filters["label"] = label
    env.filters["limit"] = lambda m: limit(m, g3)
    return env.get_template("run.html.j2").render(
        d=d, figs=figs, failed=failed, result_rows=rows, codes=[
            c for c in (d["run"]["failure_code"] or "").split(",") if c],
        generated=db.now(), db_file=db_file, gates_cfg=gates_cfg)


def write_report(con, runs_dir: Path, run_id: str, gates_cfg: dict) -> Path:
    row = db.get_run(con, run_id)
    out = run_dir(runs_dir, row["study"], run_id) / "report" / "report.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    html = render(con, run_id, gates_cfg, db_file=str(Path(runs_dir) / "runs.db"),
                  run_path=out.parent.parent)
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(html)
    tmp.replace(out)
    return out
