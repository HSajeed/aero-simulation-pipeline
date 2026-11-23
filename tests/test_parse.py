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


def test_dat_files_phase0():
    from aero.foam.parse import parse_dat
    coef = parse_dat((FX / "phase0/coefficient.dat").read_text())
    assert [r["Time"] for r in coef] == [1.0, 2.0, 3.0]
    assert {"Cd", "Cl", "CmPitch"} <= set(coef[0]) and isinstance(coef[0]["Cl"], float)
    si = parse_dat((FX / "phase0/solverInfo.dat").read_text())
    assert si[0]["Ux_initial"] == 1.0 and si[0]["U_solver"] == "smoothSolver"
    assert si[1]["p_initial"] < 1


def test_read_function_object_restart(tmp_path):
    from aero.foam.parse import read_function_object
    hdr = "# Time\tCl\n"
    d0 = tmp_path / "postProcessing/fc/0"; d0.mkdir(parents=True)
    (d0 / "coefficient.dat").write_text(hdr + "1\t0.1\n2\t0.2\n3\t0.3\n")
    d2 = tmp_path / "postProcessing/fc/2"; d2.mkdir(parents=True)
    (d2 / "coefficient.dat").write_text(hdr + "3\t0.35\n4\t0.4\n")
    rows = read_function_object(tmp_path, "fc", "coefficient")
    assert [(r["Time"], r["Cl"]) for r in rows] == [(1, 0.1), (2, 0.2), (3, 0.35), (4, 0.4)]


def test_solver_log_markers():
    from aero.foam.parse import parse_solver_log
    ok = "Time = 1\n\nExecutionTime = 0.5 s\nTime = 2\n\nExecutionTime = 1.1 s\nEnd\n"
    r = parse_solver_log(ok)
    assert r["ended"] and r["n_iterations"] == 2 and r["execution_time_s"] == 1.1 and not r["fatal"]
    bad_bc = ("--> FOAM FATAL IO ERROR: (openfoam-2412)\n"
              "Cannot find patchField entry for farfield\n")
    assert parse_solver_log(bad_bc)["setup_error"]
    div = "Time = 7\nsmoothSolver:  Solving for Ux, Initial residual = nan, Final residual = nan\n"
    assert parse_solver_log(div)["nan_residual"] and not parse_solver_log(div)["setup_error"]


def test_simplefoam_log_real_ok():
    from aero.foam.parse import parse_solver_log
    r = parse_solver_log((FX / "phase4/log.simpleFoam_ok").read_text())
    assert not r["fpe"] and not r["fatal"] and r["ended"] and r["n_iterations"] == 300
    assert r["execution_time_s"] > 0


def test_fpe_stack_detected():
    from aero.foam.parse import parse_solver_log
    crash = ("trapFpe: Floating point exception trapping enabled (FOAM_SIGFPE).\nTime = 5\n"
             "#0  Foam::error::printStack(Foam::Ostream&) at ??:?\n"
             "#1  Foam::sigFpe::sigHandler(int) at ??:?\n")
    assert parse_solver_log(crash)["fpe"]


def test_potentialfoam_logs():
    from aero.foam.parse import parse_solver_log
    bad = parse_solver_log((FX / "phase4/log.potentialFoam_missing_div").read_text())
    assert bad["fatal_io"] and bad["setup_error"] and not bad["ended"]
    ok = parse_solver_log((FX / "phase4/log.potentialFoam_ok").read_text())
    assert ok["ended"] and not ok["fatal"] and not ok["fpe"]


def test_pilot_history_from_real_postprocessing(tmp_path):
    import shutil
    from aero.foam.pilot_stage import history
    shutil.copytree(FX / "phase4/postProcessing_pilot", tmp_path / "postProcessing")
    h = history(tmp_path)
    assert len(h) == 300 and h[0]["iteration"] == 1 and h[-1]["iteration"] == 300
    assert all(isinstance(h[-1][k], float) for k in ("Ux", "Uy", "Uz", "p", "Cl", "Cd", "Cm"))
