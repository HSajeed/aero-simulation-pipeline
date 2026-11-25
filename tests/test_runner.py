import json

import pytest

from aero import db
from aero.runner import (Change, GateResult, Metric, Runner, StageResult, db_path,
                         init_study, load_context, sample)
from aero.status import Status


class Fake:
    """Passes when settings[mesh][surface_level][0] >= need; corrects by +1."""

    def __init__(self, need=0, param="mesh.surface_level", correct=True):
        self.need, self.param, self.do_correct, self.calls = need, param, correct, 0

    def run(self, ctx):
        self.calls += 1
        return StageResult(True, {"level": ctx.settings["mesh"]["surface_level"][0]})

    def check(self, ctx, res):
        ok = res.data["level"] >= self.need
        return GateResult(ok, None if ok else "LOW_LEVEL",
                          [Metric("level", res.data["level"], self.need, ok)])

    def correct(self, ctx, gate, attempt):
        if not self.do_correct:
            return None
        lv = ctx.settings["mesh"]["surface_level"]
        new = [lv[0] + 1, lv[1] + 1] if self.param == "mesh.surface_level" else 99.0
        return Change("raise surface level", self.param, new)


def quiet(msg):
    pass


class Passing:
    def run(self, ctx): return StageResult(True)
    def check(self, ctx, res): return GateResult(True, None, [Metric("ok", 1, 1, True)])
    def correct(self, ctx, g, a): return None


@pytest.fixture
def study(tmp_path, study_cfg):
    init_study(study_cfg, tmp_path)
    return tmp_path, study_cfg


def status_of(runs, rid):
    return db.get_run(db.connect(db_path(runs)), rid)["status"]


def test_init_creates_pending_runs_and_dirs(study):
    runs, cfg = study
    rows = db.list_runs(db.connect(db_path(runs)), cfg.name)
    assert len(rows) == 10 and {r["status"] for r in rows} == {"PENDING"}
    d = runs / cfg.name / rows[0]["id"]
    for sub in ("geom", "mesh", "case", "report", "status.json", "corrections.jsonl", "config.yaml"):
        assert (d / sub).exists()
    assert json.loads((d / "status.json").read_text())["status"] == "PENDING"
    created, _ = init_study(cfg, runs)               # idempotent
    assert not created and len(db.list_runs(db.connect(db_path(runs)), cfg.name)) == 10


def test_sampling_reproducible_and_in_bounds(study_cfg):
    a, b = sample(study_cfg), sample(study_cfg)
    assert a == b and len(a) == 10
    for p in study_cfg.parameters:
        vals = [s[p.name] for s in a]
        assert all(p.low <= v <= p.high for v in vals)
        assert len({int((v - p.low) / (p.high - p.low) * 10) for v in vals}) == 10  # LHS strata


def test_pass_through_and_stop_at_unbuilt_stage(study, gates_cfg):
    runs, cfg = study
    r = Runner(runs, gates_cfg, {"geom": Passing(), "mesh": Passing()}, log=lambda m: None)
    assert r.run_one("baseline-0001") == Status.MESH_OK   # pilot not implemented


def test_retry_then_pass_logs_corrections(study, gates_cfg):
    runs, cfg = study
    need = cfg.mesh.surface_level[0] + 2
    r = Runner(runs, gates_cfg, {"geom": Fake(need)}, log=lambda m: None)
    assert r.run_one("baseline-0001") == Status.GEOM_OK
    con = db.connect(db_path(runs))
    corr = db.corrections_for(con, "baseline-0001")
    assert [c["attempt"] for c in corr] == [1, 2] and corr[0]["code"] == "LOW_LEVEL"
    lines = [json.loads(l) for l in
             (runs / "baseline" / "baseline-0001" / "corrections.jsonl").read_text().splitlines()]
    assert lines[1]["old"] == [need - 1, need] and lines[1]["new"] == [need, need + 1]
    assert load_context(con, runs, "baseline-0001").settings["mesh"]["surface_level"] == [need, need + 1]


def test_budget_exhausted_unrecoverable(study, gates_cfg):
    runs, cfg = study
    fake = Fake(need=100)
    r = Runner(runs, gates_cfg, {"geom": fake}, log=lambda m: None)
    assert r.run_one("baseline-0001") == Status.UNRECOVERABLE
    assert fake.calls == 4                               # 1 + 3 corrections, then stop
    assert len(db.corrections_for(db.connect(db_path(runs)), "baseline-0001")) == 3


def test_no_correction_available(study, gates_cfg):
    runs, _ = study
    r = Runner(runs, gates_cfg, {"geom": Fake(100, correct=False)}, log=lambda m: None)
    assert r.run_one("baseline-0001") == Status.UNRECOVERABLE


@pytest.mark.parametrize("param", ["freestream.U_inf", "physics.model", "alphas"])
def test_physics_correction_refused(study, gates_cfg, param):
    runs, _ = study
    r = Runner(runs, gates_cfg, {"geom": Fake(100, param=param)}, log=lambda m: None)
    assert r.run_one("baseline-0001") == Status.UNRECOVERABLE
    con = db.connect(db_path(runs))
    assert db.get_run(con, "baseline-0001")["failure_code"] == "PHYSICS_CHANGE_REFUSED"
    assert db.corrections_for(con, "baseline-0001") == []


def test_exception_is_failure_not_pass(study, gates_cfg):
    class Boom(Passing):
        def run(self, ctx): raise RuntimeError("container died")
    runs, _ = study
    r = Runner(runs, gates_cfg, {"geom": Boom()}, log=lambda m: None)
    assert r.run_one("baseline-0001") == Status.UNRECOVERABLE
    assert db.get_run(db.connect(db_path(runs)), "baseline-0001")["failure_code"] \
        == "STAGE_EXCEPTION:RuntimeError"


def test_idempotent_rerun_and_force(study, gates_cfg):
    runs, _ = study
    geom = Passing()
    r = Runner(runs, gates_cfg, {"geom": geom}, log=lambda m: None)
    r.run_one("baseline-0001")
    con = db.connect(db_path(runs))
    n = len(con.execute("SELECT * FROM gate_results").fetchall())
    r.run_one("baseline-0001", only="geom")              # finished -> no-op
    assert len(con.execute("SELECT * FROM gate_results").fetchall()) == n
    r.force("baseline-0001", "geom")
    assert status_of(runs, "baseline-0001") == "PENDING"
    assert r.run_one("baseline-0001", only="geom") == Status.GEOM_OK


def test_resume_after_crash_counts_prior_attempts(study, gates_cfg):
    runs, cfg = study
    need = cfg.mesh.surface_level[0] + 1
    r = Runner(runs, gates_cfg, {"geom": Fake(need)}, log=lambda m: None)
    con = db.connect(db_path(runs))
    # simulate: one failure + correction logged, then the process died
    db.set_status(con, "baseline-0001", Status.GEOM_FAIL, failure_code="LOW_LEVEL")
    r._log_correction(con, load_context(con, runs, "baseline-0001"), "gate0", "LOW_LEVEL",
                      Change("raise", "mesh.surface_level", [need, need + 1]),
                      list(cfg.mesh.surface_level), 1)
    assert r.run_one("baseline-0001") == Status.GEOM_OK
    assert len(db.corrections_for(con, "baseline-0001")) == 1


def test_worker_pool(study, gates_cfg):
    runs, cfg = study
    r = Runner(runs, gates_cfg, {"geom": Passing()}, log=quiet)   # must pickle
    out = r.run_study(cfg.name, workers=3)
    assert len(out) == 10 and set(out.values()) == {Status.GEOM_OK}


def test_prod_failure_retries_then_unrecoverable(study, gates_cfg):
    runs, _ = study
    impls = {"geom": Passing(), "mesh": Passing(), "pilot": Passing(), "prod": Fake(100)}
    r = Runner(runs, gates_cfg, impls, log=lambda m: None)
    assert r.run_one("baseline-0001") == Status.UNRECOVERABLE
    corr = db.corrections_for(db.connect(db_path(runs)), "baseline-0001", "prod")
    assert len(corr) == 3


def test_locked_run_is_skipped_not_run_twice(study, gates_cfg):
    """Open item (Phase 6): two `aero run` on one study must not run a case twice."""
    from aero.runner import run_dir, run_lock
    runs, cfg = study
    impl = Fake()
    r = Runner(runs, gates_cfg, {"geom": impl}, log=quiet)
    with run_lock(run_dir(runs, cfg.name, "baseline-0001")) as got:   # "another process"
        assert got
        assert r.run_one("baseline-0001") == Status.PENDING
        assert impl.calls == 0
        with run_lock(run_dir(runs, cfg.name, "baseline-0001")) as again:
            assert not again                                          # exclusive
    assert r.run_one("baseline-0001") == Status.GEOM_OK                # lock released
    assert impl.calls == 1


def test_force_refused_while_locked(study, gates_cfg):
    from aero.runner import RunLocked, run_dir, run_lock
    runs, cfg = study
    r = Runner(runs, gates_cfg, {"geom": Passing()}, log=quiet)
    r.run_one("baseline-0001")
    with run_lock(run_dir(runs, cfg.name, "baseline-0001")):
        with pytest.raises(RunLocked):
            r.force("baseline-0001", "geom")
    assert db.get_run(db.connect(db_path(runs)), "baseline-0001")["status"] == Status.GEOM_OK


def test_parallel_runners_each_run_once(study, gates_cfg, tmp_path):
    """Two runner processes on the same study: every stage call happens once."""
    import multiprocessing as mp
    runs, cfg = study
    log = tmp_path / "calls.log"
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_count_runner, args=(runs, gates_cfg, log)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    calls = log.read_text().split()
    assert sorted(calls) == sorted(set(calls)) and len(calls) == 10


class _Slow:
    def __init__(self, log): self.log = log
    def run(self, ctx):
        import time
        with open(self.log, "a") as fh:
            fh.write(ctx.run_id + "\n")
        time.sleep(0.2)
        return StageResult(True)
    def check(self, ctx, res): return GateResult(True, None, [Metric("ok", 1, 1, True)])
    def correct(self, ctx, g, a): return None


def _count_runner(runs, gates_cfg, log):
    r = Runner(runs, gates_cfg, {"geom": _Slow(log)}, log=quiet)
    for rid in [f"baseline-{i:04d}" for i in range(1, 11)]:
        r.run_one(rid)
