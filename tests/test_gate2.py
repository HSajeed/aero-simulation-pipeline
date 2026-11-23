import math

import pytest

from aero.corrections import rules
from aero.gates.gate2 import evaluate, residual_drop
from conftest import ROOT

LOG = {"fatal": False, "fatal_io": False, "fpe": False, "nan_residual": False, "ended": True,
       "setup_error": False, "n_iterations": 300, "execution_time_s": 90.0, "last_time": 300.0}


def hist(n=300, cl_end=0.25, drop=3.0):
    rows = []
    for i in range(1, n + 1):
        r = 10 ** (-drop * (i - 1) / (n - 1))
        cl = cl_end * (1 - math.exp(-i / 30))
        rows.append({"iteration": i, "Ux": r, "Uy": r, "Uz": r, "p": r,
                     "Cl": cl, "Cd": 0.01, "Cm": 0.05})
    return rows


def data(**kw):
    d = {"logs": [dict(LOG)], "potential": dict(LOG), "hist": hist(),
         "expected_iterations": 300, "rc": 0}
    d.update(kw)
    return d


@pytest.fixture
def g2(gates_cfg):
    g = gates_cfg.model_dump(mode="json")["gate2"]
    return {**g, "cl_change_floor": 0.05, "max_abs_coeff": 5.0}   # test values


def test_healthy_pilot_passes(g2):
    g = evaluate(data(), g2)
    assert g.passed, [m for m in g.metrics if not m.passed and m.severity == "gate"]


def test_residual_drop_definition():
    h = hist(drop=3.0)
    assert residual_drop(h, "p") == pytest.approx(3.0 * (1 - 4.5 / 299), rel=0.02)
    assert residual_drop(h[:10], "p") is None                       # too short


@pytest.mark.parametrize("kw,code", [
    ({"logs": [{**LOG, "fatal": True, "fatal_io": True, "setup_error": True, "ended": False}],
      "rc": 1}, "CASE_SETUP_ERROR"),
    ({"potential": {**LOG, "ended": False, "fatal": True}, "rc": 1}, "POTENTIAL_INIT_FAILED"),
    ({"logs": [{**LOG, "fpe": True, "ended": False}], "rc": 136}, "SOLVER_CRASHED"),
    ({"logs": [{**LOG, "nan_residual": True}]}, "NAN_DETECTED"),
    ({"hist": hist()[:250]}, "MISSING_OUTPUT"),
    ({"hist": [{**h, "Cl": 50.0} for h in hist()]}, "FORCES_UNBOUNDED"),
    ({"hist": hist(drop=1.0)}, "RESIDUAL_DROP_LOW"),
])
def test_failure_codes(g2, kw, code):
    g = evaluate(data(**kw), g2)
    assert not g.passed and g.failure_code == code


def test_cl_not_settled(g2):
    h = hist()
    for i, r in enumerate(h):
        r["Cl"] = 0.25 * i / 300          # still climbing linearly
    assert evaluate(data(hist=h), g2).failure_code == "CL_NOT_SETTLED"


def test_cl_change_uses_floor_near_zero_cl(g2):
    h = hist(cl_end=0.0)
    for i, r in enumerate(h):
        r["Cl"] = -0.03 + (0.0005 if i == len(h) - 1 else 0.0)
    # |dCl| = 5e-4; relative to |Cl| 0.03 -> 1.7 %, floor irrelevant; passes
    assert evaluate(data(hist=h), g2).passed


def test_undecided_thresholds_block(gates_cfg):
    g = gates_cfg.model_dump(mode="json")["gate2"]
    g.update(cl_change_floor=None, max_abs_coeff=None)
    r = evaluate(data(), g)
    assert not r.passed and r.failure_code == "UNDECIDED_THRESHOLD"


def test_gate2_rules(study_cfg):
    r = rules.load(ROOT / "configs/corrections.yaml")
    s = study_cfg.model_dump(mode="json")
    steps = [rules.propose(r, "gate2", "NAN_DETECTED", s, k) for k in (1, 2, 3)]
    assert [c.param for c in steps] == ["solver.relax_U", "solver.upwind_startup_iters",
                                        "solver.n_non_orth_correctors"]
    assert steps[0].new == pytest.approx(0.49) and steps[1].new == 100
    assert rules.propose(r, "gate2", "CASE_SETUP_ERROR", s, 1) is None   # physics -> human
    for code in ("SOLVER_CRASHED", "RESIDUAL_DROP_LOW", "CL_NOT_SETTLED", "POTENTIAL_INIT_FAILED"):
        assert rules.propose(r, "gate2", code, s, 1).param.startswith("solver.")
