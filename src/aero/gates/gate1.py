"""Gate 1: mesh quality, cell budget, snapped wetted area.

Failure-code priority (first blocking failure wins): crashes and broken
meshes first, then budget overrun (so a refinement fix is never chosen while
over budget), then quality, then surface fidelity, then too few cells.
Any missing parsed value fails its check.
"""

from __future__ import annotations

from aero.runner import GateResult, Metric

PRIORITY = ["SHM_FAILED", "CHECKMESH_FAILED", "NEGATIVE_VOLUMES", "OPEN_BOUNDARY",
            "TOO_MANY_CELLS", "NON_ORTH_HIGH", "SKEWNESS_HIGH", "AREA_RATIO_LOW",
            "TOO_FEW_CELLS"]


def evaluate(shm: dict, cm: dict, area_ratio: float | None, g1: dict) -> GateResult:
    metrics: list[Metric] = []
    failures: set[str] = set()

    def chk(name, value, threshold, ok, code):
        ok = bool(ok) and value is not None
        metrics.append(Metric(name, value, threshold, ok))
        if not ok:
            failures.add(code)

    chk("shm_finished", float(shm.get("finished", False)), 1,
        shm.get("finished") and not shm.get("fatal"), "SHM_FAILED")
    chk("checkmesh_parsed", float(cm.get("cells") is not None), 1,
        cm.get("cells") is not None, "CHECKMESH_FAILED")
    neg = cm.get("negative_volumes")
    chk("negative_volumes", neg, g1["max_negative_volumes"],
        neg is not None and neg <= g1["max_negative_volumes"], "NEGATIVE_VOLUMES")
    if g1["require_closed_boundary"]:
        ob = cm.get("open_boundary")
        chk("closed_boundary", None if ob is None else float(not ob), 1,
            ob is False and not cm.get("open_cells"), "OPEN_BOUNDARY")
    cells = cm.get("cells")
    chk("cells_max", cells, g1["max_cells"], cells is not None and cells <= g1["max_cells"],
        "TOO_MANY_CELLS")
    nonorth = cm.get("max_non_orth")
    chk("max_non_orth", nonorth, g1["max_non_orth"],
        nonorth is not None and nonorth < g1["max_non_orth"], "NON_ORTH_HIGH")
    skew = cm.get("max_skewness")
    chk("max_skewness", skew, g1["max_skewness"],
        skew is not None and skew < g1["max_skewness"], "SKEWNESS_HIGH")
    chk("wetted_area_ratio", area_ratio, g1["min_wetted_area_ratio"],
        area_ratio is not None and area_ratio >= g1["min_wetted_area_ratio"], "AREA_RATIO_LOW")
    chk("cells_min", cells, g1["min_cells"], cells is not None and cells >= g1["min_cells"],
        "TOO_FEW_CELLS")
    # informational
    metrics.append(Metric("max_aspect_ratio", cm.get("max_aspect_ratio"), None,
                          cm.get("max_aspect_ratio") is not None, "warn"))
    metrics.append(Metric("checkmesh_ok", None if cm.get("mesh_ok") is None
                          else float(cm["mesh_ok"]), None, bool(cm.get("mesh_ok")), "warn"))
    metrics.append(Metric("shm_time_s", shm.get("mesh_time_s"), None,
                          shm.get("mesh_time_s") is not None, "warn"))

    code = next((c for c in PRIORITY if c in failures), None)
    return GateResult(code is None, code, metrics)
