"""Geometry stage: OpenVSP build -> STL -> surfaceCheck (container) -> Gate 0.
VSPAERO reference results are computed in the same stage; a VSPAERO failure
is recorded (warn) and does not block geometry, but Gate 3 will lack its
reference and must flag the run."""

from __future__ import annotations

from aero import db
from aero.corrections import rules
from aero.foam.container import run_script, unique_path
from aero.foam.parse import parse_surface_check
from aero.gates import gate0
from aero.geom import openvsp_build, vspaero
from aero.runner import Change, GateResult, Metric, RunContext, StageResult


class GeomStage:
    def run(self, ctx: RunContext) -> StageResult:
        gdir = ctx.dir / "geom"
        ref = openvsp_build.build(ctx.params, ctx.settings, gdir)
        db.put_geometry(ctx.con, ctx.run_id, ref)

        vsp_ok = True
        try:
            rows = vspaero.run(gdir / "model.vsp3", list(ctx.settings["alphas"]), ref,
                               ctx.settings["freestream"],
                               unique_path(gdir / f"log.vspaero.a{ctx.attempt}"))
            db.put_reference(ctx.con, ctx.run_id, "vspaero", rows)
        except Exception as e:   # recorded; Gate 3 flags a missing reference later
            vsp_ok = False
            db.clear_reference(ctx.con, ctx.run_id, "vspaero")
            unique_path(gdir / f"vspaero_error.a{ctx.attempt}.txt").write_text(repr(e) + "\n")

        name = f"surfaceCheck.a{ctx.attempt}"   # keep every attempt's log
        r = run_script(gdir, "surfaceCheck -checkSelfIntersection body.stl", name,
                       engine=ctx.settings["container_engine"], image=ctx.settings["image"],
                       timeout=600)
        sc = parse_surface_check(r.log_path.read_text())
        if r.returncode != 0:
            sc["finished"] = False
        return StageResult(True, {"surface_check": sc, "ref": ref, "vspaero_ok": vsp_ok})

    def check(self, ctx: RunContext, result: StageResult) -> GateResult:
        g = gate0.evaluate(result.data["surface_check"], result.data["ref"],
                           ctx.settings, ctx.gates["gate0"])
        ok = result.data["vspaero_ok"]
        g.metrics.append(Metric("vspaero_ok", float(ok), None, ok, "warn"))
        return g

    def correct(self, ctx: RunContext, gate: GateResult, attempt: int) -> Change | None:
        return rules.propose(ctx.corrections, "gate0", gate.failure_code, ctx.settings)
