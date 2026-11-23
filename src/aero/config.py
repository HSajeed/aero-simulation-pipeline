"""Study and gates configuration: YAML validated by pydantic v2."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aero.foam.container import IMAGE

PARAM_NAMES = Literal["span", "sweep_le", "taper", "twist_tip", "fuselage_fineness"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Physics(_Strict):
    """Physics v1. Literal-typed: only the v1 values validate, so physics can't
    drift through config or auto-correction. Changing it is a human decision
    that requires a code change (Phase 11)."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    solver: Literal["simpleFoam"] = "simpleFoam"
    flow: Literal["incompressible_steady"] = "incompressible_steady"
    model: Literal["laminar"] = "laminar"
    wall_bc: Literal["slip"] = "slip"
    symmetry: Literal["half_model_y0"] = "half_model_y0"
    farfield: Literal["freestream"] = "freestream"


class Parameter(_Strict):
    name: PARAM_NAMES
    low: float
    high: float
    unit: str = ""

    @model_validator(mode="after")
    def _range(self):
        if not self.low < self.high:
            raise ValueError(f"{self.name}: low must be < high")
        return self


class Sampling(_Strict):
    method: Literal["lhs"] = "lhs"
    n: int = Field(gt=0)
    seed: int


class Freestream(_Strict):
    U_inf: float = Field(gt=0)        # m/s
    nu: float = Field(gt=0)           # m^2/s
    rho_inf: Literal[1.0] = 1.0       # forceCoeffs: rho rhoInf; rhoInf 1


class Geometry(_Strict):
    """Fixed (non-sampled) geometry values."""
    root_chord: float = Field(gt=0)
    fuselage_length: float = Field(gt=0)


class MeshSettings(_Strict):
    """Numerical: may be auto-corrected."""
    base_cell_size: float = Field(gt=0)
    surface_level: tuple[int, int]
    feature_level: int = Field(ge=0)
    near_body_level: int = Field(ge=0)
    n_cells_between_levels: int = Field(ge=1)


class SolverSettings(_Strict):
    """Numerical: may be auto-corrected."""
    pilot_iterations: int = Field(gt=0)
    max_iterations: int = Field(gt=0)
    residual_target: float = Field(gt=0)
    relax_U: float = Field(gt=0, le=1)
    relax_p: float = Field(gt=0, le=1)


class StudyConfig(_Strict):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    physics: Physics = Physics()
    parameters: list[Parameter]
    sampling: Sampling
    geometry: Geometry
    freestream: Freestream
    alphas: tuple[float, float]       # deg; second warm-started from first
    mesh: MeshSettings
    solver: SolverSettings
    workers: int = Field(ge=1)
    container_engine: Literal["podman", "docker"] = "podman"
    image: str = IMAGE

    @model_validator(mode="after")
    def _unique_params(self):
        names = [p.name for p in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("duplicate parameter names")
        return self


# Sections a correction may touch. Everything else is physics or design.
NUMERICAL_SECTIONS = ("mesh", "solver")


# ---------------------------------------------------------------- gates
# A null threshold means "not decided yet". Gate code must treat an undecided
# check as not passable (fail/flag), never as a pass.
class Gate0(_Strict):
    require_closed: bool = True
    max_self_intersections: int | None = None
    min_triangle_quality: float | None = None


class Gate1(_Strict):
    max_non_orth: float
    max_skewness: float
    max_negative_volumes: int = 0
    require_closed_boundary: bool = True
    min_cells: int
    max_cells: int
    min_wetted_area_ratio: float


class Gate2(_Strict):
    min_residual_drop_orders: float
    max_cl_change: float
    cl_change_window: int


class Gate3(_Strict):
    max_cl_rel_diff_vspaero: float
    max_mass_imbalance: float
    coeff_std_window: int | None = None
    max_coeff_std: float | None = None
    cl_alpha_slope_range: tuple[float, float] | None = None   # per rad
    min_cd: float | None = None


class GatesConfig(_Strict):
    max_correction_attempts: int = Field(ge=0, le=3)   # CLAUDE.md rule 3
    gate0: Gate0
    gate1: Gate1
    gate2: Gate2
    gate3: Gate3


def _load(path: Path | str) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def load_study(path: Path | str) -> StudyConfig:
    return StudyConfig.model_validate(_load(path))


def load_gates(path: Path | str) -> GatesConfig:
    return GatesConfig.model_validate(_load(path))
