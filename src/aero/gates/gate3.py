"""Gate 3 (automatic part): plausibility of converged results -> CHECKS_PASS
or FLAGGED. Any single violation flags; the pipeline never auto-accepts a
flagged run. All violated checks are listed in the failure code (comma
separated) and as failed metrics.

Per alpha: coefficient convergence (std of CL over the window), farfield mass
imbalance, CL vs VSPAERO (relative, denominator floored), CD > min_cd.
Across alphas: CL-alpha slope (per rad). Cm and dCm/dalpha: warn only (human
decision: wing-only VSPAERO is not a fair reference for a wing+body moment).
Null thresholds flag as UNDECIDED_THRESHOLD.
"""

from __future__ import annotations

import math

from aero.runner import GateResult, Metric


def evaluate(results: dict[float, dict], refs: dict[float, dict], g3: dict) -> GateResult:
    metrics: list[Metric] = []
    reasons: list[str] = []

    def chk(name, value, threshold, ok, code):
        ok = bool(ok) and value is not None and not (isinstance(value, float)
                                                     and not math.isfinite(value))
        metrics.append(Metric(name, value, threshold, ok))
        if not ok and code not in reasons:
            reasons.append(code)

    def need(key):
        if g3.get(key) is None:
            metrics.append(Metric(f"threshold_{key}", None, None, False))
            if "UNDECIDED_THRESHOLD" not in reasons:
                reasons.append("UNDECIDED_THRESHOLD")
            return False
        return True

    for a, r in sorted(results.items()):
        tag = f"a{a:g}"
        if need("max_coeff_std"):
            chk(f"cl_std_{tag}", r["cl_std"], g3["max_coeff_std"],
                r["cl_std"] is not None and r["cl_std"] <= g3["max_coeff_std"],
                "COEFF_NOT_CONVERGED")
        chk(f"mass_imbalance_{tag}", r["mass_imbalance"], g3["max_mass_imbalance"],
            r["mass_imbalance"] is not None and r["mass_imbalance"] <= g3["max_mass_imbalance"],
            "MASS_IMBALANCE")
        ref = refs.get(a)
        if ref is None or ref.get("CL") is None:
            chk(f"cl_vs_vspaero_{tag}", None, g3["max_cl_rel_diff_vspaero"], False, "NO_REFERENCE")
        elif need("cl_vspaero_floor"):
            diff = abs(r["CL"] - ref["CL"]) / max(abs(ref["CL"]), g3["cl_vspaero_floor"])
            chk(f"cl_vs_vspaero_{tag}", diff, g3["max_cl_rel_diff_vspaero"],
                diff <= g3["max_cl_rel_diff_vspaero"], "CL_VS_VSPAERO")
        if need("min_cd"):
            chk(f"cd_{tag}", r["CD"], g3["min_cd"], r["CD"] is not None and r["CD"] > g3["min_cd"],
                "NEGATIVE_CD")
        metrics.append(Metric(f"cm_{tag}", r["Cm"], None, r["Cm"] is not None, "warn"))
        metrics.append(Metric(f"converged_{tag}", r["converged"], None, bool(r["converged"]), "warn"))

    alphas = sorted(results)
    if len(alphas) == 2 and need("cl_alpha_slope_range"):
        a0, a1 = alphas
        da = math.radians(a1 - a0)
        slope = (results[a1]["CL"] - results[a0]["CL"]) / da
        lo, hi = g3["cl_alpha_slope_range"]
        chk("cl_alpha_slope", slope, hi, lo <= slope <= hi, "CL_ALPHA_SLOPE")
        cm_slope = (results[a1]["Cm"] - results[a0]["Cm"]) / da
        metrics.append(Metric("cm_alpha_slope", cm_slope, None, True, "warn"))
        if refs.get(a0) and refs.get(a1) and refs[a0].get("Cm") is not None:
            metrics.append(Metric("cm_alpha_slope_vspaero",
                                  (refs[a1]["Cm"] - refs[a0]["Cm"]) / da, None, True, "warn"))
    elif len(alphas) != 2:
        chk("n_alphas", float(len(alphas)), 2, False, "MISSING_RESULTS")

    return GateResult(not reasons, ",".join(reasons) or None, metrics)
