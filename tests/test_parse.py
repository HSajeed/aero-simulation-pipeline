from aero.foam.parse import parse_surface_check
from conftest import ROOT

FX = ROOT / "tests/fixtures"


def test_surface_check_good():
    sc = parse_surface_check((FX / "phase2/log.surfaceCheck_good").read_text())
    assert sc["closed"] is True and sc["illegal_triangles"] == 0 and sc["finished"]
    assert sc["n_triangles"] == 5176 and sc["n_parts"] == 1 and sc["n_zones"] == 1
    assert sc["self_intersections"] == 15 and 0 < sc["min_quality"] < 1e-4


def test_surface_check_phase0():
    sc = parse_surface_check((FX / "phase0/log.surfaceCheck").read_text())
    assert sc["closed"] is True and sc["self_intersections"] == 23 and sc["n_triangles"] == 4848


def test_surface_check_open_bad():
    sc = parse_surface_check((FX / "phase2/log.surfaceCheck_open").read_text())
    assert sc["closed"] is False and sc["illegal_triangles"] == 1
    assert sc["n_parts"] == 2 and sc["min_quality"] == 0 and sc["self_intersections"] == 14


def test_surface_check_garbage_is_none():
    sc = parse_surface_check("Segmentation fault\n")
    assert sc["closed"] is None and sc["illegal_triangles"] is None and not sc["finished"]
    assert sc["self_intersections"] is None


def test_check_mesh_ok():
    from aero.foam.parse import parse_check_mesh
    cm = parse_check_mesh((FX / "phase3/log.checkMesh_ok").read_text())
    assert cm["cells"] == 78527 and cm["mesh_ok"] is True and cm["failed_checks"] == 0
    assert cm["negative_volumes"] == 0 and cm["open_boundary"] is False and not cm["open_cells"]
    assert abs(cm["max_non_orth"] - 47.319) < 1e-3 and abs(cm["max_skewness"] - 3.4225) < 1e-3
    assert set(cm["patches"]) == {"symmetry", "farfield", "wing", "fuselage"}


def test_check_mesh_skew_fail():
    from aero.foam.parse import parse_check_mesh
    cm = parse_check_mesh((FX / "phase3/log.checkMesh_skew").read_text())
    assert cm["mesh_ok"] is False and cm["failed_checks"] >= 1
    assert abs(cm["max_skewness"] - 6.88672516) < 1e-6 and cm["cells"] == 269410


def test_check_mesh_phase0_and_garbage():
    from aero.foam.parse import parse_check_mesh
    assert parse_check_mesh((FX / "phase0/log.checkMesh").read_text())["cells"] == 114674
    cm = parse_check_mesh("FOAM FATAL ERROR\n")
    assert cm["cells"] is None and cm["negative_volumes"] is None and cm["open_boundary"] is None


def test_snappy_log():
    from aero.foam.parse import parse_snappy
    s = parse_snappy((FX / "phase3/log.snappyHexMesh_ok").read_text())
    assert s["finished"] and not s["fatal"] and s["snapped_cells"] == 78527
    assert s["mesh_time_s"] > 0
    assert parse_snappy("--> FOAM FATAL ERROR: boom\n") == {
        "finished": False, "snapped_cells": None, "mesh_time_s": None, "fatal": True}
