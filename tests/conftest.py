from pathlib import Path

import pytest

from aero.config import load_gates, load_study

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def study_cfg():
    return load_study(ROOT / "configs/study_baseline.yaml")


@pytest.fixture
def gates_cfg():
    return load_gates(ROOT / "configs/gates.yaml")
