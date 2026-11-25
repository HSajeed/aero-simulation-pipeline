import math

import pytest

from aero.foam.prod_stage import window_stats
from aero.gates.gate3 import evaluate


def res(CL, CD=0.01, Cm=0.0, std=1e-5, mi=1e-9, conv=1):
    return {"CL": CL, "CD": CD, "Cm": Cm, "cl_std": std, "cd_std": 0.0, "cm_std": 0.0,
            "mass_imbalance": mi, "converged": conv}


GOOD = {0.0: res(-0.04), 4.0: res(0.27)}            # slope 4.44 / rad
REFS = {0.0: {"CL": -0.035, "Cm": -0.003}, 4.0: {"CL": 0.28, "Cm": 0.09}}


@pytest.fixture
def g3(gates_cfg):
    return gates_cfg.model_dump(mode="json")["gate3"]


def test_good_passes(g3):
    g = evaluate(GOOD, REFS, g3)
    assert g.passed, [m for m in g.metrics if not m.passed and m.severity == "gate"]


@pytest.mark.parametrize("change,code", [
    ({4.0: res(0.27, std=5e-3)}, "COEFF_NOT_CONVERGED"),
    ({4.0: res(0.27, mi=1e-2)}, "MASS_IMBALANCE"),
    ({4.0: res(0.40)}, "CL_VS_VSPAERO"),
    ({0.0: res(-0.04, CD=-0.001)}, "NEGATIVE_CD"),
    ({4.0: res(0.28), 0.0: res(0.23)}, "CL_ALPHA_SLOPE"),   # 0.72 / rad
])
def test_flags(g3, change, code):
    g = evaluate({**GOOD, **change}, REFS, g3)
    assert not g.passed and code in g.failure_code.split(",")


def test_all_reasons_listed(g3):
    bad = {0.0: res(-0.04, CD=-0.001, mi=1e-2), 4.0: res(0.27)}
    codes = evaluate(bad, REFS, g3).failure_code.split(",")
    assert {"NEGATIVE_CD", "MASS_IMBALANCE"} <= set(codes)


def test_floor_at_small_cl(g3):
    # |dCL| = 0.012 at alpha 0: 34 % relative, but below the 0.0125 floored limit
    near = {0.0: res(-0.0230), 4.0: res(0.27)}
    refs = {0.0: {"CL": -0.035, "Cm": 0}, 4.0: {"CL": 0.28, "Cm": 0}}
    assert evaluate(near, refs, g3).passed
    far = {0.0: res(-0.0200), 4.0: res(0.27)}          # |dCL| = 0.015 > 0.0125
    assert "CL_VS_VSPAERO" in evaluate(far, refs, g3).failure_code


def test_missing_reference_flags(g3):
    g = evaluate(GOOD, {}, g3)
    assert "NO_REFERENCE" in g.failure_code


def test_cm_never_flags(g3):
    weird = {0.0: res(-0.04, Cm=+0.5), 4.0: res(0.27, Cm=-0.5)}
    g = evaluate(weird, REFS, g3)
    assert g.passed
    assert {m.metric for m in g.metrics if m.severity == "warn"} >= {"cm_alpha_slope", "cm_a0"}


def test_undecided_threshold_flags(g3):
    g = evaluate(GOOD, REFS, {**g3, "cl_vspaero_floor": None})
    assert "UNDECIDED_THRESHOLD" in g.failure_code


def test_nan_result_flags(g3):
    g = evaluate({0.0: res(float("nan")), 4.0: res(0.27)}, REFS, g3)
    assert not g.passed


def test_window_stats():
    rows = [{"iteration": i, "Cl": 0.2 + (0.01 if i % 2 else -0.01), "Cd": 0.01, "Cm": 0.0}
            for i in range(1, 401)]
    st = window_stats(rows, 200)
    assert st["CL"] == pytest.approx(0.2) and st["cl_std"] == pytest.approx(0.01)
    rows[-1]["Cl"] = float("nan")
    assert window_stats(rows, 200)["CL"] is None


def test_mass_imbalance_from_function_objects(tmp_path):
    from aero.foam.prod_stage import mass_imbalance
    for name, col, val in (("farfieldFlux", "sum(phi)", "2.0e-3"),
                           ("farfieldFluxMag", "sumMag(phi)", "4.0e+1")):
        d = tmp_path / "postProcessing" / name / "300"
        d.mkdir(parents=True)
        (d / "surfaceFieldValue.dat").write_text(
            f"# Region type : patch farfield\n# Time\t{col}\n301\t{val}\n")
    assert mass_imbalance(tmp_path, 301) == pytest.approx(2e-3 / 20)
    assert mass_imbalance(tmp_path, 999) is None


def test_checks_stage_sampling_never_affects_gate(tmp_path, study_cfg, gates_cfg, monkeypatch):
    """Phase 8: automatic plane sampling is report-only; a crash in it is logged
    and the Gate 3 verdict is the same as without sampling."""
    from aero import db
    from aero.foam import sample
    from aero.gates.checks_stage import ChecksStage
    from aero.runner import db_path, init_study, load_context
    runs = tmp_path / "runs"
    init_study(study_cfg, runs)
    con = db.connect(db_path(runs))
    rid = "baseline-0001"
    for a, r in GOOD.items():
        db.put_result(con, rid, a, r)
    db.put_reference(con, rid, "vspaero", [{"alpha": a, **v} for a, v in REFS.items()])
    con.commit()

    def boom(*a, **k):
        raise RuntimeError("container gone")
    monkeypatch.setattr(sample, "sample_run", boom)
    verdicts = []
    for stage in (ChecksStage(sample=False), ChecksStage(sample=True)):
        ctx = load_context(con, runs, rid)
        ctx.con, ctx.gates = con, gates_cfg.model_dump(mode="json")
        g = stage.check(ctx, stage.run(ctx))
        verdicts.append((g.passed, g.failure_code, [(m.metric, m.value) for m in g.metrics]))
    assert verdicts[0] == verdicts[1] and verdicts[0][0]
    assert "container gone" in (ctx.dir / "case" / "log.sample_status").read_text()
