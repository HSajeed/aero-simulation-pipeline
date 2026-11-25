"""Checks stage: Gate 3 on the DB results (PROD_DONE -> CHECKS_PASS | FLAGGED).
No retries, no corrections: a flagged run waits for a human.

Before Gate 3 it writes the report-only cutting-plane samples (`aero sample`,
decision V18; automatic since Phase 8), so flagged runs reach the reviewer with
contours. Sampling never affects the gate (rule 4): its outcome, including any
exception, is only logged to case/log.sample_status.
"""

from __future__ import annotations

import traceback

from aero import db
from aero.gates import gate3
from aero.runner import Change, GateResult, RunContext, StageResult


class ChecksStage:
    def __init__(self, sample: bool = True):
        self.sample = sample

    def _sample(self, ctx: RunContext) -> None:
        from aero.foam.sample import sample_run
        try:
            out = sample_run(ctx.con, ctx.dir, ctx.run_id, engine=ctx.settings["container_engine"],
                             image=ctx.settings["image"])
            msg = out["status"]
        except Exception:            # report-only: never let it touch the gate
            msg = "exception\n" + traceback.format_exc()
        (ctx.dir / "case").mkdir(parents=True, exist_ok=True)
        with (ctx.dir / "case" / "log.sample_status").open("a") as fh:
            fh.write(f"{db.now()} {msg}\n")

    def run(self, ctx: RunContext) -> StageResult:
        if self.sample:
            self._sample(ctx)
        results = {a: dict(r) for a, r in db.get_results(ctx.con, ctx.run_id).items()}
        refs = {r["alpha"]: dict(r) for r in ctx.con.execute(
            "SELECT * FROM reference_results WHERE run=? AND source='vspaero'", (ctx.run_id,))}
        return StageResult(True, {"results": results, "refs": refs})

    def check(self, ctx: RunContext, result: StageResult) -> GateResult:
        return gate3.evaluate(result.data["results"], result.data["refs"], ctx.gates["gate3"])

    def correct(self, ctx: RunContext, gate: GateResult, attempt: int) -> Change | None:
        return None
