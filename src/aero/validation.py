"""Phase 7: mesh convergence (GCI) and CL-alpha validation, all from the run DB.

GCI: Celik et al. (2008), "Procedure for estimation and reporting of
uncertainty due to discretization in CFD applications", J. Fluids Eng. 130.
Three grids, index 1 = fine, 3 = coarse. Two definitions of the cell size h
are evaluated, because snappyHexMesh meshes are not geometrically similar
(transition bands are nCellsBetweenLevels *cells* thick, so near-surface bands
thin out with refinement and N grows ~r^2, not r^3):
  "cells":     h = (V / N)^(1/3), Celik's global measure (same domain volume V
               on every level: the GCI studies set mesh.domain_cell_size);
  "geometric": h = blockMesh base cell size; every surface/feature/box cell
               scales with it (r = sqrt(2) here).
Observed order p from the fixed-point iteration of eq. (5), Richardson
extrapolation eq. (6), GCI_fine = Fs * e_a / (r^p - 1) with Fs = 1.25.

Batch mesh choice. Original rule (human 2026-11-25): the coarsest level whose
GCI for CL at the higher alpha and for the CL-alpha slope is <= 3%, under BOTH
h definitions. No level met it (CL(4) observed order 0.2-0.45, GCI 11-21% even
on the fine level). Relaxed rule (human 2026-11-25, after seeing the data;
choose_level_relaxed): CL-alpha GCI <= 3% under both h definitions AND the
relative change of CL(alpha_hi) to the next finer level <= 1%.
CD is excluded: with slip walls it is numerical drag, not physical drag.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

import numpy as np

FS = 1.25          # Celik et al.: safety factor for a three-grid study
LEVELS = ("fine", "medium", "coarse")


# ------------------------------------------------------------------ GCI
@dataclass
class GCI:
    phi: tuple[float, float, float]      # fine, medium, coarse
    h: tuple[float, float, float]
    r21: float
    r32: float
    p: float | None                      # observed order (None: not computable)
    convergence: str                     # monotone | oscillatory | divergent | converged
    phi_ext: float | None                # Richardson extrapolation (fine pair)
    e_a21: float                         # |(phi1 - phi2) / phi1|
    e_ext21: float | None                # |(phi_ext - phi1) / phi_ext|
    gci_fine: float | None               # GCI_21 (on the fine grid)
    gci_medium: float | None             # GCI_32 (on the medium grid)
    gci_coarse: float | None             # Fs |phi_ext - phi3| / |phi3| (estimate, same form)
    asymptotic: float | None             # GCI_32 / (r21^p GCI_21), ~1 in the asymptotic range

    def gci_at(self, level: str) -> float | None:
        return {"fine": self.gci_fine, "medium": self.gci_medium,
                "coarse": self.gci_coarse}[level]

    def as_dict(self) -> dict:
        return asdict(self)


def observed_order(e21: float, e32: float, r21: float, r32: float,
                   tol: float = 1e-10, max_iter: int = 200) -> float | None:
    """Celik eq. (5a-c): p = |ln|e32/e21| + q(p)| / ln r21,
    q(p) = ln((r21^p - s) / (r32^p - s)), s = sign(e32/e21)."""
    if e21 == 0 or e32 == 0:
        return None
    s = math.copysign(1.0, e32 / e21)
    p = abs(math.log(abs(e32 / e21))) / math.log(r21)        # start: q = 0
    for _ in range(max_iter):
        num, den = r21 ** p - s, r32 ** p - s
        if num <= 0 or den <= 0:
            return None
        p_new = abs(math.log(abs(e32 / e21)) + math.log(num / den)) / math.log(r21)
        if abs(p_new - p) < tol:
            return p_new
        p = p_new
    return None


def gci(phi: tuple[float, float, float], h: tuple[float, float, float], fs: float = FS) -> GCI:
    """phi, h ordered fine, medium, coarse (h1 < h2 < h3)."""
    f1, f2, f3 = map(float, phi)
    h1, h2, h3 = map(float, h)
    if not h1 < h2 < h3:
        raise ValueError("need h_fine < h_medium < h_coarse")
    r21, r32 = h2 / h1, h3 / h2
    e21, e32 = f2 - f1, f3 - f2
    e_a21 = abs((f1 - f2) / f1) if f1 else math.inf
    if e21 == 0 and e32 == 0:
        return GCI(phi, h, r21, r32, None, "converged", f1, 0.0, 0.0, 0.0, 0.0, 0.0, None)
    ratio = e32 / e21 if e21 else math.inf
    if ratio < 0:
        conv = "oscillatory"
    elif abs(e21) >= abs(e32):
        conv = "divergent"           # change does not shrink with refinement
    else:
        conv = "monotone"
    p = observed_order(e21, e32, r21, r32) if conv == "monotone" else None
    if p is None or p <= 0:
        return GCI(phi, h, r21, r32, p, conv, None, e_a21, None, None, None, None, None)
    a21, a32 = r21 ** p, r32 ** p
    ext = (a21 * f1 - f2) / (a21 - 1)
    g21 = fs * e_a21 / (a21 - 1)
    g32 = fs * abs((f2 - f3) / f2) / (a32 - 1) if f2 else math.inf
    g3 = fs * abs((ext - f3) / f3) if f3 else math.inf
    return GCI(phi, h, r21, r32, p, conv, ext, e_a21, abs((ext - f1) / ext), g21, g32, g3,
               g32 / (a21 * g21) if g21 else None)


def choose_level(results: dict[str, GCI], limit: float) -> str | None:
    """Coarsest level at which every GCI given is <= limit; None if none
    qualifies (or a GCI is not computable: then a human decides). Pass the
    GCIs of both h definitions to require both."""
    for level in reversed(LEVELS):                 # coarse, medium, fine
        vals = [g.gci_at(level) for g in results.values()]
        if all(v is not None and v <= limit for v in vals):
            return level
    return None


def choose_level_relaxed(slope_gcis: list[GCI], cl: dict[str, float],
                         slope_limit: float, cl_change_limit: float) -> str | None:
    """Coarsest of coarse/medium with every slope GCI <= slope_limit and
    |CL_k - CL_finer| / |CL_finer| <= cl_change_limit. The fine level has no
    finer comparison, so it is never chosen here (None -> human decides)."""
    finer = {"coarse": "medium", "medium": "fine"}
    for level in ("coarse", "medium"):
        g_ok = all(g.gci_at(level) is not None and g.gci_at(level) <= slope_limit
                   for g in slope_gcis)
        f = cl[finer[level]]
        if g_ok and f and abs(cl[level] - f) / abs(f) <= cl_change_limit:
            return level
    return None


# --------------------------------------------------------- lifting line
def helmbold_slope(span: float, sref_full: float, taper: float, sweep_le_deg: float,
                   a0: float = 2 * math.pi) -> float:
    """Helmbold / DATCOM lifting-line CL-alpha (per rad) for a straight-tapered
    wing: a = 2 pi A / (2 + sqrt(4 + A^2 (1 + tan^2 L_c/2) (2 pi / a0)^2)).
    A = b^2 / S (planform from the centreline, decision G7), half-chord sweep
    from tan L_x = tan L_LE - 4 x (1 - taper) / (A (1 + taper)), x = 0.5.
    Thin-airfoil a0 = 2 pi. Wing only: no fuselage."""
    A = span ** 2 / sref_full
    t = math.tan(math.radians(sweep_le_deg)) - 2 * (1 - taper) / (A * (1 + taper))
    k = a0 / (2 * math.pi)
    return 2 * math.pi * A / (2 + math.sqrt(4 + (A / k) ** 2 * (1 + t * t)))


def slope(cl_lo: float, cl_hi: float, a_lo: float, a_hi: float) -> float:
    return (cl_hi - cl_lo) / math.radians(a_hi - a_lo)


# ------------------------------------------------------------ DB access
def _last_gate_metric(con, run_id: str, gate: str, metric: str) -> float | None:
    row = con.execute("SELECT value FROM gate_results WHERE run=? AND gate=? AND metric=? "
                      "ORDER BY attempt DESC, id DESC LIMIT 1", (run_id, gate, metric)).fetchone()
    return None if row is None else row[0]


def run_summary(con, run_id: str) -> dict:
    """Everything Phase 7 needs about one finished run, read from the DB."""
    run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    geom = con.execute("SELECT * FROM geometry WHERE run=?", (run_id,)).fetchone()
    res = {r["alpha"]: dict(r) for r in
           con.execute("SELECT * FROM results WHERE run=? ORDER BY alpha", (run_id,))}
    ref = {r["alpha"]: dict(r) for r in
           con.execute("SELECT * FROM reference_results WHERE run=? AND source='vspaero'",
                       (run_id,))}
    corr = [dict(r) for r in con.execute("SELECT gate, code, param, old, new, attempt "
                                         "FROM corrections WHERE run=? ORDER BY id", (run_id,))]
    params = json.loads(run["params"])
    out = {"run": run_id, "study": run["study"], "status": run["status"], "params": params,
           "cells": _last_gate_metric(con, run_id, "gate1", "cells_max"),
           "max_skewness": _last_gate_metric(con, run_id, "gate1", "max_skewness"),
           "max_non_orth": _last_gate_metric(con, run_id, "gate1", "max_non_orth"),
           "mesh_wall_s": _last_gate_metric(con, run_id, "gate1", "mesh_wall_time_s"),
           "results": res, "vspaero": ref, "corrections": corr}
    if geom is not None:
        out.update(sref_full=geom["sref_full"], span=geom["span"], mac=geom["mac"])
    alphas = sorted(res)
    if len(alphas) == 2 and all(res[a]["CL"] is not None for a in alphas):
        out["slope_cfd"] = slope(res[alphas[0]]["CL"], res[alphas[1]]["CL"], *alphas)
        out["solve_wall_s"] = sum(res[a]["wall_time"] or 0 for a in alphas)
    if len(ref) == 2:
        a = sorted(ref)
        out["slope_vspaero"] = slope(ref[a[0]]["CL"], ref[a[1]]["CL"], *a)
    if geom is not None:
        out["slope_helmbold"] = helmbold_slope(geom["span"], geom["sref_full"],
                                               params["taper"], params["sweep_le"])
    return out


def pilot_wall_s(con, run_id: str) -> float | None:
    """Wall time of the passing pilot (Gate 2), if recorded."""
    return _last_gate_metric(con, run_id, "gate2", "pilot_wall_time_s")


def level_table(con, studies: dict[str, str]) -> dict[str, dict]:
    """{level: run summary + base_cell_size} for the single run of each GCI study."""
    import yaml
    out = {}
    for level, study in studies.items():
        ids = [r[0] for r in con.execute("SELECT id FROM runs WHERE study=? ORDER BY idx", (study,))]
        if len(ids) != 1:
            raise ValueError(f"GCI study {study!r} must have exactly one run, has {len(ids)}")
        out[level] = run_summary(con, ids[0])
        cfg = yaml.safe_load(con.execute("SELECT config_yaml FROM studies WHERE name=?",
                                         (study,)).fetchone()[0])
        out[level]["base_cell_size"] = cfg["mesh"]["base_cell_size"]
        out[level]["mesh_cfg"] = cfg["mesh"]
    return out


def gci_study(levels: dict[str, dict], alpha_hi: float, alpha_lo: float,
              h_mode: str = "cells") -> dict[str, GCI]:
    """GCI for CL(alpha_hi), CL-alpha slope, CL(alpha_lo) and CD(alpha_hi).
    h_mode "cells": h = (1 / N)^(1/3) (common domain; V cancels in every ratio);
    "geometric": h = the level's blockMesh base cell size."""
    if h_mode == "cells":
        h = tuple((1.0 / levels[k]["cells"]) ** (1 / 3) for k in LEVELS)
    elif h_mode == "geometric":
        h = tuple(float(levels[k]["base_cell_size"]) for k in LEVELS)
    else:
        raise ValueError(h_mode)

    def q(f):
        return tuple(f(levels[k]) for k in LEVELS)

    return {
        f"CL({alpha_hi:g})": gci(q(lambda s: s["results"][alpha_hi]["CL"]), h),
        "CL_alpha": gci(q(lambda s: s["slope_cfd"]), h),
        f"CL({alpha_lo:g})": gci(q(lambda s: s["results"][alpha_lo]["CL"]), h),
        f"CD({alpha_hi:g})": gci(q(lambda s: s["results"][alpha_hi]["CD"]), h),
    }


def baseline_slopes(con, study: str = "baseline") -> list[dict]:
    """CFD / VSPAERO / Helmbold CL-alpha slope for every finished run of a study."""
    rows = []
    for (rid,) in con.execute("SELECT id FROM runs WHERE study=? ORDER BY idx", (study,)):
        s = run_summary(con, rid)
        if "slope_cfd" in s:
            rows.append(s)
    return rows


def np_summary(values) -> tuple[float, float]:
    a = np.asarray(values, dtype=float)
    return float(a.min()), float(a.max())
