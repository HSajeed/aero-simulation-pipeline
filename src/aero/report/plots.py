"""Report figures (matplotlib, Agg). Every function takes rows read from the DB
and returns PNG bytes, or None when there is nothing to plot. Figures are for
humans only; no gate reads them (CLAUDE.md rule 4)."""

from __future__ import annotations

import io
import math

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

FIGSIZE = (7.5, 3.4)


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


def _segments(history: list[dict]):
    """Group history rows into contiguous (phase, tag) segments, in iteration order."""
    segs: list[tuple[str, list[dict]]] = []
    for r in sorted(history, key=lambda r: r["iteration"]):
        key = f"{r['phase']}/{r['tag']}"
        if not segs or segs[-1][0] != key:
            segs.append((key, []))
        segs[-1][1].append(r)
    return segs


def _mark_segments(ax, segs):
    for key, rows in segs[1:]:
        ax.axvline(rows[0]["iteration"], color="0.6", lw=0.8, ls="--")
    for key, rows in segs:
        ax.text(rows[0]["iteration"], 1.0, f" {key}", transform=ax.get_xaxis_transform(),
                fontsize=7, va="bottom", color="0.35")


def residuals(history: list[dict]) -> bytes | None:
    if not history:
        return None
    segs = _segments(history)
    fig, ax = plt.subplots(figsize=FIGSIZE)
    rows = sorted(history, key=lambda r: r["iteration"])
    it = [r["iteration"] for r in rows]
    for f in ("Ux", "Uy", "Uz", "p"):
        y = [r[f] if r[f] and r[f] > 0 else math.nan for r in rows]
        ax.semilogy(it, y, lw=1, label=f)
    _mark_segments(ax, segs)
    ax.set_xlabel("iteration")
    ax.set_ylabel("initial residual")
    ax.legend(fontsize=8, ncol=4, loc="upper right")
    ax.grid(True, which="major", alpha=0.3)
    return _png(fig)


def coefficients(history: list[dict], results: dict[float, dict],
                 window: int | None) -> bytes | None:
    """CL and CD convergence; the Gate 3 statistics window of each alpha shaded."""
    if not history:
        return None
    rows = sorted(history, key=lambda r: r["iteration"])
    it = [r["iteration"] for r in rows]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(7.5, 4.6), sharex=True)
    a1.plot(it, [r["Cl"] for r in rows], lw=1, color="C0")
    a2.plot(it, [r["Cd"] for r in rows], lw=1, color="C1")
    for ax in (a1, a2):
        if window:
            for res in results.values():
                if res.get("end_iter") is not None:
                    ax.axvspan(res["end_iter"] - window, res["end_iter"], color="C2", alpha=0.12)
        ax.grid(True, alpha=0.3)
    _mark_segments(a1, _segments(history))
    a1.set_ylabel("CL")
    a2.set_ylabel("CD (numerical)")
    a2.set_xlabel("iteration (shaded: Gate 3 averaging window)")
    return _png(fig)


def cfd_vs_reference(results: dict[float, dict], refs: dict[float, dict]) -> bytes | None:
    """CL vs alpha: CFD and VSPAERO (wing only)."""
    fig, ax = plt.subplots(figsize=(5.0, 3.2))
    for src, lab, st in ((results, "CFD", "o-"), (refs, "VSPAERO (wing only)", "s--")):
        pts = sorted((a, r["CL"]) for a, r in src.items() if r.get("CL") is not None)
        if pts:
            ax.plot(*zip(*pts), st, label=lab)
    if not ax.lines:
        plt.close(fig)
        return None
    ax.set_xlabel("α [°]")
    ax.set_ylabel("CL")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    return _png(fig)


def _plottable(m: dict) -> bool:
    v, t = m["value"], m["threshold"]
    if m["severity"] != "gate" or v is None or t is None or not math.isfinite(v):
        return False
    if v in (0.0, 1.0) and t in (0.0, 1.0):     # boolean checks (closed, finished, ...)
        return False
    return t > 0 and v > 0


def gate_margins(metrics: list[dict]) -> bytes | None:
    """value / threshold on a log axis for every numeric gate check (latest batch of
    each gate). Colour = the gate's own pass/fail verdict from the DB, so the plot
    needs no knowledge of whether a limit is a minimum or a maximum."""
    ms = [m for m in metrics if _plottable(m)]
    if not ms:
        return None
    fig, ax = plt.subplots(figsize=(7.5, 0.28 * len(ms) + 1.0))
    labels = [f"{m['gate']}: {m['metric']}" for m in ms]
    ratios = [m["value"] / m["threshold"] for m in ms]
    colors = ["#2a9d55" if m["pass"] else "#d1495b" for m in ms]
    y = list(range(len(ms)))
    lim = 3.0                                     # decades shown; larger margins are clipped
    logs = [max(-lim, min(lim, math.log10(r))) for r in ratios]
    ax.barh(y, logs, color=colors)
    for yi, lg, r in zip(y, logs, ratios):
        ax.text(lg + (0.05 if lg >= 0 else -0.05), yi, f"{r:.3g}", va="center",
                ha="left" if lg >= 0 else "right", fontsize=7)
    ax.set_xlim(-lim - 0.8, lim + 0.8)
    ax.axvline(0, color="0.2", lw=1)
    ax.set_yticks(y, labels, fontsize=7)
    ax.invert_yaxis()
    ax.set_xlabel("log10(value / threshold), clipped at ±3; label = ratio   "
                  "(green = pass, red = fail)")
    ax.grid(True, axis="x", alpha=0.3)
    return _png(fig)


def section_window(y: float, params: dict, geo: dict) -> tuple[float, float, float, float]:
    """x/z window around the wing section at spanwise station y (from the design
    parameters and geometry reference values in the DB)."""
    b2 = geo["span"] / 2
    c = geo["root_chord"] * (1 - (1 - params["taper"]) * min(y / b2, 1.0))
    x_le = geo["x_le_root"] + y * math.tan(math.radians(params["sweep_le"]))
    zw = -0.2 * geo["fuselage_diameter"]          # WING_Z_FRAC (geom/openvsp_build.py)
    return x_le - 0.6 * c, x_le + 1.8 * c, zw - 0.8 * c, zw + 0.8 * c


def planes(alpha: float, U_inf: float, planes_data: list[tuple[str, float, dict]],
           params: dict, geo: dict) -> bytes | None:
    """Cutting-plane contours at one alpha: u along the freestream / U_inf
    (blue < 0 = reversed flow) and Cp, on the sampled mesh faces (no interpolation).
    planes_data: [(name, y, parsed_vtk), ...]."""
    import numpy as np
    from matplotlib.collections import PolyCollection
    from matplotlib.colors import TwoSlopeNorm

    if not planes_data:
        return None
    a = math.radians(alpha)
    e = np.array([math.cos(a), 0.0, math.sin(a)])
    fig, axes = plt.subplots(len(planes_data), 2, figsize=(9.0, 3.0 * len(planes_data)),
                             squeeze=False)
    for row, (name, y, d) in enumerate(planes_data):
        x0, x1, z0, z1 = section_window(y, params, geo)
        pts = d["points"][:, [0, 2]]
        polys = [pts[p] for p in d["polygons"]]
        cen = np.array([q.mean(axis=0) for q in polys])
        keep = (cen[:, 0] > x0) & (cen[:, 0] < x1) & (cen[:, 1] > z0) & (cen[:, 1] < z1)
        polys = [q for q, k in zip(polys, keep) if k]
        us = d["cell"]["U"][keep] @ e / U_inf
        cp = 2.0 * d["cell"]["p"][keep, 0] / U_inf ** 2       # p kinematic, p_inf = 0
        for ax, vals, cmap, norm, label in (
                (axes[row, 0], us, "RdBu_r", TwoSlopeNorm(vmin=-0.5, vcenter=0.0, vmax=1.5),
                 "u·e∞ / U∞  (blue < 0: reversed flow)"),
                (axes[row, 1], cp, "viridis",
                 TwoSlopeNorm(vmin=min(-1e-3, max(-8.0, float(cp.min()))),
                              vcenter=0.0, vmax=1.0), "Cp")):
            pc = PolyCollection(polys, array=vals, cmap=cmap, norm=norm, edgecolors="face",
                                linewidths=0.2)
            ax.add_collection(pc)
            ax.set_xlim(x0, x1)
            ax.set_ylim(z0, z1)
            ax.set_aspect("equal")
            ax.set_title(f"{name}: y = {y:.2f} m   {label}", fontsize=8)
            ax.tick_params(labelsize=7)
            fig.colorbar(pc, ax=ax, shrink=0.85).ax.tick_params(labelsize=7)
        axes[row, 0].set_ylabel("z (m)", fontsize=8)
    for ax in axes[-1]:
        ax.set_xlabel("x (m)", fontsize=8)
    fig.suptitle(f"α = {alpha:g}°: final iteration (instantaneous), cutting planes", fontsize=9)
    return _png(fig)
