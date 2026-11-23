import math

import pytest

from aero.corrections import rules
from aero.gates.gate0 import evaluate
from aero.geom.openvsp_build import planform
from aero.geom.vspaero import parse_polar
from conftest import ROOT

GOOD = {"finished": True, "closed": True, "illegal_triangles": 0, "self_intersections": 15,
        "min_quality": 1e-5, "n_parts": 1, "n_zones": 1, "n_triangles": 5000}
REF = {"tip_chord": 0.5}


@pytest.fixture
def g0(gates_cfg):
    return gates_cfg.model_dump(mode="json")["gate0"]


@pytest.fixture
def settings(study_cfg):
    return study_cfg.model_dump(mode="json")


def test_good_passes_with_warnings(g0, settings):
    g = evaluate(GOOD, REF, settings, g0)
    assert g.passed and g.failure_code is None
    warn = {m.metric for m in g.metrics if m.severity == "warn"}
    assert {"self_intersections", "min_triangle_quality"} <= warn


@pytest.mark.parametrize("change,code", [
    ({"closed": False}, "NOT_CLOSED"), ({"closed": None}, "NOT_CLOSED"),
    ({"illegal_triangles": 2}, "ILLEGAL_TRIANGLES"), ({"illegal_triangles": None}, "ILLEGAL_TRIANGLES"),
    ({"finished": False}, "SURFACECHECK_FAILED")])
def test_failures(g0, settings, change, code):
    g = evaluate({**GOOD, **change}, REF, settings, g0)
    assert not g.passed and g.failure_code == code


def test_feature_too_small(g0, settings):
    g = evaluate(GOOD, {"tip_chord": 0.1}, settings, g0)   # 2.56 cells < 8
    assert g.failure_code == "FEATURE_TOO_SMALL"


def test_undecided_threshold_blocks(g0, settings):
    g = evaluate(GOOD, REF, settings, {**g0, "max_self_intersections": None})
    assert not g.passed and g.failure_code == "UNDECIDED_THRESHOLD"


def test_numeric_self_intersection_threshold(g0, settings):
    assert evaluate(GOOD, REF, settings, {**g0, "max_self_intersections": 0}).failure_code \
        == "SELF_INTERSECTING"


def test_correction_rules(settings):
    r = rules.load(ROOT / "configs/corrections.yaml")
    ch = rules.propose(r, "gate0", "NOT_CLOSED", settings)
    assert ch.param == "tessellation"
    assert ch.new["wing_tess_w"] == round(settings["tessellation"]["wing_tess_w"] * 1.5)
    assert rules.propose(r, "gate0", "FEATURE_TOO_SMALL", settings) is None


def test_planform_reference_values():
    ref = planform({"span": 10, "taper": 0.5, "sweep_le": 0, "fuselage_fineness": 8}, 2.0, 10.0)
    assert ref["sref_full"] == pytest.approx(15.0) and ref["sref_half"] == pytest.approx(7.5)
    assert ref["mac"] == pytest.approx(2 / 3 * 2 * 1.75 / 1.5)
    assert ref["x_ref"] == pytest.approx(3.5 + 0.25 * ref["mac"])   # no sweep
    assert ref["fuselage_diameter"] == pytest.approx(1.25)


def test_polar_parse():
    rows = parse_polar((ROOT / "tests/fixtures/phase2/model.polar").read_text())
    assert [r["alpha"] for r in rows] == [0.0, 4.0]
    slope = (rows[1]["CL"] - rows[0]["CL"]) / math.radians(4)
    assert 3.0 < slope < 6.0 and rows[1]["CDi"] > 0


def test_polar_nan_is_failure():
    with pytest.raises(ValueError, match="non-finite"):
        parse_polar((ROOT / "tests/fixtures/phase2/model_nan.polar").read_text())


def test_vspaero_panel_errors_rejected():
    from aero.geom.vspaero import check_log
    text = (ROOT / "tests/fixtures/phase2/log.vspaero_panel_errors_head").read_text()
    with pytest.raises(ValueError, match="geometry error"):
        check_log(text)
    check_log("Done\n")   # clean log passes
