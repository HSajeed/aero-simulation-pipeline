"""Pilot stage: potentialFoam init + the first N simpleFoam iterations of the
real case (first alpha) in case/, then Gate 2. Every attempt restarts from
iteration 0; the previous attempt's postProcessing is moved (never deleted)
to case/attempts/. On a pass, production (Phase 5) continues this case."""

from __future__ import annotations

import json
import shutil

from aero import db
from aero.corrections import rules
from aero.foam.case import write_solve_case
from aero.foam.container import run_script, unique_path
from aero.foam.parse import parse_solver_log, read_function_object
from aero.gates import gate2
from aero.runner import Change, GateResult, Metric, RunContext, StageResult

TEMPLATES = None  # override (tests only) to render a deliberately broken case


def history(case) -> list[dict]:
    """Join solverInfo (initial residuals) and forceCoeffs rows by iteration."""
    res = {int(r["Time"]): r for r in read_function_object(case, "solverInfo1", "solverInfo")}
    coef = {int(r["Time"]): r for r in read_function_object(case, "forceCoeffs1", "coefficient")}
    out = []
    for it in sorted(set(res) | set(coef)):
        r, c = res.get(it, {}), coef.get(it, {})
        out.append({"iteration": it,
                    "Ux": r.get("Ux_initial"), "Uy": r.get("Uy_initial"),
                    "Uz": r.get("Uz_initial"), "p": r.get("p_initial"),
                    "Cl": c.get("Cl"), "Cd": c.get("Cd"), "Cm": c.get("CmPitch")})
    return out


class PilotStage:
    def run(self, ctx: RunContext) -> StageResult:
        row = ctx.con.execute("SELECT data FROM geometry WHERE run=?", (ctx.run_id,)).fetchone()
        if row is None:
            raise RuntimeError("no geometry record")
        geom = json.loads(row["data"])
        s = ctx.settings["solver"]
        alpha = float(ctx.settings["alphas"][0])
        case = ctx.dir / "case"
        tag_dir = unique_path(case / "attempts" / f"a{ctx.attempt}")
        tag_dir.mkdir(parents=True)
        tag = tag_dir.name
        pp = case / "postProcessing"
        if pp.exists():                                   # keep, never delete
            shutil.move(str(pp), str(tag_dir / "previous_postProcessing"))
        script = write_solve_case(case, ctx.dir / "mesh", geom, ctx.settings, alpha, tag,
                                  s["pilot_iterations"], residual_control=False,
                                  templates=TEMPLATES)
        r = run_script(case, script, f"pilot.{tag}", engine=ctx.settings["container_engine"],
                       image=ctx.settings["image"], timeout=4 * 3600)

        names = [case / f"log.simpleFoam.{tag}"] + sorted(case.glob(f"log.simpleFoam.{tag}.s[0-9]"))
        logs = [parse_solver_log(p.read_text(errors="replace")) for p in names if p.exists()]
        pot_log = case / f"log.potentialFoam.{tag}"
        pot = parse_solver_log(pot_log.read_text(errors="replace")) if pot_log.exists() else None
        hist = history(case)
        db.put_history(ctx.con, ctx.run_id, "pilot", tag, alpha, hist)
        exe = sum(l["execution_time_s"] or 0 for l in logs)
        its = sum(l["n_iterations"] for l in logs)
        return StageResult(True, {
            "logs": logs, "potential": pot if s["potential_init"] else None, "hist": hist,
            "expected_iterations": s["pilot_iterations"], "rc": r.returncode, "tag": tag,
            "s_per_100_iter": 100 * exe / its if its else None, "wall_time_s": r.wall_time_s})

    def check(self, ctx: RunContext, result: StageResult) -> GateResult:
        d = result.data
        g = gate2.evaluate(d, ctx.gates["gate2"])
        g.metrics.append(Metric("s_per_100_iter", d["s_per_100_iter"], None,
                                d["s_per_100_iter"] is not None, "warn"))
        g.metrics.append(Metric("pilot_wall_time_s", d["wall_time_s"], None, True, "warn"))
        return g

    def correct(self, ctx: RunContext, gate: GateResult, attempt: int) -> Change | None:
        n = sum(1 for c in db.corrections_for(ctx.con, ctx.run_id, "gate2")
                if c["code"] == gate.failure_code)
        return rules.propose(ctx.corrections, "gate2", gate.failure_code, ctx.settings, n + 1)
