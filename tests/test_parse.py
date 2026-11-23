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
