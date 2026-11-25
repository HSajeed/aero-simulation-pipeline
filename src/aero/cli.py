"""CLI entry point: `aero`."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

import typer
import yaml

from aero import db
from aero.config import StudyConfig, load_gates, load_review, load_study
from aero.runner import STAGE_NAMES, Runner, db_path, init_study

app = typer.Typer(no_args_is_help=True, add_completion=False)

RunsDir = typer.Option(Path("runs"), "--runs-dir", help="Runs root (DB at <runs-dir>/runs.db)")
GatesOpt = typer.Option(Path("configs/gates.yaml"), "--gates", help="Gates config")
CorrOpt = typer.Option(Path("configs/corrections.yaml"), "--corrections",
                       help="Correction rules")
ReviewOpt = typer.Option(Path("configs/review.yaml"), "--review-config", help="Review labels")

review_app = typer.Typer(no_args_is_help=True, help="Tier 3 human review of FLAGGED runs.")
app.add_typer(review_app, name="review")


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
        from aero.runner import RunLocked
        for rid in ids:
            try:
                runner.force(rid, stage)
            except RunLocked as e:
                typer.echo(str(e), err=True)
                raise typer.Exit(2)
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


# ------------------------------------------------------------------ reports
def _write_reports(con, runs_dir: Path, ids: list[str], gates: Path) -> list[Path]:
    from aero.report.html import write_report
    g = load_gates(gates).model_dump(mode="json")
    return [write_report(con, runs_dir, rid, g) for rid in ids]


@app.command()
def report(study: str, run_id: Optional[str] = typer.Option(None, "--run"),
           runs_dir: Path = RunsDir, gates: Path = GatesOpt):
    """Write the self-contained HTML report of each run (from the DB)."""
    con = db.connect(db_path(runs_dir))
    _study_cfg(con, study)
    ids = [run_id] if run_id else [r["id"] for r in db.list_runs(con, study)]
    for p in _write_reports(con, runs_dir, ids, gates):
        typer.echo(str(p))


@app.command()
def summary(study: str, reports: bool = typer.Option(False, "--reports",
                                                     help="also (re)write every run report"),
            runs_dir: Path = RunsDir, gates: Path = GatesOpt):
    """Write the study summary HTML (funnel, corrections, flags, reviews, wall time, results) from the DB."""
    from aero.report.summary import write_summary
    con = db.connect(db_path(runs_dir))
    _study_cfg(con, study)
    if reports:
        _write_reports(con, runs_dir, [r["id"] for r in db.list_runs(con, study)], gates)
    typer.echo(str(write_summary(con, runs_dir, study)))


# ------------------------------------------------------------------- review
@review_app.command("list")
def review_list(study: str, runs_dir: Path = RunsDir, gates: Path = GatesOpt):
    """FLAGGED runs of a study, most severe first (violations, check priority, exceedance)."""
    from aero.review import queue
    con = db.connect(db_path(runs_dir))
    _study_cfg(con, study)
    entries = queue.flagged(con, study, load_gates(gates).model_dump(mode="json")["gate3"])
    if not entries:
        typer.echo(f"no FLAGGED runs in '{study}'")
        return
    typer.echo(f"{'#':>2}  {'run':<16}{'viol':>4}  {'worst check':<20}{'exceed':>8}  reasons")
    for i, e in enumerate(entries, 1):
        ex = "-" if e.exceedance is None else ("inf" if e.exceedance == float("inf")
                                               else f"{e.exceedance:.2f}x")
        typer.echo(f"{i:>2}  {e.run_id:<16}{e.n_violations:>4}  {e.top_check:<20}{ex:>8}  "
                   f"{','.join(e.codes)}")
    typer.echo(f"{len(entries)} flagged")


@review_app.command("open")
def review_open(run_id: str, runs_dir: Path = RunsDir, gates: Path = GatesOpt):
    """(Re)write a run's report from the DB and print its path."""
    con = db.connect(db_path(runs_dir))
    try:
        db.get_run(con, run_id)
    except KeyError:
        typer.echo(f"unknown run: {run_id}", err=True)
        raise typer.Exit(2)
    typer.echo(str(_write_reports(con, runs_dir, [run_id], gates)[0]))


@review_app.command("decide")
def review_decide(run_id: str,
                  accept: bool = typer.Option(False, "--accept"),
                  reject: bool = typer.Option(False, "--reject"),
                  label: str = typer.Option(..., "--label", help="from configs/review.yaml"),
                  note: Optional[str] = typer.Option(None, "--note"),
                  reviewer: Optional[str] = typer.Option(None, "--reviewer",
                                                         help="default: login name"),
                  runs_dir: Path = RunsDir, gates: Path = GatesOpt,
                  review_config: Path = ReviewOpt):
    """Accept or reject a FLAGGED run with a label (stored in the reviews table)."""
    from aero.review import queue
    from aero.runner import run_dir, write_status_mirror
    if accept == reject:
        typer.echo("give exactly one of --accept / --reject", err=True)
        raise typer.Exit(2)
    con = db.connect(db_path(runs_dir))
    try:
        row = queue.decide(con, run_id, accept, label, note, load_review(review_config).labels,
                           reviewer)
    except KeyError:
        typer.echo(f"unknown run: {run_id}", err=True)
        raise typer.Exit(2)
    except queue.ReviewError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)
    write_status_mirror(run_dir(runs_dir, row["study"], run_id), row)
    path = _write_reports(con, runs_dir, [run_id], gates)[0]
    typer.echo(f"{run_id}: {row['status']} ({label}); report {path}")


@review_app.command("failures")
def review_failures(study: str, runs_dir: Path = RunsDir):
    """UNRECOVERABLE runs of a study and their failure label (if any)."""
    from aero.review import queue
    con = db.connect(db_path(runs_dir))
    _study_cfg(con, study)
    rows = queue.failures(con, study)
    for r in rows:
        typer.echo(f"{r['run_id']:<16}{r['failure_code'] or '-':<22}{r['label'] or 'UNLABELLED'}")
    n = sum(r["label"] is None for r in rows)
    typer.echo(f"{len(rows)} unrecoverable, {n} unlabelled")


@review_app.command("label")
def review_label(run_id: str,
                 label: str = typer.Option(..., "--label", help="failure_labels in configs/review.yaml"),
                 note: Optional[str] = typer.Option(None, "--note"),
                 reviewer: Optional[str] = typer.Option(None, "--reviewer"),
                 runs_dir: Path = RunsDir, gates: Path = GatesOpt,
                 review_config: Path = ReviewOpt):
    """Label why an UNRECOVERABLE run failed (status unchanged)."""
    from aero.review import queue
    con = db.connect(db_path(runs_dir))
    try:
        queue.label_failure(con, run_id, label, note, load_review(review_config).failure_labels,
                            reviewer)
    except KeyError:
        typer.echo(f"unknown run: {run_id}", err=True)
        raise typer.Exit(2)
    except queue.ReviewError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)
    path = _write_reports(con, runs_dir, [run_id], gates)[0]
    typer.echo(f"{run_id}: labelled {label} (status UNRECOVERABLE unchanged); report {path}")


@app.command()
def sample(study: str, run_id: Optional[str] = typer.Option(None, "--run"),
           force: bool = typer.Option(False, "--force"), runs_dir: Path = RunsDir):
    """Write report-only cutting-plane samples (U, p) for finished runs; no solve."""
    from aero.foam.sample import sample_run
    from aero.runner import run_dir
    con = db.connect(db_path(runs_dir))
    cfg = _study_cfg(con, study)
    ids = [run_id] if run_id else [r["id"] for r in db.list_runs(con, study)]
    for rid in ids:
        out = sample_run(con, run_dir(runs_dir, study, rid), rid, engine=cfg.container_engine,
                         image=cfg.image, force=force)
        typer.echo(f"{rid}  {out['status']}")
