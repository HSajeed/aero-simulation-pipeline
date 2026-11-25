import pytest
import yaml
from pydantic import ValidationError

from aero.config import StudyConfig, load_gates
from conftest import ROOT


def raw():
    return yaml.safe_load((ROOT / "configs/study_baseline.yaml").read_text())


def test_baseline_loads(study_cfg):
    assert study_cfg.sampling.n == 10 and len(study_cfg.alphas) == 2
    assert study_cfg.physics.model == "laminar" and study_cfg.physics.wall_bc == "slip"


@pytest.mark.parametrize("key,val", [("model", "kOmegaSST"), ("wall_bc", "noSlip"),
                                     ("flow", "compressible")])
def test_physics_cannot_change(key, val):
    d = raw(); d["physics"][key] = val
    with pytest.raises(ValidationError):
        StudyConfig.model_validate(d)


def test_bad_range_and_unknown_keys():
    d = raw(); d["parameters"][0]["low"] = 99
    with pytest.raises(ValidationError):
        StudyConfig.model_validate(d)
    d = raw(); d["turbulence"] = "SA"
    with pytest.raises(ValidationError):
        StudyConfig.model_validate(d)


def test_three_alphas_rejected():
    d = raw(); d["alphas"] = [0, 2, 4]
    with pytest.raises(ValidationError):
        StudyConfig.model_validate(d)


def test_retry_budget_capped(tmp_path):
    d = yaml.safe_load((ROOT / "configs/gates.yaml").read_text())
    d["max_correction_attempts"] = 4
    p = tmp_path / "g.yaml"; p.write_text(yaml.safe_dump(d))
    with pytest.raises(ValidationError):
        load_gates(p)


def _points_cfg(study_cfg, points, n=None):
    d = study_cfg.model_dump(mode="json")
    d["sampling"] = {"method": "points", "n": n or len(points), "points": points}
    return d


def test_points_sampling(study_cfg):
    from aero.runner import sample
    pt = {"span": 10.0, "sweep_le": 1.0, "taper": 0.6, "twist_tip": 1.8, "fuselage_fineness": 8.2}
    cfg = StudyConfig.model_validate(_points_cfg(study_cfg, [pt]))
    assert sample(cfg) == [pt]


@pytest.mark.parametrize("bad", [
    {"span": 10.0},                                                          # missing keys
    {"span": 30.0, "sweep_le": 1.0, "taper": 0.6, "twist_tip": 1.8, "fuselage_fineness": 8.2},
])
def test_points_sampling_rejects_bad_points(study_cfg, bad):
    with pytest.raises(ValidationError):
        StudyConfig.model_validate(_points_cfg(study_cfg, [bad]))


def test_points_sampling_n_must_match(study_cfg):
    pt = {"span": 10.0, "sweep_le": 1.0, "taper": 0.6, "twist_tip": 1.8, "fuselage_fineness": 8.2}
    with pytest.raises(ValidationError):
        StudyConfig.model_validate(_points_cfg(study_cfg, [pt], n=2))


def test_baseline_mesh_is_frozen_gci_medium():
    """Phase 7: the study config's mesh = the GCI medium level's mesh."""
    from aero.config import load_study
    base = load_study(ROOT / "configs/study_baseline.yaml").mesh
    med = load_study(ROOT / "configs/study_gci_medium.yaml").mesh
    assert base == med
