"""Production stage: continue the pilot case to convergence at alpha_1, then
warm-start alpha_2 from the converged alpha_1 field (same mesh, freestream
vector switched). Stops on residualControl (residual_target) or max_iterations.

Per alpha (results table): CL/CD/Cm = mean over the last `coeff_std_window`
iterations, their std, farfield mass imbalance |sum(phi)| / (sumMag(phi)/2)
at the last iteration, converged flag, iteration range, wall time.
Iteration budget: alpha_1 ends at max(max_iterations, pilot end) (the pilot
counts toward it); alpha_2 gets max_iterations of its own.
Minimum leg (human decision 2026-11-23): each alpha runs >= 2 x window
iterations from its own initial condition (alpha_1: the potentialFoam field at
iteration 0; alpha_2: the alpha_1 solution) before the residual stop may end it,
so the statistics window never contains the start-up transient. alpha_1
statistics use pilot + production rows (one continuous solution).
A finished alpha is never recomputed (idempotent); a failed leg restarts from
the latest written time (alpha_1 end or pilot end).
"""

from __future__ import annotations

import json
import math
import shutil
import statistics

from aero import db
from aero.corrections import rules
from aero.foam.case import write_solve_system
from aero.foam.container import run_script, unique_path
from aero.foam.parse import parse_solver_log, read_function_object, simple_converged
from aero.foam.pilot_stage import history
from aero.runner import Change, GateResult, Metric, RunContext, StageResult

STD_WINDOW_DEFAULT = 200


def latest_time(case) -> int:
    times = [int(p.name) for p in case.iterdir() if p.is_dir() and p.name.isdigit()]
    return max(times)


def mass_imbalance(case, iteration: int) -> float | None:
    net = {int(r["Time"]): r.get("sum(phi)") for r in
           read_function_object(case, "farfieldFlux", "surfaceFieldValue")}
    mag = {int(r["Time"]): r.get("sumMag(phi)") for r in
           read_function_object(case, "farfieldFluxMag", "surfaceFieldValue")}
    n, m = net.get(iteration), mag.get(iteration)
    if not isinstance(n, float) or not isinstance(m, float) or m <= 0:
        return None
    return abs(n) / (m / 2)


def window_stats(rows: list[dict], window: int) -> dict:
    tail = rows[-window:]
    out = {}
    for k, key in (("Cl", "CL"), ("Cd", "CD"), ("Cm", "Cm")):
        vals = [r[k] for r in tail if isinstance(r.get(k), float)]
        ok = len(vals) == len(tail) and len(vals) >= 2 and all(math.isfinite(v) for v in vals)
        out[key] = statistics.fmean(vals) if ok else None
        out[f"{k.lower()}_std"] = statistics.pstdev(vals) if ok else None
    return out


class ProdStage:
    def run(self, ctx: RunContext) -> StageResult:
        row = ctx.con.execute("SELECT data FROM geometry WHERE run=?", (ctx.run_id,)).fetchone()
        geom = json.loads(row["data"])
        s = ctx.settings["solver"]
        window = ctx.gates["gate3"].get("coeff_std_window") or STD_WINDOW_DEFAULT
        case = ctx.dir / "case"
        alphas = [float(a) for a in ctx.settings["alphas"]]
        done = db.get_results(ctx.con, ctx.run_id)
        legs = []
        for i, alpha in enumerate(alphas):
            if alpha in done:
                legs.append({"alpha": alpha, "skipped": True, "ok": True})
                continue
            start = latest_time(case)
            if i == 0:
                origin = 0                                  # potentialFoam initial field
                end = max(s["max_iterations"], start)
            else:                                           # alpha_1 solution (from the DB,
                origin = int(db.get_results(ctx.con, ctx.run_id)[alphas[0]]["end_iter"])
                end = origin + s["max_iterations"]          # robust to a partial retry)
            min_end = min(origin + 2 * window, end)
            fresh_switch = i > 0 and start == origin
            if fresh_switch:                                # keep alpha_1's final fields
                arch = case / "attempts" / f"alpha1_final_{origin}"
                if not arch.exists():
                    shutil.copytree(case / str(origin), arch)
            steps = []                                      # (end_time, residual_control)
            if min_end > start:
                steps.append((min_end, False))
            if end > max(min_end, start):
                steps.append((end, True))
            leg = {"alpha": alpha, "start": start, "end_requested": end, "rc": 0,
                   "wall_time_s": 0.0, "tags": []}
            log_text, log = "", None
            for k, (step_end, resid) in enumerate(steps):
                tag = unique_path(case / f"log.simpleFoam.prod.a{i + 1}.t{ctx.attempt}.s{k}") \
                    .name.removeprefix("log.simpleFoam.")
                script = write_solve_system(case, geom, ctx.settings, alpha, step_end,
                                            residual_control=resid,
                                            change_alpha=(fresh_switch and k == 0), tag=tag)
                r = run_script(case, script, tag, engine=ctx.settings["container_engine"],
                               image=ctx.settings["image"], timeout=12 * 3600)
                lp = case / f"log.simpleFoam.{tag}"
                log_text = lp.read_text(errors="replace") if lp.exists() else ""
                log = parse_solver_log(log_text)
                leg["tags"].append(tag)
                leg["rc"], leg["log"] = r.returncode, log
                leg["wall_time_s"] += r.wall_time_s
                if r.returncode != 0 or log["fatal"] or log["fpe"] or not log["ended"] \
                        or log["nan_residual"]:
                    break
            if log is None:                                 # nothing to run (budget spent)
                log = {"fatal": False, "fpe": False, "ended": True, "nan_residual": False,
                       "setup_error": False}
                leg["log"] = log
            if leg["rc"] != 0 or log["fatal"] or log["fpe"] or not log["ended"] \
                    or log["nan_residual"]:
                leg["ok"] = False
                legs.append(leg)
                break                                       # alpha_2 needs alpha_1
            last = latest_time(case)
            hist = history(case)
            rows = [h for h in hist if start < h["iteration"] <= last]
            db.put_history(ctx.con, ctx.run_id, "prod", f"alpha{alpha:g}", alpha, rows)
            stat_rows = [h for h in hist if origin < h["iteration"] <= last]
            st = window_stats(stat_rows, window)
            res = {**st, "iterations": last - origin, "wall_time": leg["wall_time_s"],
                   "mass_imbalance": mass_imbalance(case, last),
                   "converged": int(simple_converged(log_text)),
                   "start_iter": start, "end_iter": last}
            leg.update(ok=st["CL"] is not None, result=res)
            if leg["ok"]:
                db.put_result(ctx.con, ctx.run_id, alpha, res)
            legs.append(leg)
            if not leg["ok"]:
                break
        return StageResult(True, {"legs": legs, "n_alphas": len(alphas)})

    def check(self, ctx: RunContext, result: StageResult) -> GateResult:
        legs, metrics = result.data["legs"], []
        code = None
        for leg in legs:
            a = leg["alpha"]
            if leg.get("skipped"):
                metrics.append(Metric(f"alpha_{a:g}_done", 1.0, 1, True))
                continue
            log = leg["log"]
            if not leg["ok"]:
                code = ("CASE_SETUP_ERROR" if log["setup_error"] else
                        "NAN_DETECTED" if log["nan_residual"] else
                        "SOLVER_CRASHED" if (leg["rc"] != 0 or log["fatal"] or log["fpe"]
                                             or not log["ended"]) else "MISSING_OUTPUT")
                metrics.append(Metric(f"alpha_{a:g}_done", 0.0, 1, False))
                break
            res = leg["result"]
            metrics.append(Metric(f"alpha_{a:g}_done", 1.0, 1, True))
            for k in ("iterations", "converged", "mass_imbalance", "wall_time"):
                metrics.append(Metric(f"alpha_{a:g}_{k}", res[k], None, True, "warn"))
        n_ok = sum(1 for l in legs if l["ok"])
        if code is None and n_ok < result.data["n_alphas"]:
            code = "MISSING_OUTPUT"
        return GateResult(code is None, code, metrics)

    def correct(self, ctx: RunContext, gate: GateResult, attempt: int) -> Change | None:
        n = sum(1 for c in db.corrections_for(ctx.con, ctx.run_id, "prod")
                if c["code"] == gate.failure_code)
        return rules.propose(ctx.corrections, "prod", gate.failure_code, ctx.settings, n + 1)
