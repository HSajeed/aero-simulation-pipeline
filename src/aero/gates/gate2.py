"""Gate 2: pilot run health (first alpha, first N iterations).

Checks: setup error (never auto-fixed: it means the case/physics is wrong),
potentialFoam init, solver crash, NaN, forces bounded, residual drop, CL
settled. Residual drop per field = log10(max initial residual over the first
10 iterations / median over the last 10); the gate uses the minimum over
Ux, Uy, Uz, p. CL change = |Cl[-1] - Cl[-1-window]| / max(|Cl[-1]|, floor).
Null thresholds block as UNDECIDED_THRESHOLD.
"""

from __future__ import annotations

import math
import statistics

from aero.runner import GateResult, Metric

PRIORITY = ["CASE_SETUP_ERROR", "POTENTIAL_INIT_FAILED", "SOLVER_CRASHED", "NAN_DETECTED",
            "MISSING_OUTPUT", "FORCES_UNBOUNDED", "RESIDUAL_DROP_LOW", "CL_NOT_SETTLED",
            "UNDECIDED_THRESHOLD"]
FIELDS = ("Ux", "Uy", "Uz", "p")


def residual_drop(hist: list[dict], field: str) -> float | None:
    vals = [h[field] for h in hist if h.get(field) is not None]
    if len(vals) < 20 or not all(math.isfinite(v) and v > 0 for v in vals):
        return None
    return math.log10(max(vals[:10]) / statistics.median(vals[-10:]))


def evaluate(d: dict, g2: dict) -> GateResult:
    """d: {'logs': {...parsed solver logs...}, 'potential': parsed|None,
    'hist': [rows], 'expected_iterations': int, 'rc': int}"""
    metrics: list[Metric] = []
    failures: set[str] = set()

    def chk(name, value, threshold, ok, code, severity="gate"):
        metrics.append(Metric(name, value, threshold, bool(ok), severity))
        if not ok and severity == "gate":
            failures.add(code)

    logs, pot, hist = d["logs"], d.get("potential"), d["hist"]
    setup = any(l["setup_error"] for l in logs) or bool(pot and pot["setup_error"])
    chk("case_setup_ok", float(not setup), 1, not setup, "CASE_SETUP_ERROR")
    if pot is not None:
        chk("potential_init_ok", float(pot["ended"]), 1, pot["ended"] and not pot["fatal"],
            "POTENTIAL_INIT_FAILED")
    crashed = d["rc"] != 0 or not logs or any(l["fatal"] or l["fpe"] or not l["ended"] for l in logs)
    chk("solver_completed", float(not crashed), 1, not crashed, "SOLVER_CRASHED")

    nan = any(l["nan_residual"] for l in logs) or any(
        isinstance(v, float) and not math.isfinite(v)
        for h in hist for k, v in h.items() if k != "iteration")
    chk("finite_values", float(not nan), 1, not nan, "NAN_DETECTED")

    n = len(hist)
    chk("iterations", n, d["expected_iterations"], n >= d["expected_iterations"], "MISSING_OUTPUT")
    window = g2["cl_change_window"]

    lim = g2.get("max_abs_coeff")
    tail = hist[-window:] if hist else []
    peak = max((abs(h[k]) for h in tail for k in ("Cl", "Cd", "Cm")
                if isinstance(h.get(k), float) and math.isfinite(h[k])), default=None)
    if lim is None:
        chk("max_abs_coeff", peak, None, False, "UNDECIDED_THRESHOLD")
    else:
        chk("max_abs_coeff", peak, lim, peak is not None and peak <= lim, "FORCES_UNBOUNDED")

    drops = {f: residual_drop(hist, f) for f in FIELDS}
    for f, v in drops.items():
        metrics.append(Metric(f"residual_drop_{f}", v, None, v is not None, "warn"))
    worst = None if any(v is None for v in drops.values()) else min(drops.values())
    chk("residual_drop_orders", worst, g2["min_residual_drop_orders"],
        worst is not None and worst >= g2["min_residual_drop_orders"], "RESIDUAL_DROP_LOW")

    floor = g2.get("cl_change_floor")
    change = None
    if n > window and floor is not None:
        cl1, cl0 = hist[-1].get("Cl"), hist[-1 - window].get("Cl")
        if isinstance(cl1, float) and isinstance(cl0, float):
            change = abs(cl1 - cl0) / max(abs(cl1), floor)
    if floor is None:
        chk("cl_change", None, g2["max_cl_change"], False, "UNDECIDED_THRESHOLD")
    else:
        chk("cl_change", change, g2["max_cl_change"],
            change is not None and math.isfinite(change) and change < g2["max_cl_change"],
            "CL_NOT_SETTLED")
    if hist:
        last = hist[-1]
        for k in ("Cl", "Cd", "Cm"):
            metrics.append(Metric(f"pilot_{k}", last.get(k), None, True, "warn"))

    code = next((c for c in PRIORITY if c in failures), None)
    return GateResult(code is None, code, metrics)
