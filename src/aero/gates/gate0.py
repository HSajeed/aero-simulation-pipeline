"""Gate 0: STL validity (surfaceCheck) + minimum feature size.

Checks in priority order; the first blocking failure gives the failure code.
Thresholds come from configs/gates.yaml; null -> UNDECIDED_THRESHOLD (blocks),
"warn" -> recorded with severity 'warn', never blocks. Missing parse values
fail the check.
"""

from __future__ import annotations

from aero.runner import GateResult, Metric


def finest_surface_cell(mesh: dict) -> float:
    return mesh["base_cell_size"] / 2 ** max(mesh["surface_level"])


def evaluate(sc: dict, ref: dict, settings: dict, g0: dict) -> GateResult:
    metrics: list[Metric] = []
    failures: list[str] = []

    def gate(name, value, threshold, ok, code):
        metrics.append(Metric(name, value, threshold, bool(ok)))
        if not ok:
            failures.append(code)

    def limit(name, value, threshold, code, upper: bool):
        """Threshold may be a number, 'warn' or None (undecided)."""
        if threshold == "warn":
            ok = value is not None
            metrics.append(Metric(name, value, None, ok, "warn"))
            return
        if threshold is None:
            gate(name, value, None, False, "UNDECIDED_THRESHOLD")
            return
        ok = value is not None and (value <= threshold if upper else value >= threshold)
        gate(name, value, threshold, ok, code)

    gate("surfacecheck_finished", float(sc.get("finished", False)), 1,
         sc.get("finished", False), "SURFACECHECK_FAILED")
    if g0["require_closed"]:
        closed = sc.get("closed")
        gate("closed", None if closed is None else float(closed), 1, closed is True, "NOT_CLOSED")
    limit("illegal_triangles", sc.get("illegal_triangles"), g0["max_illegal_triangles"],
          "ILLEGAL_TRIANGLES", upper=True)
    limit("self_intersections", sc.get("self_intersections"), g0["max_self_intersections"],
          "SELF_INTERSECTING", upper=True)
    limit("min_triangle_quality", sc.get("min_quality"), g0["min_triangle_quality"],
          "LOW_TRIANGLE_QUALITY", upper=False)
    cells = ref["tip_chord"] / finest_surface_cell(settings["mesh"])
    limit("tip_chord_cells", cells, g0["min_tip_chord_cells"], "FEATURE_TOO_SMALL", upper=False)
    # informational
    for k in ("n_parts", "n_zones", "n_triangles"):
        metrics.append(Metric(k, sc.get(k), None, sc.get(k) is not None, "warn"))

    return GateResult(not failures, failures[0] if failures else None, metrics)
