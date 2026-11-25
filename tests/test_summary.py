"""Phase 8: study summary from a synthetic DB (tmp_path only, never runs/)."""

from typer.testing import CliRunner

from aero import db
from aero.cli import app
from aero.report import summary
from aero.status import Status
from test_review import _full_run, study  # noqa: F401  (fixture)

cli = CliRunner()


def _gate(con, rid, gate, attempt, ok, extra=()):
    db.add_gate_results(con, rid, gate, attempt,
                        [{"metric": "x", "value": 1.0, "threshold": 1.0, "pass": ok}, *extra])


def _setup(con):
    """0001 flagged+rejected (1 gate1 correction, recovered), 0002 CHECKS_PASS,
    0003 UNRECOVERABLE at gate1 after a correction (labelled), 0004 GEOM_FAIL
    UNRECOVERABLE, rest PENDING."""
    _full_run(con, "baseline-0001")
    for g in ("gate0", "gate1", "gate2", "prod"):
        _gate(con, "baseline-0001", g, 0, True)
    con.execute("UPDATE runs SET status=? WHERE id=?", (Status.REJECTED, "baseline-0001"))
    con.execute("INSERT INTO reviews (run, decision, label, note, reviewer, time) VALUES "
                "('baseline-0001','reject','separated_flow',NULL,'t','2026-01-01')")
    con.execute("UPDATE runs SET status=? WHERE id='baseline-0002'", (Status.CHECKS_PASS,))
    for a, cl in ((0.0, 0.01), (4.0, 0.3)):
        db.put_result(con, "baseline-0002", a, {"CL": cl, "CD": 0.01, "wall_time": 60.0})
    _gate(con, "baseline-0002", "gate1", 0, True,
          [{"metric": "mesh_wall_time_s", "value": 30.0, "threshold": None, "pass": True}])
    _gate(con, "baseline-0003", "gate0", 0, True)
    _gate(con, "baseline-0003", "gate1", 0, False)
    _gate(con, "baseline-0003", "gate1", 1, False)
    db.add_correction(con, "baseline-0003", "gate1", "SKEWNESS_HIGH", "set", "mesh.max_boundary_skewness",
                      4.0, 3.0, 1)
    con.execute("UPDATE runs SET status=?, failure_code='SKEWNESS_HIGH' WHERE id='baseline-0003'",
                (Status.UNRECOVERABLE,))
    con.execute("INSERT INTO reviews (run, decision, label, note, reviewer, time) VALUES "
                "('baseline-0003','label','mesher_limitation',NULL,'t','2026-01-01')")
    _gate(con, "baseline-0004", "gate0", 0, False)
    con.execute("UPDATE runs SET status=?, failure_code='FEATURE_TOO_SMALL' WHERE id='baseline-0004'",
                (Status.UNRECOVERABLE,))
    con.commit()


def test_summary_numbers(study):
    runs_dir, con = study
    _setup(con)
    d = summary.collect(con, "baseline")
    passed = [f["passed"] for f in d["funnel"]]
    assert d["n"] == 10 and passed == [3, 2, 2, 2, 1]     # 0001, 0002, 0003 | 0003 stops at gate1
    assert [f["corrected"] for f in d["funnel"]][1] == 1  # 0001 passed gate1 after a correction
    assert d["funnel"][0]["stopped"] == 1 and d["funnel"][1]["stopped"] == 1
    assert d["flagged"] == 1 and d["reached_g3"] == 2
    assert d["decisions"] == {"reject": 1} and d["review_labels"] == {"separated_flow": 1}
    assert d["failure_labels"] == {"mesher_limitation": 1, "unlabelled": 1}
    assert d["fail_codes"] == {"SKEWNESS_HIGH": 1, "FEATURE_TOO_SMALL": 1}
    by = {(c["code"], c["param"]): c for c in d["corrections"]}
    assert by[("SKEWNESS_HIGH", "mesh.n_smooth_patch")]["recovered"] == 1
    assert by[("SKEWNESS_HIGH", "mesh.max_boundary_skewness")]["recovered"] == 0
    assert d["wall"]["mesh"] == 30.0 and d["wall"]["prod"] == 2 * 100.0 + 2 * 60.0


def test_summary_cli_writes_html(study):
    runs_dir, con = study
    _setup(con)
    r = cli.invoke(app, ["summary", "baseline", "--runs-dir", str(runs_dir)])
    assert r.exit_code == 0, r.output
    html = (runs_dir / "baseline" / "summary.html").read_text()
    assert "Funnel" in html and "data:image/png;base64," in html
    assert "SKEWNESS_HIGH" in html and "mesher_limitation" in html and "separated_flow" in html
    assert "baseline-0010" in html


def test_summary_empty_study(study):
    runs_dir, con = study                                  # all PENDING, nothing to plot
    html = summary.render(con, "baseline", runs_dir)
    assert "Study baseline" in html and "none" in html
