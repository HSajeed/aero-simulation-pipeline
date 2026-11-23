"""Real OpenVSP + container. Skipped when either is unavailable."""
import json
import shutil
import subprocess

import pytest
import yaml

from aero import db
from aero.corrections import rules
from aero.geom.stage import GeomStage
from aero.runner import Runner, db_path, init_study
from aero.status import Status
from conftest import ROOT


def _available():
    try:
        import openvsp  # noqa: F401
    except Exception:
        return False
    from aero.foam.container import IMAGE
    return shutil.which("podman") is not None and subprocess.run(
        ["podman", "image", "exists", IMAGE]).returncode == 0


pytestmark = pytest.mark.skipif(not _available(), reason="needs openvsp + podman image")


def _one_run_study(tmp_path, study_cfg, params):
    cfg = study_cfg.model_copy(update={"name": "t",
                                       "sampling": study_cfg.sampling.model_copy(update={"n": 1})})
    init_study(cfg, tmp_path)
    rdir = tmp_path / "t" / "t-0001"
    frozen = yaml.safe_load((rdir / "config.yaml").read_text())
    frozen["params"] = params                     # pin the geometry for the test
    (rdir / "config.yaml").write_text(yaml.safe_dump(frozen))
    return Runner(tmp_path, gates_cfg_global, {"geom": GeomStage()}, log=lambda m: None,
                  corrections=rules.load(ROOT / "configs/corrections.yaml"))


gates_cfg_global = None


@pytest.fixture(autouse=True)
def _gates(gates_cfg):
    global gates_cfg_global
    gates_cfg_global = gates_cfg


GOOD = {"span": 10.0, "sweep_le": 5.0, "taper": 0.5, "twist_tip": 2.0, "fuselage_fineness": 8.0}


def test_good_geometry_passes(tmp_path, study_cfg):
    r = _one_run_study(tmp_path, study_cfg, GOOD)
    assert r.run_one("t-0001") == Status.GEOM_OK
    con = db.connect(db_path(tmp_path))
    g = con.execute("SELECT * FROM geometry").fetchone()
    assert g["sref_half"] == pytest.approx(10 * 2 * 1.5 / 4)
    data = json.loads(g["data"])
    assert data["vsp_wing_total_area"] == pytest.approx(g["sref_full"], rel=1e-6)
    assert data["stl_area"] == pytest.approx(data["compgeom_total_wet_area"], rel=1e-3)
    ref = con.execute("SELECT alpha, CL FROM reference_results ORDER BY alpha").fetchall()
    assert [row["alpha"] for row in ref] == [0.0, 4.0] and ref[1]["CL"] > ref[0]["CL"]
    assert (tmp_path / "t/t-0001/geom/log.surfaceCheck.a0").exists()


def test_bad_geometry_tiny_tip_unrecoverable(tmp_path, study_cfg):
    r = _one_run_study(tmp_path, study_cfg, {**GOOD, "taper": 0.05})   # tip chord 0.1 m
    assert r.run_one("t-0001") == Status.UNRECOVERABLE
    con = db.connect(db_path(tmp_path))
    row = db.get_run(con, "t-0001")
    assert row["failure_code"] == "FEATURE_TOO_SMALL"
    assert db.corrections_for(con, "t-0001") == []        # design is never "healed"
    params = yaml.safe_load((tmp_path / "t/t-0001/config.yaml").read_text())["params"]
    assert params["taper"] == 0.05


def test_mesh_stage_real(tmp_path, study_cfg):
    from aero.foam.mesh_stage import MeshStage
    r = _one_run_study(tmp_path, study_cfg, GOOD)
    r.impls["mesh"] = MeshStage()
    assert r.run_one("t-0001") == Status.MESH_OK
    con = db.connect(db_path(tmp_path))
    m = {row["metric"]: row["value"] for row in con.execute(
        "SELECT metric, value FROM gate_results WHERE gate='gate1'")}
    assert m["wetted_area_ratio"] >= 0.98 and m["cells_min"] >= 50000
    assert m["snapped_area_wing"] > 0 and m["snapped_area_fuselage"] > 0
    assert (tmp_path / "t/t-0001/mesh/log.checkMesh.a0").exists()
    assert (tmp_path / "t/t-0001/mesh/settings_used.a0.json").exists()


def test_broken_bc_caught_not_fixed(tmp_path, study_cfg, monkeypatch):
    """PLAN Phase 4 acceptance: a bad BC is caught and NOT auto-fixed."""
    import shutil as sh
    from aero.foam import case as case_mod, pilot_stage
    from aero.foam.mesh_stage import MeshStage
    tpl = tmp_path / "broken_templates"
    sh.copytree(case_mod.TEMPLATES / "solve", tpl)
    u = (tpl / "U.j2").read_text()
    (tpl / "U.j2").write_text("\n".join(l for l in u.splitlines() if "farfield" not in l))
    monkeypatch.setattr(pilot_stage, "TEMPLATES", tpl)
    runs = tmp_path / "runs"
    r = _one_run_study(runs, study_cfg, GOOD)
    r.impls.update(mesh=MeshStage(), pilot=pilot_stage.PilotStage())
    assert r.run_one("t-0001") == Status.UNRECOVERABLE
    con = db.connect(db_path(runs))
    assert db.get_run(con, "t-0001")["failure_code"] == "CASE_SETUP_ERROR"
    assert db.corrections_for(con, "t-0001", "gate2") == []          # nothing auto-changed
    assert "farfield" not in (runs / "t/t-0001/case/0/U").read_text()  # BC left as written
