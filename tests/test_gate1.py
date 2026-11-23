import pytest

from aero.corrections import rules
from aero.foam.case import mesh_layout
from aero.gates.gate1 import evaluate
from conftest import ROOT

SHM = {"finished": True, "fatal": False, "snapped_cells": 80000, "mesh_time_s": 15.0}
CM = {"cells": 80000, "max_non_orth": 47.0, "max_skewness": 3.4, "max_aspect_ratio": 6.0,
      "negative_volumes": 0, "open_boundary": False, "open_cells": False, "mesh_ok": True}


@pytest.fixture
def g1(gates_cfg):
    return gates_cfg.model_dump(mode="json")["gate1"]


def test_good_mesh_passes(g1):
    g = evaluate(SHM, CM, 0.998, g1)
    assert g.passed, [m for m in g.metrics if not m.passed]


@pytest.mark.parametrize("shm,cm,ratio,code", [
    ({"finished": False}, {}, 0.998, "SHM_FAILED"),
    ({}, {"cells": None}, 0.998, "CHECKMESH_FAILED"),
    ({}, {"negative_volumes": 3}, 0.998, "NEGATIVE_VOLUMES"),
    ({}, {"negative_volumes": None}, 0.998, "NEGATIVE_VOLUMES"),
    ({}, {"open_boundary": True}, 0.998, "OPEN_BOUNDARY"),
    ({}, {"cells": 2_000_000}, 0.998, "TOO_MANY_CELLS"),
    ({}, {"max_non_orth": 75.0}, 0.998, "NON_ORTH_HIGH"),
    ({}, {"max_skewness": 6.9}, 0.998, "SKEWNESS_HIGH"),
    ({}, {}, 0.95, "AREA_RATIO_LOW"),
    ({}, {}, None, "AREA_RATIO_LOW"),
    ({}, {"cells": 10_000}, 0.998, "TOO_FEW_CELLS"),
])
def test_failure_codes(g1, shm, cm, ratio, code):
    g = evaluate({**SHM, **shm}, {**CM, **cm}, ratio, g1)
    assert not g.passed and g.failure_code == code


def test_priority_budget_before_area(g1):
    # over budget AND poor snapping: must not pick a refinement fix
    g = evaluate(SHM, {**CM, "cells": 2_000_000}, 0.9, g1)
    assert g.failure_code == "TOO_MANY_CELLS"


def test_rule_sequences(study_cfg):
    r = rules.load(ROOT / "configs/corrections.yaml")
    s = study_cfg.model_dump(mode="json")
    steps = [rules.propose(r, "gate1", "TOO_FEW_CELLS", s, k) for k in (1, 2, 3, 4)]
    assert [c.param for c in steps] == ["mesh.near_body_level", "mesh.wake_level",
                                        "mesh.base_cell_size", "mesh.base_cell_size"]
    assert steps[0].new == s["mesh"]["near_body_level"] + 1 and steps[2].new == 2.0
    area = rules.propose(r, "gate1", "AREA_RATIO_LOW", s, 3)
    assert area.param == "mesh.surface_level" and area.new == [6, 7]
    assert rules.propose(r, "gate1", "SHM_FAILED", s, 1) is None
    for code in ("TOO_MANY_CELLS", "TOO_FEW_CELLS", "AREA_RATIO_LOW", "NON_ORTH_HIGH"):
        for k in (1, 2, 3):
            assert rules.propose(r, "gate1", code, s, k).param.startswith("mesh.")


def test_rule_floor_returns_none(study_cfg):
    s = study_cfg.model_dump(mode="json")
    s["mesh"]["wake_level"] = 0
    r = {"gate1": {"X": {"action": "add", "param": "mesh.wake_level", "by": -1}}}
    assert rules.propose(r, "gate1", "X", s) is None     # can't go below 0 -> no change


GEOM = {"stl_bbox": [[0.0, -5.0, -0.7], [10.0, 5.0, 0.7]], "root_chord": 2.0,
        "fuselage_diameter": 1.4, "x_le_root": 3.5, "span": 10.0}


def test_mesh_layout(study_cfg):
    m = study_cfg.model_dump(mode="json")["mesh"]
    L = mesh_layout(GEOM, m, [0.0, 4.0])
    h, c = m["base_cell_size"], 2.0
    assert L["x0"] <= 0 - 15 * c and L["x1"] >= 10 + 25 * c          # PLAN extents
    assert L["y0"] == 0.0 and L["y1"] >= 5 + 15 * c
    for a, n in (("x", "nx"), ("y", "ny"), ("z", "nz")):
        assert L[f"{a}1"] - L[f"{a}0"] == pytest.approx(L[n] * h)       # whole cells
    lx, ly, lz = L["loc"]
    assert L["x0"] < lx < L["x1"] and L["y0"] < ly < L["y1"] and L["z0"] < lz < L["z1"]
    assert not (L["nb"]["min"][0] < lx < L["nb"]["max"][0])            # far from the body
    assert L["wk"]["max"][2] > L["wk"]["min"][2] + c                    # opened by alpha
