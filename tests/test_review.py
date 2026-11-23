"""Phase 6: review queue, reports, review CLI. Synthetic DB in tmp_path only
(never runs/): statuses and metrics are written directly to exercise the code."""

import json
import math
import re

import pytest
from typer.testing import CliRunner

from aero import db
from aero.cli import app
from aero.config import load_review
from aero.report import html, plots
from aero.review import queue
from aero.runner import db_path, init_study
from aero.status import Status
from conftest import ROOT

cli = CliRunner()
G3 = {"max_coeff_std": 1e-3, "max_mass_imbalance": 1e-3, "max_cl_rel_diff_vspaero": 0.25,
      "cl_vspaero_floor": 0.05, "cl_alpha_slope_range": [3.0, 6.0], "min_cd": 0.0,
      "coeff_std_window": 200}
LABELS = load_review(ROOT / "configs/review.yaml").labels


def _flag(con, rid, metrics, code, t="2026-01-01T00:00:00+00:00"):
    """Put a run into FLAGGED with the given failed gate3 metrics (synthetic)."""
    con.execute("UPDATE runs SET status=?, failure_code=? WHERE id=?", (Status.FLAGGED, code, rid))
    for name, value, thr in metrics:
        con.execute("INSERT INTO gate_results (run, gate, attempt, metric, value, threshold, pass,"
                    " severity, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (rid, "gate3", 0, name, value, thr, 0, "gate", t))
    con.execute("INSERT INTO gate_results (run, gate, attempt, metric, value, threshold, pass,"
                " severity, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, "gate3", 0, "cm_a0", 0.01, None, 1, "warn", t))


@pytest.fixture
def study(tmp_path, study_cfg):
    init_study(study_cfg, tmp_path)
    con = db.connect(db_path(tmp_path))
    return tmp_path, con


def _full_run(con, rid):
    """Results, reference, history and gate metrics for a complete (flagged) run."""
    _flag(con, rid, [("cl_vs_vspaero_a0", 0.4, 0.25)], "CL_VS_VSPAERO")
    for a, cl in ((0.0, 0.02), (4.0, 0.33)):
        db.put_result(con, rid, a, {"CL": cl, "CD": 0.01, "Cm": -0.01, "iterations": 400,
                                    "wall_time": 100.0, "cl_std": 1e-6, "mass_imbalance": 1e-9,
                                    "converged": 1, "start_iter": 1, "end_iter": 400 * (1 + a / 4)})
    db.put_reference(con, rid, "vspaero", [{"alpha": 0.0, "CL": 0.0, "CDi": 0.0, "Cm": 0.0},
                                           {"alpha": 4.0, "CL": 0.30, "CDi": 0.004, "Cm": 0.01}])
    rows = [{"iteration": i, "Ux": 10 ** (-i / 100), "Uy": 1e-3, "Uz": 1e-3, "p": 1e-2,
             "Cl": 0.02, "Cd": 0.01, "Cm": -0.01} for i in range(1, 401)]
    db.put_history(con, rid, "pilot", "a0", 0.0, rows[:300])
    db.put_history(con, rid, "prod", "alpha0", 0.0, rows[300:])
    db.add_gate_results(con, rid, "gate1", 0, [
        {"metric": "max_non_orth", "value": 50.0, "threshold": 70.0, "pass": True},
        {"metric": "closed_boundary", "value": 1.0, "threshold": 1.0, "pass": True}])
    db.add_correction(con, rid, "gate1", "SKEWNESS_HIGH", "add mesh.n_smooth_patch 2",
                      "mesh.n_smooth_patch", 3, 5, 1)


# ------------------------------------------------------------------ severity
def test_exceedance():
    assert queue.exceedance("cl_vs_vspaero_a4", 0.5, 0.25, G3) == pytest.approx(2.0)
    assert queue.exceedance("cl_alpha_slope", 7.5, 6.0, G3) == pytest.approx(1.25)
    assert queue.exceedance("cl_alpha_slope", 2.0, 6.0, G3) == pytest.approx(1.5)
    assert queue.exceedance("cl_alpha_slope", -1.0, 6.0, G3) == math.inf
    assert queue.exceedance("cl_vs_vspaero_a0", None, 0.25, G3) == math.inf
    assert queue.exceedance("mass_imbalance_a0", float("nan"), 1e-3, G3) == math.inf
    assert queue.exceedance("cd_a0", -0.001, 0.0, G3) is None


def test_queue_order(study):
    runs_dir, con = study
    _flag(con, "baseline-0001", [("cl_vs_vspaero_a0", 0.5, 0.25)], "CL_VS_VSPAERO")       # 2x
    _flag(con, "baseline-0002", [("cl_vs_vspaero_a0", 0.3, 0.25),
                                 ("cd_a0", -0.001, 0.0)], "CL_VS_VSPAERO,NEGATIVE_CD")    # 2 viol.
    _flag(con, "baseline-0003", [("mass_imbalance_a4", 1.1e-3, 1e-3)], "MASS_IMBALANCE")
    _flag(con, "baseline-0004", [("cl_vs_vspaero_a4", 0.3, 0.25)], "CL_VS_VSPAERO")       # 1.2x
    con.execute("UPDATE runs SET status='CHECKS_PASS' WHERE id='baseline-0005'")
    q = queue.flagged(con, "baseline", G3)
    assert [e.run_id for e in q] == ["baseline-0002", "baseline-0003", "baseline-0001",
                                     "baseline-0004"]
    assert q[0].n_violations == 2 and q[0].top_check == "CL_VS_VSPAERO"
    assert q[0].exceedance == pytest.approx(1.2)          # cd has no scale, skipped
    assert q[1].top_check == "MASS_IMBALANCE"


def test_latest_batch_only(study):
    _, con = study
    _flag(con, "baseline-0001", [("cl_vs_vspaero_a0", 9.0, 0.25)], "CL_VS_VSPAERO",
          t="2026-01-01T00:00:00+00:00")
    _flag(con, "baseline-0001", [("cl_vs_vspaero_a0", 0.5, 0.25)], "CL_VS_VSPAERO",
          t="2026-01-02T00:00:00+00:00")
    (e,) = queue.flagged(con, "baseline", G3)
    assert e.exceedance == pytest.approx(2.0) and e.n_violations == 1


# ------------------------------------------------------------------ decisions
def test_decide_accept_and_reject(study):
    _, con = study
    _flag(con, "baseline-0001", [("cl_vs_vspaero_a0", 0.5, 0.25)], "CL_VS_VSPAERO")
    _flag(con, "baseline-0002", [("cl_vs_vspaero_a0", 0.5, 0.25)], "CL_VS_VSPAERO")
    row = queue.decide(con, "baseline-0001", True, "ok_false_alarm", "fuselage lift", LABELS, "me")
    assert row["status"] == Status.ACCEPTED and row["failure_code"] == "CL_VS_VSPAERO"
    row = queue.decide(con, "baseline-0002", False, "mesh_too_coarse", None, LABELS)
    assert row["status"] == Status.REJECTED
    revs = queue.reviews_for(con, "baseline-0001")
    assert [(r["decision"], r["label"], r["note"], r["reviewer"]) for r in revs] == [
        ("accept", "ok_false_alarm", "fuselage lift", "me")]
    assert queue.reviews_for(con, "baseline-0002")[0]["reviewer"]    # login name default


@pytest.mark.parametrize("label,note,msg", [("bogus", None, "not in vocabulary"),
                                            ("other", None, "requires --note"),
                                            ("other", "  ", "requires --note")])
def test_decide_bad_label(study, label, note, msg):
    _, con = study
    _flag(con, "baseline-0001", [], "CL_VS_VSPAERO")
    with pytest.raises(queue.ReviewError, match=msg):
        queue.decide(con, "baseline-0001", True, label, note, LABELS)
    assert db.get_run(con, "baseline-0001")["status"] == Status.FLAGGED
    assert queue.reviews_for(con, "baseline-0001") == []


def test_decide_only_flagged(study):
    _, con = study
    with pytest.raises(queue.ReviewError, match="not FLAGGED"):     # PENDING
        queue.decide(con, "baseline-0001", True, "ok_false_alarm", None, LABELS)
    _flag(con, "baseline-0001", [], "CL_VS_VSPAERO")
    queue.decide(con, "baseline-0001", False, "not_converged", None, LABELS)
    with pytest.raises(queue.ReviewError, match="not FLAGGED"):     # already decided
        queue.decide(con, "baseline-0001", True, "ok_false_alarm", None, LABELS)
    assert len(queue.reviews_for(con, "baseline-0001")) == 1


def test_review_labels_config():
    assert LABELS == ["ok_false_alarm", "mesh_too_coarse", "separated_flow", "geometry_artifact",
                      "not_converged", "reference_wrong", "other"]


# ------------------------------------------------------------------ reports
def test_fmt():
    assert html.fmt(None) == "—"
    assert html.fmt(3.0) == "3" and html.fmt(7) == "7"
    assert html.fmt(0.123456789) == "0.1235"
    assert html.fmt(float("inf")) == "inf"


def test_report_pending_run(study, gates_cfg):
    runs_dir, con = study
    out = html.write_report(con, runs_dir, "baseline-0001", gates_cfg.model_dump(mode="json"))
    s = out.read_text()
    assert out == runs_dir / "baseline" / "baseline-0001" / "report" / "report.html"
    assert "PENDING" in s and "No results yet" in s and "data:image/png" not in s


def test_report_full_run(study, gates_cfg):
    runs_dir, con = study
    _full_run(con, "baseline-0001")
    s = html.render(con, "baseline-0001", gates_cfg.model_dump(mode="json"))
    assert s.count("data:image/png;base64,") == 4
    assert "CL_VS_VSPAERO" in s and "SKEWNESS_HIGH" in s and "mesh.n_smooth_patch" in s
    assert "0.33" in s and "numerical drag" in s
    # |0.02 - 0| / max(0, 0.05) = 0.4 and |0.33 - 0.30| / 0.30 = 0.1 from the DB values
    assert re.search(r">0\.4<", s) and re.search(r">0\.1<", s)
    assert "1.6×" in s                                             # exceedance 0.4 / 0.25


def test_plots_empty_inputs():
    assert plots.residuals([]) is None and plots.coefficients([], {}, 200) is None
    assert plots.cfd_vs_reference({}, {}) is None and plots.gate_margins([]) is None
    png = plots.cfd_vs_reference({0.0: {"CL": 0.1}}, {})        # missing reference
    assert png.startswith(b"\x89PNG")


# ------------------------------------------------------------------ CLI end to end
def test_review_cli(study):
    runs_dir, con = study
    _full_run(con, "baseline-0001")
    _flag(con, "baseline-0002", [("mass_imbalance_a4", 5e-3, 1e-3)], "MASS_IMBALANCE")
    rd = ["--runs-dir", str(runs_dir), "--gates", str(ROOT / "configs/gates.yaml")]

    r = cli.invoke(app, ["review", "list", "baseline", *rd])
    assert r.exit_code == 0, r.output
    lines = [l for l in r.output.splitlines() if "baseline-" in l]
    assert lines[0].split()[1] == "baseline-0002" and "5.00x" in lines[0]
    assert lines[1].split()[1] == "baseline-0001" and "1.60x" in lines[1]

    r = cli.invoke(app, ["review", "open", "baseline-0001", *rd])
    assert r.exit_code == 0 and r.output.strip().endswith("report.html")

    r = cli.invoke(app, ["review", "decide", "baseline-0001", "--accept", "--reject",
                         "--label", "other", *rd])
    assert r.exit_code == 2
    r = cli.invoke(app, ["review", "decide", "baseline-0001", "--reject", "--label", "nope", *rd])
    assert r.exit_code == 2 and "vocabulary" in r.output
    r = cli.invoke(app, ["review", "decide", "baseline-0001", "--accept", "--label",
                         "reference_wrong", "--note", "wing-only VLM", "--reviewer", "tester",
                         *rd, "--review-config", str(ROOT / "configs/review.yaml")])
    assert r.exit_code == 0 and "ACCEPTED" in r.output, r.output

    rdir = runs_dir / "baseline" / "baseline-0001"
    assert json.loads((rdir / "status.json").read_text())["status"] == "ACCEPTED"
    rep = (rdir / "report" / "report.html").read_text()
    assert "Human review" in rep and "reference_wrong" in rep and "wing-only VLM" in rep

    r = cli.invoke(app, ["review", "list", "baseline", *rd])
    assert "baseline-0001" not in r.output and "1 flagged" in r.output
    r = cli.invoke(app, ["review", "open", "nope", *rd])
    assert r.exit_code == 2
