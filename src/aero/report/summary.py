"""Study summary (Phase 8): one self-contained HTML page per study, from the DB.

Funnel (how many runs passed each gate, how many of those after an automatic
correction), corrections by type, flag rate and human review outcomes, wall
time per stage, CL/CD over the parameter space, and a table of every run with
a link to its report. Every number is read from the run DB (CLAUDE.md rule 5);
regenerating from an unchanged DB gives the same page apart from the timestamp.
"""

from __future__ import annotations

import base64
import json
from collections import Counter, defaultdict
from pathlib import Path

import jinja2
import yaml

from aero import db
from aero.report.html import TEMPLATES, fmt
from aero.report.plots import _png, plt
from aero.runner import run_dir

# funnel steps: (gate, label); a run "passed" a gate if a later stage was reached
STEPS = [("gate0", "Gate 0 geometry"), ("gate1", "Gate 1 mesh"), ("gate2", "Gate 2 pilot"),
         ("prod", "Production"), ("gate3", "Gate 3 plausibility")]
# furthest gate passed for each non-UNRECOVERABLE status (index into STEPS, -1 = none)
PASSED = {"PENDING": -1, "GEOM_FAIL": -1, "GEOM_OK": 0, "MESH_FAIL": 0, "MESH_OK": 1,
          "PILOT_FAIL": 1, "PILOT_OK": 2, "PROD_FAIL": 2, "PROD_DONE": 3,
          "FLAGGED": 3, "ACCEPTED": 3, "REJECTED": 3, "CHECKS_PASS": 4}
FLAG_STATES = ("FLAGGED", "ACCEPTED", "REJECTED")
GATE_INDEX = {g: i for i, (g, _) in enumerate(STEPS)}


def _furthest(con, run: dict) -> int:
    """Index of the last gate the run passed. UNRECOVERABLE: the gate it stopped
    at is the gate of its last gate_results row."""
    if run["status"] != "UNRECOVERABLE":
        return PASSED[run["status"]]
    row = con.execute("SELECT gate FROM gate_results WHERE run=? ORDER BY id DESC LIMIT 1",
                      (run["id"],)).fetchone()
    return GATE_INDEX.get(row["gate"], 0) - 1 if row else -1


def _metric_sum(con, run_id: str, gate: str, metric: str) -> float:
    r = con.execute("SELECT SUM(value) FROM gate_results WHERE run=? AND gate=? AND metric=?",
                    (run_id, gate, metric)).fetchone()[0]
    return float(r or 0.0)


def collect(con, study: str) -> dict:
    cfg = yaml.safe_load(con.execute("SELECT config_yaml FROM studies WHERE name=?",
                                     (study,)).fetchone()[0])
    alphas = [float(a) for a in cfg["alphas"]]
    runs = []
    for r in db.list_runs(con, study):
        r = dict(r)
        r["params"] = json.loads(r["params"])
        r["furthest"] = _furthest(con, r)
        r["corrections"] = [dict(c) for c in db.corrections_for(con, r["id"])]
        r["results"] = {a: dict(x) for a, x in db.get_results(con, r["id"]).items()}
        rev = con.execute("SELECT decision, label FROM reviews WHERE run=? AND decision!='label' "
                          "ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        r["review"] = dict(rev) if rev else None
        fl = con.execute("SELECT label FROM reviews WHERE run=? AND decision='label'",
                         (r["id"],)).fetchone()        # failure label (V15), status unchanged
        r["failure_label"] = fl["label"] if fl else None
        r["wall"] = {"mesh": _metric_sum(con, r["id"], "gate1", "mesh_wall_time_s"),
                     "pilot": _metric_sum(con, r["id"], "gate2", "pilot_wall_time_s"),
                     "prod": sum(x["wall_time"] or 0.0 for x in r["results"].values())}
        runs.append(r)

    n = len(runs)
    funnel = []
    for i, (gate, label) in enumerate(STEPS):
        passed = [r for r in runs if r["furthest"] >= i]
        corrected = [r for r in passed if any(c["gate"] == gate for c in r["corrections"])]
        funnel.append({"gate": gate, "label": label, "passed": len(passed),
                       "corrected": len(corrected),
                       "stopped": len([r for r in runs if r["furthest"] == i - 1
                                       and r["status"] not in ("PENDING",) + FLAG_STATES])})
    reached_g3 = [r for r in runs if r["furthest"] >= 3]
    flagged = [r for r in runs if r["status"] in FLAG_STATES]

    corr = defaultdict(lambda: {"n": 0, "runs": set(), "recovered": set()})
    for r in runs:
        for c in r["corrections"]:
            k = (c["gate"], c["code"], c["param"])
            corr[k]["n"] += 1
            corr[k]["runs"].add(r["id"])
            if r["furthest"] >= GATE_INDEX.get(c["gate"], 99):
                corr[k]["recovered"].add(r["id"])
    corrections = [{"gate": g, "code": code, "param": p, "n": v["n"], "runs": len(v["runs"]),
                    "recovered": len(v["recovered"])}
                   for (g, code, p), v in sorted(corr.items(), key=lambda kv: -kv[1]["n"])]

    wall = {k: sum(r["wall"][k] for r in runs) for k in ("mesh", "pilot", "prod")}
    return {
        "study": study, "cfg": cfg, "alphas": alphas, "runs": runs, "n": n,
        "status_counts": Counter(r["status"] for r in runs),
        "funnel": funnel, "reached_g3": len(reached_g3), "flagged": len(flagged),
        "auto_corrected_runs": len({r["id"] for r in runs if r["corrections"]}),
        "recovered_runs": len({r["id"] for r in runs if r["corrections"]
                               and r["status"] != "UNRECOVERABLE"}),
        "flag_reasons": Counter(c for r in flagged for c in (r["failure_code"] or "").split(",") if c),
        "fail_codes": Counter(r["failure_code"] for r in runs if r["status"] == "UNRECOVERABLE"),
        "decisions": Counter(r["review"]["decision"] for r in flagged if r["review"]),
        "review_labels": Counter(r["review"]["label"] for r in flagged if r["review"]),
        "failure_labels": Counter(r["failure_label"] or "unlabelled"
                                  for r in runs if r["status"] == "UNRECOVERABLE"),
        "corrections": corrections, "wall": wall, "wall_total": sum(wall.values()),
    }


# ------------------------------------------------------------------ figures
def funnel_png(d: dict) -> bytes | None:
    if not d["n"]:
        return None
    labels = ["started"] + [f["label"] for f in d["funnel"]]
    passed = [d["n"]] + [f["passed"] for f in d["funnel"]]
    corrected = [0] + [f["corrected"] for f in d["funnel"]]
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    y = list(range(len(labels)))[::-1]
    ax.barh(y, passed, color="#4c78a8", label="passed")
    ax.barh(y, corrected, color="#f58518", label="passed after auto-correction")
    for yi, p in zip(y, passed):
        ax.text(p, yi, f" {p}", va="center", fontsize=9)
    if d["flagged"]:
        ax.text(0.99, 0.02, f"flagged at Gate 3: {d['flagged']}", transform=ax.transAxes,
                ha="right", fontsize=9, color="#b35c00")
    ax.set_yticks(y, labels)
    ax.set_xlim(0, d["n"] * 1.12)
    ax.set_xlabel("runs")
    ax.legend(fontsize=8, loc="lower left")
    ax.set_title(f"{d['study']}: funnel ({d['n']} runs)")
    return _png(fig)


COLORS = {"CHECKS_PASS": "#1f7a45", "ACCEPTED": "#1f7a45", "FLAGGED": "#d97a00",
          "REJECTED": "#b3263a"}
MARKERS = "os^Dv<>p*h"


def _num(r: dict) -> str:
    return r["id"].rsplit("-", 1)[-1].lstrip("0") or "0"


def _cl(r: dict) -> list[tuple[float, float]]:
    return sorted((a, x["CL"]) for a, x in r["results"].items() if x.get("CL") is not None)


def _unit(p: dict) -> str:
    u = {"deg": "°"}.get(p.get("unit", ""), p.get("unit", ""))
    return f"{p['name']} [{u}]" if u else p["name"]


def _status_legend(ax, runs, extra=()):
    from matplotlib.lines import Line2D
    h = [Line2D([], [], color=c, marker="o", ls="", label=s) for s, c in COLORS.items()
         if any(r["status"] == s for r in runs)]
    ax.legend(handles=h + list(extra), fontsize=7, loc="best")


def cl_alpha_png(d: dict) -> bytes | None:
    """CL vs alpha, one line per run, labelled with its run number."""
    runs = [r for r in d["runs"] if _cl(r)]
    if not runs:
        return None
    fig, ax = plt.subplots(figsize=(7.5, 4.6))
    for k, r in enumerate(runs):
        pts, c = _cl(r), COLORS.get(r["status"], "0.5")
        ax.plot(*zip(*pts), "o-", color=c, lw=1, ms=3)
        end, dx = (pts[-1], 4) if k % 2 else (pts[0], -4)    # alternate ends to reduce overlap
        ax.annotate(_num(r), end, xytext=(dx, 0), textcoords="offset points", fontsize=7, color=c,
                    va="center", ha="left" if dx > 0 else "right")
    ax.set_xlabel("α [°]")
    ax.set_ylabel("CL")
    ax.grid(True, alpha=0.3)
    _status_legend(ax, runs)
    return _png(fig)


def cl_params_png(d: dict) -> bytes | None:
    """CL vs each parameter; marker = alpha, colour = status, number = run."""
    from matplotlib.lines import Line2D
    runs = [r for r in d["runs"] if _cl(r)]
    params = d["cfg"]["parameters"]
    if not runs or not params:
        return None
    ncol = min(3, len(params))
    nrow = -(-len(params) // ncol)
    fig, axs = plt.subplots(nrow, ncol, figsize=(4.0 * ncol, 3.4 * nrow), squeeze=False)
    mk = {a: MARKERS[i % len(MARKERS)] for i, a in enumerate(d["alphas"])}
    for ax, p in zip(axs.flat, params):
        for r in runs:
            x, pts, c = r["params"][p["name"]], _cl(r), COLORS.get(r["status"], "0.5")
            ax.plot([x] * len(pts), [v for _, v in pts], color="0.8", lw=0.8, zorder=1)
            for a, v in pts:
                ax.scatter(x, v, marker=mk.get(a, "o"), color=c, s=22, zorder=2)
            ax.annotate(_num(r), (x, pts[-1][1]), xytext=(3, 3), textcoords="offset points",
                        fontsize=6, color=c)
        ax.set_xlabel(_unit(p))
        ax.set_ylabel("CL")
        ax.grid(True, alpha=0.3)
    for ax in list(axs.flat)[len(params):]:
        ax.axis("off")
    _status_legend(axs.flat[0], runs, [Line2D([], [], color="k", marker=m, ls="", label=f"α = {a:g}°")
                                       for a, m in mk.items()])
    fig.tight_layout()
    return _png(fig)


def _b64(png: bytes | None) -> str | None:
    return base64.b64encode(png).decode() if png else None


def render(con, study: str, runs_dir: Path) -> str:
    d = collect(con, study)
    out_dir = Path(runs_dir) / study
    for r in d["runs"]:
        rep = run_dir(runs_dir, study, r["id"]) / "report" / "report.html"
        r["report"] = f"{r['id']}/report/report.html" if rep.exists() else None
    figs = {"funnel": _b64(funnel_png(d)), "cl_alpha": _b64(cl_alpha_png(d)),
            "cl_params": _b64(cl_params_png(d))}
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES),
                             autoescape=jinja2.select_autoescape(["html", "j2"]),
                             undefined=jinja2.StrictUndefined)
    env.filters["fmt"] = fmt
    env.filters["unit"] = _unit
    return env.get_template("summary.html.j2").render(
        d=d, figs=figs, generated=db.now(), db_file=str(Path(runs_dir) / "runs.db"),
        out_dir=str(out_dir))


def write_summary(con, runs_dir: Path, study: str) -> Path:
    out = Path(runs_dir) / study / "summary.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(render(con, study, runs_dir))
    tmp.replace(out)
    return out
