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
