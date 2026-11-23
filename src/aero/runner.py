"""Study initialization and the stage runner.

Each stage: run(ctx) -> StageResult, check(ctx, result) -> GateResult,
correct(ctx, gate_result, attempt) -> Change | None.

Retries: a failed gate gets at most `max_correction_attempts` corrections;
then UNRECOVERABLE. Corrections may only touch NUMERICAL_SECTIONS. The
effective settings of a run = its frozen config.yaml + its corrections
replayed in order, so a run resumes correctly after a crash.
"""

from __future__ import annotations

import copy
import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np
import yaml
from scipy.stats import qmc

from aero import db
from aero.config import NUMERICAL_SECTIONS, GatesConfig, StudyConfig
from aero.status import TERMINAL, Status

SUBDIRS = ("geom", "mesh", "case", "report")


# ------------------------------------------------------------------ paths
def db_path(runs_dir: Path) -> Path:
    return Path(runs_dir) / "runs.db"


def run_dir(runs_dir: Path, study: str, run_id: str) -> Path:
    return Path(runs_dir) / study / run_id


def write_status_mirror(rdir: Path, row) -> None:
    """status.json is a write-only mirror of the DB row."""
    d = dict(row)
    d["params"] = json.loads(d["params"])
    (rdir / "status.json").write_text(json.dumps(d, indent=2) + "\n")


# --------------------------------------------------------------- sampling
def sample(cfg: StudyConfig) -> list[dict[str, float]]:
    """Latin hypercube over the parameter ranges, fixed seed."""
    lo = np.array([p.low for p in cfg.parameters])
    hi = np.array([p.high for p in cfg.parameters])
    unit = qmc.LatinHypercube(d=len(lo), seed=cfg.sampling.seed).random(cfg.sampling.n)
    pts = qmc.scale(unit, lo, hi)
    return [{p.name: float(v) for p, v in zip(cfg.parameters, row)} for row in pts]


def init_study(cfg: StudyConfig, runs_dir: Path) -> tuple[bool, list[str]]:
    """Create PENDING runs + run dirs. Idempotent: existing study -> no-op.
    Returns (created, run_ids)."""
    con = db.connect(db_path(runs_dir))
    if db.study_exists(con, cfg.name):
        return False, [r["id"] for r in db.list_runs(con, cfg.name)]
    resolved = cfg.model_dump(mode="json")
    runs = [(f"{cfg.name}-{i:04d}", i, p) for i, p in enumerate(sample(cfg), start=1)]
    db.create_study(con, cfg.name, yaml.safe_dump(resolved, sort_keys=False), runs)
    for rid, _, params in runs:
        rdir = run_dir(runs_dir, cfg.name, rid)
        for s in SUBDIRS:
            (rdir / s).mkdir(parents=True, exist_ok=True)
        frozen = {"run_id": rid, "params": params, "study": resolved}
        (rdir / "config.yaml").write_text(yaml.safe_dump(frozen, sort_keys=False))
        (rdir / "corrections.jsonl").touch()
        write_status_mirror(rdir, db.get_run(con, rid))
    return True, [r[0] for r in runs]


# ------------------------------------------------------------ stage protocol
@dataclass
class StageResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Metric:
    metric: str
    value: float | None
    threshold: float | None
    passed: bool


@dataclass
class GateResult:
    passed: bool
    failure_code: str | None = None
    metrics: list[Metric] = field(default_factory=list)


@dataclass
class Change:
    action: str
    param: str        # dotted path into the run settings, e.g. "mesh.surface_level"
    new: Any


@dataclass
class RunContext:
    run_id: str
    dir: Path
    params: dict[str, float]
    settings: dict[str, Any]     # resolved study config + replayed corrections
    attempt: int = 0


class Stage(Protocol):
    """Implementation of one pipeline stage. Status wiring lives in PIPELINE."""

    def run(self, ctx: RunContext) -> StageResult: ...
    def check(self, ctx: RunContext, result: StageResult) -> GateResult: ...
    def correct(self, ctx: RunContext, gate: GateResult, attempt: int) -> Change | None: ...


@dataclass(frozen=True)
class StageSpec:
    name: str
    gate: str
    requires: Status
    ok: Status
    fail: Status | None   # None: stage has no fail state
    retry: bool           # False: failure is final for the runner (checks -> FLAGGED)


PIPELINE: list[StageSpec] = [
    StageSpec("geom", "gate0", Status.PENDING, Status.GEOM_OK, Status.GEOM_FAIL, True),
    StageSpec("mesh", "gate1", Status.GEOM_OK, Status.MESH_OK, Status.MESH_FAIL, True),
    StageSpec("pilot", "gate2", Status.MESH_OK, Status.PILOT_OK, Status.PILOT_FAIL, True),
    StageSpec("prod", "prod", Status.PILOT_OK, Status.PROD_DONE, Status.PROD_FAIL, True),
    StageSpec("checks", "gate3", Status.PROD_DONE, Status.CHECKS_PASS, Status.FLAGGED, False),
]
STAGE_NAMES = [s.name for s in PIPELINE]


def spec_for(status: Status) -> StageSpec | None:
    """The stage that consumes `status` (a *_FAIL is consumed by its own stage's retry)."""
    for sp in PIPELINE:
        if status == sp.requires or (sp.retry and status == sp.fail):
            return sp
    return None


class PhysicsChangeError(RuntimeError):
    """A correction tried to touch a non-numerical setting."""


def get_path(d: dict, path: str) -> Any:
    for k in path.split("."):
        d = d[k]
    return d


def set_path(d: dict, path: str, value: Any) -> None:
    *head, last = path.split(".")
    for k in head:
        d = d[k]
    if last not in d:
        raise KeyError(path)
    d[last] = value


def check_numerical(param: str) -> None:
    if param.split(".")[0] not in NUMERICAL_SECTIONS:
        raise PhysicsChangeError(
            f"correction to '{param}' refused: only {NUMERICAL_SECTIONS} may be auto-corrected")


def load_context(con, runs_dir: Path, run_id: str) -> RunContext:
    row = db.get_run(con, run_id)
    rdir = run_dir(runs_dir, row["study"], run_id)
    frozen = yaml.safe_load((rdir / "config.yaml").read_text())
    settings = copy.deepcopy(frozen["study"])
    for c in db.corrections_for(con, run_id):
        set_path(settings, c["param"], json.loads(c["new"]))
    return RunContext(run_id, rdir, frozen["params"], settings)


# -------------------------------------------------------------------- runner
class Runner:
    def __init__(self, runs_dir: Path, gates: GatesConfig, impls: dict[str, Stage],
                 log: Callable[[str], None] = print):
        self.runs_dir = Path(runs_dir)
        self.gates = gates
        self.impls = impls        # stage name -> implementation; missing = not built yet
        self.log = log

    def _con(self):
        return db.connect(db_path(self.runs_dir))

    def _set(self, con, ctx: RunContext, status: Status, code: str | None = None):
        row = db.set_status(con, ctx.run_id, status, failure_code=code)
        write_status_mirror(ctx.dir, row)

    def force(self, run_id: str, stage: str) -> None:
        """Rewind a run to the input status of `stage` (explicit --force only)."""
        sp = next(s for s in PIPELINE if s.name == stage)
        con = self._con()
        row = db.force_reset(con, run_id, sp.requires)
        write_status_mirror(run_dir(self.runs_dir, row["study"], run_id), row)
        self.log(f"{run_id}: --force reset to {sp.requires}")

    def run_one(self, run_id: str, only: str | None = None) -> Status:
        """Advance a run until terminal / FLAGGED / an unbuilt stage, or after
        stage `only`. Finished stages are skipped, so reruns are no-ops."""
        con = self._con()
        while True:
            status = Status(db.get_run(con, run_id)["status"])
            sp = spec_for(status)
            if status in TERMINAL or sp is None:
                return status
            if only is not None and sp.name != only:
                self.log(f"{run_id}: at {status}, not the input of stage '{only}'; no-op")
                return status
            impl = self.impls.get(sp.name)
            if impl is None:
                self.log(f"{run_id}: at {status}; stage '{sp.name}' not implemented yet")
                return status
            if not self._run_stage(con, run_id, sp, impl) or only is not None:
                return Status(db.get_run(con, run_id)["status"])

    def _run_stage(self, con, run_id: str, sp: StageSpec, impl: Stage) -> bool:
        """Returns True if the run may continue to the next stage."""
        attempt = len(db.corrections_for(con, run_id, sp.gate))
        while True:
            ctx = load_context(con, self.runs_dir, run_id)
            ctx.attempt = attempt
            try:
                gate = impl.check(ctx, impl.run(ctx))
            except Exception as e:  # a stage crash is a gate failure, never a pass
                gate = GateResult(False, f"STAGE_EXCEPTION:{type(e).__name__}",
                                  [Metric("exception", None, None, False)])
                self.log(f"{run_id}: {sp.name} raised {e!r}")
            db.add_gate_results(con, run_id, sp.gate, attempt,
                                [{"metric": m.metric, "value": m.value,
                                  "threshold": m.threshold, "pass": m.passed}
                                 for m in gate.metrics])
            if gate.passed:
                self._set(con, ctx, sp.ok)
                self.log(f"{run_id}: {sp.gate} pass -> {sp.ok}")
                return True
            if sp.fail is None:
                self.log(f"{run_id}: {sp.name} failed ({gate.failure_code}); status kept")
                return False
            self._set(con, ctx, sp.fail, gate.failure_code)
            self.log(f"{run_id}: {sp.gate} fail ({gate.failure_code}) -> {sp.fail}")
            if not sp.retry:
                return False
            if attempt >= self.gates.max_correction_attempts:
                self._set(con, ctx, Status.UNRECOVERABLE, gate.failure_code)
                self.log(f"{run_id}: retry budget spent -> UNRECOVERABLE")
                return False
            change = impl.correct(ctx, gate, attempt + 1)
            if change is None:
                self._set(con, ctx, Status.UNRECOVERABLE, gate.failure_code)
                self.log(f"{run_id}: no correction for {gate.failure_code} -> UNRECOVERABLE")
                return False
            try:
                check_numerical(change.param)
            except PhysicsChangeError as e:   # a bug in a correction rule: stop, don't apply
                self._set(con, ctx, Status.UNRECOVERABLE, "PHYSICS_CHANGE_REFUSED")
                self.log(f"{run_id}: {e} -> UNRECOVERABLE")
                return False
            attempt += 1
            old = get_path(ctx.settings, change.param)
            self._log_correction(con, ctx, sp.gate, gate.failure_code or "", change, old, attempt)

    def _log_correction(self, con, ctx, gate, code, change: Change, old, attempt) -> None:
        db.add_correction(con, ctx.run_id, gate, code, change.action, change.param,
                          old, change.new, attempt)
        rec = {"time": db.now(), "gate": gate, "failure_code": code, "action": change.action,
               "param": change.param, "old": old, "new": change.new, "attempt": attempt}
        with (ctx.dir / "corrections.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        self.log(f"{ctx.run_id}: correction {attempt}: {change.param} {old} -> {change.new}")

    def run_study(self, study: str, workers: int, run_id: str | None = None,
                  only: str | None = None) -> dict[str, Status]:
        con = self._con()
        ids = [run_id] if run_id else [r["id"] for r in db.list_runs(con, study)]
        if workers <= 1 or len(ids) <= 1:
            return {i: self.run_one(i, only) for i in ids}
        with ProcessPoolExecutor(max_workers=workers) as pool:
            return dict(zip(ids, pool.map(self.run_one, ids, [only] * len(ids))))
