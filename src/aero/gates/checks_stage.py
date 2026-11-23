"""Checks stage: Gate 3 on the DB results (PROD_DONE -> CHECKS_PASS | FLAGGED).
No retries, no corrections: a flagged run waits for a human."""

from __future__ import annotations

from aero import db
from aero.gates import gate3
from aero.runner import Change, GateResult, RunContext, StageResult


class ChecksStage:
    def run(self, ctx: RunContext) -> StageResult:
        results = {a: dict(r) for a, r in db.get_results(ctx.con, ctx.run_id).items()}
        refs = {r["alpha"]: dict(r) for r in ctx.con.execute(
            "SELECT * FROM reference_results WHERE run=? AND source='vspaero'", (ctx.run_id,))}
        return StageResult(True, {"results": results, "refs": refs})

    def check(self, ctx: RunContext, result: StageResult) -> GateResult:
        return gate3.evaluate(result.data["results"], result.data["refs"], ctx.gates["gate3"])

    def correct(self, ctx: RunContext, gate: GateResult, attempt: int) -> Change | None:
        return None
