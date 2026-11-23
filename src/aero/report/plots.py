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
    alphas = sorted(set(results) | set(refs))
    if not alphas:
        return None
    fig, ax = plt.subplots(figsize=(5.0, 3.2))
    x = range(len(alphas))
    w = 0.38
    cfd = [results.get(a, {}).get("CL") for a in alphas]
    ref = [refs.get(a, {}).get("CL") for a in alphas]
    ax.bar([i - w / 2 for i in x], [v if v is not None else 0 for v in cfd], w, label="CFD")
    ax.bar([i + w / 2 for i in x], [v if v is not None else 0 for v in ref], w,
           label="VSPAERO (wing only)")
    for i, (c, r) in enumerate(zip(cfd, ref)):
        if c is None:
            ax.text(i - w / 2, 0, "n/a", ha="center", va="bottom", fontsize=8)
        if r is None:
            ax.text(i + w / 2, 0, "n/a", ha="center", va="bottom", fontsize=8)
    ax.axhline(0, color="0.3", lw=0.8)
    ax.set_xticks(list(x), [f"α = {a:g}°" for a in alphas])
    ax.set_ylabel("CL")
    ax.legend(fontsize=8)
    ax.grid(True, axis="y", alpha=0.3)
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
