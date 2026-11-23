"""Tier 3 review queue: rank FLAGGED runs, record human decisions.

Severity (human decision, Phase 6) is shown three ways and sorted in this order:
1. number of violated Gate 3 checks (failed gate-severity metrics, latest batch),
2. the most serious violated check type (CHECK_PRIORITY, lower = worse),
3. the worst normalised exceedance (how many times over the limit).
All inputs come from the DB (runs.failure_code, gate_results).

Only this module moves a run out of FLAGGED, always with human=True, and only
to ACCEPTED or REJECTED. The pipeline never auto-accepts.
"""

from __future__ import annotations

import getpass
import math
from dataclasses import dataclass

from aero import db
from aero.status import Status

# Lower index = more serious. Missing data first: nothing about the run can be trusted.
CHECK_PRIORITY = ["MISSING_RESULTS", "NO_REFERENCE", "UNDECIDED_THRESHOLD", "MASS_IMBALANCE",
                  "COEFF_NOT_CONVERGED", "CL_ALPHA_SLOPE", "CL_VS_VSPAERO", "NEGATIVE_CD"]


class ReviewError(ValueError):
    pass


def latest_batch(con, run_id: str, gate: str) -> list[dict]:
    """Metrics of the most recent evaluation of `gate` for a run."""
    row = con.execute("SELECT MAX(id) FROM gate_results WHERE run=? AND gate=?",
                      (run_id, gate)).fetchone()
    if row[0] is None:
        return []
    last = con.execute("SELECT attempt, created_at FROM gate_results WHERE id=?",
                       (row[0],)).fetchone()
    return [dict(r) for r in con.execute(
        "SELECT * FROM gate_results WHERE run=? AND gate=? AND attempt=? AND created_at=?"
        " ORDER BY id", (run_id, gate, last["attempt"], last["created_at"]))]


def exceedance(metric: str, value: float | None, threshold: float | None,
               g3: dict) -> float | None:
    """How many times over the limit a failed Gate 3 metric is (>= 1 means violated).
    inf: no value at all (missing data / undecided threshold).
    None: the check has no natural scale (CD vs a 0 limit)."""
    if metric.startswith("cd_"):
        return None
    if value is None or not math.isfinite(value):
        return math.inf
    if metric == "cl_alpha_slope":
        lo, hi = g3["cl_alpha_slope_range"]
        if value > hi:
            return value / hi
        return lo / value if value > 0 else math.inf
    if threshold:
        return abs(value) / threshold
    return math.inf


@dataclass
class QueueEntry:
    run_id: str
    study: str
    codes: list[str]
    n_violations: int
    top_check: str
    exceedance: float | None
    params: str

    def sort_key(self):
        pri = (CHECK_PRIORITY.index(self.top_check) if self.top_check in CHECK_PRIORITY
               else len(CHECK_PRIORITY))
        return (-self.n_violations, pri, -(self.exceedance if self.exceedance is not None else 0.0),
                self.run_id)


def flagged(con, study: str, g3: dict) -> list[QueueEntry]:
    """FLAGGED runs of a study, most severe first."""
    out = []
    for r in db.list_runs(con, study):
        if r["status"] != Status.FLAGGED:
            continue
        codes = [c for c in (r["failure_code"] or "").split(",") if c]
        failed = [m for m in latest_batch(con, r["id"], "gate3")
                  if m["severity"] == "gate" and not m["pass"]]
        ex = [e for e in (exceedance(m["metric"], m["value"], m["threshold"], g3) for m in failed)
              if e is not None]
        top = min(codes, key=lambda c: CHECK_PRIORITY.index(c) if c in CHECK_PRIORITY
                  else len(CHECK_PRIORITY)) if codes else "?"
        out.append(QueueEntry(r["id"], study, codes, len(failed) or len(codes), top,
                              max(ex) if ex else None, r["params"]))
    return sorted(out, key=QueueEntry.sort_key)


def decide(con, run_id: str, accept: bool, label: str, note: str | None,
           labels: list[str], reviewer: str | None = None):
    """Record a human decision: FLAGGED -> ACCEPTED | REJECTED plus a reviews row,
    in one transaction. Returns the updated run row."""
    if label not in labels:
        raise ReviewError(f"label '{label}' not in vocabulary: {', '.join(labels)}")
    if label == "other" and not (note and note.strip()):
        raise ReviewError("label 'other' requires --note")
    row = db.get_run(con, run_id)
    if row["status"] != Status.FLAGGED:
        raise ReviewError(f"{run_id} is {row['status']}, not FLAGGED; only flagged runs are reviewed")
    decision = "accept" if accept else "reject"
    new = Status.ACCEPTED if accept else Status.REJECTED
    reviewer = reviewer or getpass.getuser()
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("INSERT INTO reviews (run, decision, label, note, reviewer, time)"
                    " VALUES (?,?,?,?,?,?)", (run_id, decision, label, note, reviewer, db.now()))
        # failure_code kept: the flag reasons stay attached to the reviewed run
        row = db.set_status(con, run_id, new, failure_code=row["failure_code"], human=True)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return row


def reviews_for(con, run_id: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM reviews WHERE run=? ORDER BY id", (run_id,))]
