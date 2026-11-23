"""CLI entry point: `aero`."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

import typer
import yaml

from aero import db
from aero.config import StudyConfig, load_gates, load_study
from aero.runner import STAGE_NAMES, Runner, db_path, init_study

app = typer.Typer(no_args_is_help=True, add_completion=False)

RunsDir = typer.Option(Path("runs"), "--runs-dir", help="Runs root (DB at <runs-dir>/runs.db)")
GatesOpt = typer.Option(Path("configs/gates.yaml"), "--gates", help="Gates config")
CorrOpt = typer.Option(Path("configs/corrections.yaml"), "--corrections",
                       help="Correction rules")


def impls() -> dict:
    """Stage implementations, registered as phases land."""
    from aero.foam.mesh_stage import MeshStage
    from aero.foam.pilot_stage import PilotStage
    from aero.foam.prod_stage import ProdStage
    from aero.gates.checks_stage import ChecksStage
    from aero.geom.stage import GeomStage
    return {"geom": GeomStage(), "mesh": MeshStage(), "pilot": PilotStage(),
            "prod": ProdStage(), "checks": ChecksStage()}


def _study_cfg(con, study: str) -> StudyConfig:
    row = con.execute("SELECT config_yaml FROM studies WHERE name=?", (study,)).fetchone()
    if row is None:
        typer.echo(f"unknown study: {study}", err=True)
        raise typer.Exit(2)
    return StudyConfig.model_validate(yaml.safe_load(row[0]))


@app.command("init-study")
def init_study_cmd(config: Path, runs_dir: Path = RunsDir):
    """Create PENDING runs for a study config (no-op if the study exists)."""
    cfg = load_study(config)
    created, ids = init_study(cfg, runs_dir)
    if created:
        typer.echo(f"study '{cfg.name}': created {len(ids)} PENDING runs in {runs_dir / cfg.name}")
    else:
        typer.echo(f"study '{cfg.name}' already exists ({len(ids)} runs); nothing changed")


@app.command()
def run(study: str, run_id: Optional[str] = typer.Option(None, "--run"),
        stage: Optional[str] = typer.Option(None, "--stage", help=f"one of {STAGE_NAMES}"),
        force: bool = typer.Option(False, "--force", help="rerun --stage on a run past it"),
        workers: Optional[int] = typer.Option(None, "--workers"),
        runs_dir: Path = RunsDir, gates: Path = GatesOpt, corrections: Path = CorrOpt):
    """Advance runs through the pipeline (idempotent, resumable)."""
    if stage is not None and stage not in STAGE_NAMES:
        typer.echo(f"--stage must be one of {STAGE_NAMES}", err=True)
        raise typer.Exit(2)
    if force and stage is None:
        typer.echo("--force requires --stage", err=True)
        raise typer.Exit(2)
    con = db.connect(db_path(runs_dir))
    cfg = _study_cfg(con, study)
    from aero.corrections import rules
    runner = Runner(runs_dir, load_gates(gates), impls(), corrections=rules.load(corrections))
    ids = [run_id] if run_id else [r["id"] for r in db.list_runs(con, study)]
    if force:
        for rid in ids:
            runner.force(rid, stage)
    out = runner.run_study(study, workers or cfg.workers, run_id, stage)
    for rid, st in out.items():
        typer.echo(f"{rid}  {st}")


@app.command()
def status(study: str, runs_dir: Path = RunsDir):
    """List runs of a study with status and parameters."""
    con = db.connect(db_path(runs_dir))
    _study_cfg(con, study)
    rows = db.list_runs(con, study)
    import json
    for r in rows:
        p = json.loads(r["params"])
        ps = " ".join(f"{k}={v:.3g}" for k, v in p.items())
        code = f" [{r['failure_code']}]" if r["failure_code"] else ""
        typer.echo(f"{r['id']}  {r['status']:<13}{code}  {ps}")
    counts = Counter(r["status"] for r in rows)
    typer.echo("total %d: %s" % (len(rows), ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))))


@app.command()
def doctor(engine: str = typer.Option("podman", "--engine"), runs_dir: Path = RunsDir):
    """Check the environment (container engine, image, OpenVSP, paths)."""
    from aero.doctor import run_checks
    rows = run_checks(engine=engine, runs_dir=runs_dir)
    for name, ok, detail in rows:
        typer.echo(f"[{'ok' if ok else 'FAIL':>4}] {name:<18} {detail}")
    if not all(ok for _, ok, _ in rows):
        raise typer.Exit(1)
