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


def param_maps_png(d: dict) -> bytes | None:
    """CL and CD at the higher alpha against each parameter (one dot per run with
    results, colour = status), failed runs as x on the parameter axis."""
    params = [p["name"] for p in d["cfg"]["parameters"]]
    a_hi = max(d["alphas"])
    have = [r for r in d["runs"] if a_hi in r["results"] and r["results"][a_hi]["CL"] is not None]
    failed = [r for r in d["runs"] if r["status"] == "UNRECOVERABLE"]
    if not have and not failed:
        return None
    fig, axs = plt.subplots(2, len(params), figsize=(2.6 * len(params), 5.0), squeeze=False)
    for j, p in enumerate(params):
        for i, q in enumerate(("CL", "CD")):
            ax = axs[i][j]
            ax.scatter([r["params"][p] for r in have], [r["results"][a_hi][q] for r in have],
                       c=[COLORS.get(r["status"], "0.5") for r in have], s=22)
            if failed and have:
                lo = min(r["results"][a_hi][q] for r in have)
                ax.scatter([r["params"][p] for r in failed], [lo] * len(failed), marker="x",
                           color="#b3263a", s=26, label="unrecoverable")
            if i == 1:
                ax.set_xlabel(p)
            if j == 0:
                ax.set_ylabel(f"{q} ({a_hi:g}°)")
    fig.suptitle("green: pass/accepted, orange: flagged, red: rejected; x: unrecoverable (at axis min)",
                 fontsize=9)
    return _png(fig)


def map2d_png(d: dict) -> bytes | None:
    """CL at the higher alpha over the first two parameters (filled = has results,
    x = unrecoverable)."""
    params = [p["name"] for p in d["cfg"]["parameters"]][:2]
    a_hi = max(d["alphas"])
    have = [r for r in d["runs"] if a_hi in r["results"] and r["results"][a_hi]["CL"] is not None]
    if len(params) < 2 or not have:
        return None
    fig, ax = plt.subplots(figsize=(5.6, 4.2))
    sc = ax.scatter([r["params"][params[0]] for r in have], [r["params"][params[1]] for r in have],
                    c=[r["results"][a_hi]["CL"] for r in have], cmap="viridis", s=60,
                    edgecolors=[COLORS.get(r["status"], "0.5") for r in have], linewidths=1.5)
    fail = [r for r in d["runs"] if r["status"] == "UNRECOVERABLE"]
    ax.scatter([r["params"][params[0]] for r in fail], [r["params"][params[1]] for r in fail],
               marker="x", color="#b3263a", s=50, label="unrecoverable")
    fig.colorbar(sc, ax=ax, label=f"CL ({a_hi:g}°)")
    ax.set_xlabel(params[0])
    ax.set_ylabel(params[1])
    if fail:
        ax.legend(fontsize=8)
    return _png(fig)


def _b64(png: bytes | None) -> str | None:
    return base64.b64encode(png).decode() if png else None


def render(con, study: str, runs_dir: Path) -> str:
    d = collect(con, study)
    out_dir = Path(runs_dir) / study
    for r in d["runs"]:
        rep = run_dir(runs_dir, study, r["id"]) / "report" / "report.html"
        r["report"] = f"{r['id']}/report/report.html" if rep.exists() else None
    figs = {"funnel": _b64(funnel_png(d)), "params": _b64(param_maps_png(d)),
            "map2d": _b64(map2d_png(d))}
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES),
                             autoescape=jinja2.select_autoescape(["html", "j2"]),
                             undefined=jinja2.StrictUndefined)
    env.filters["fmt"] = fmt
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
