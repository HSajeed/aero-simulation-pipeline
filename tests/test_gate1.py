import math
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
    assert steps[0].new == s["mesh"]["near_body_level"] + 1 and steps[2].new == round(s["mesh"]["base_cell_size"] * 0.8, 6)
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
    m["domain_cell_size"] = None                     # extents in whole base cells
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


def test_mesh_layout_default_unchanged(study_cfg):
    """Field defaults (frozen configs from before Phase 7) -> Phase 3-6 behaviour."""
    from aero.config import MeshSettings
    raw = study_cfg.model_dump(mode="json")["mesh"]
    for k in ("domain_cell_size", "max_boundary_skewness"):
        raw.pop(k)
    m = MeshSettings.model_validate(raw).model_dump(mode="json")
    assert m["domain_cell_size"] is None and m["max_boundary_skewness"] == 20.0
    L = mesh_layout(GEOM, m, [0.0, 4.0])
    h = m["base_cell_size"]
    assert L["nx"] == math.ceil((10 + 25 * 2.0 + 15 * 2.0) / h)


def test_mesh_layout_shared_domain(study_cfg):
    """Phase 7 GCI: same domain for every base size when domain_cell_size is set."""
    m = study_cfg.model_dump(mode="json")["mesh"]
    m["domain_cell_size"] = 2.5
    layouts = []
    for h in (2.5, 2.5 / math.sqrt(2), 1.25):
        m["base_cell_size"] = h
        layouts.append(mesh_layout(GEOM, m, [0.0, 4.0]))
    for k in ("x0", "x1", "y0", "y1", "z0", "z1"):
        assert len({L[k] for L in layouts}) == 1
    coarse, med, fine = layouts
    assert fine["nx"] == 2 * coarse["nx"]
    assert abs(med["nx"] / coarse["nx"] - math.sqrt(2)) < 1 / coarse["nx"]


def test_skewness_sequence_p63(study_cfg):
    """P6-3: SKEWNESS_HIGH tightens sHM's boundary skewness first, then smooths."""
    r = rules.load(ROOT / "configs/corrections.yaml")
    s = study_cfg.model_dump(mode="json")
    c1 = rules.propose(r, "gate1", "SKEWNESS_HIGH", s, 1)
    assert (c1.param, c1.new) == ("mesh.max_boundary_skewness", 3.0)
    c2 = rules.propose(r, "gate1", "SKEWNESS_HIGH", s, 2)
    assert (c2.param, c2.new) == ("mesh.n_smooth_patch", s["mesh"]["n_smooth_patch"] + 2)


def test_snappy_dict_renders_boundary_skewness(study_cfg, tmp_path):
    from aero.foam.case import write_mesh_case
    stl = tmp_path / "b.stl"
    stl.write_text("solid wing\nendsolid wing\n")
    m = study_cfg.model_dump(mode="json")["mesh"]
    m["max_boundary_skewness"] = 3.0
    write_mesh_case(tmp_path, stl, GEOM, m, [0.0, 4.0], "a0")
    txt = (tmp_path / "system/snappyHexMeshDict").read_text()
    assert "maxBoundarySkewness 3.0;" in txt
    assert txt.index("meshQualityDict.cfg") < txt.index("maxBoundarySkewness")   # override after include
