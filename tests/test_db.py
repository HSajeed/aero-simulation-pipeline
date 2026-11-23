import pytest

from aero import db
from aero.status import AUTO_TRANSITIONS, Status, allowed


def test_migration_idempotent(tmp_path):
    con = db.connect(tmp_path / "r.db")
    assert con.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert db.migrate(con) == len(db.MIGRATIONS)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "gate_results", "corrections", "results", "reviews", "studies"} <= tables


def make(tmp_path):
    con = db.connect(tmp_path / "r.db")
    db.create_study(con, "s", "name: s", [("s-0001", 1, {"span": 10.0})])
    return con


def test_happy_path_transitions(tmp_path):
    con = make(tmp_path)
    for st in ["GEOM_OK", "MESH_OK", "PILOT_OK", "PROD_FAIL", "PROD_DONE", "FLAGGED"]:
        db.set_status(con, "s-0001", Status(st))
    with pytest.raises(db.TransitionError):          # pipeline may not accept
        db.set_status(con, "s-0001", Status.ACCEPTED)
    assert db.set_status(con, "s-0001", Status.ACCEPTED, human=True)["status"] == "ACCEPTED"


def test_retry_and_unrecoverable(tmp_path):
    con = make(tmp_path)
    db.set_status(con, "s-0001", Status.GEOM_FAIL, failure_code="X")
    db.set_status(con, "s-0001", Status.GEOM_FAIL, failure_code="X")
    db.set_status(con, "s-0001", Status.UNRECOVERABLE, failure_code="X")
    with pytest.raises(db.TransitionError):
        db.set_status(con, "s-0001", Status.GEOM_OK)


@pytest.mark.parametrize("old,new", [("PENDING", "MESH_OK"), ("GEOM_OK", "PILOT_OK"),
                                     ("PILOT_OK", "CHECKS_PASS"), ("CHECKS_PASS", "FLAGGED"),
                                     ("PENDING", "UNRECOVERABLE"), ("FLAGGED", "CHECKS_PASS")])
def test_forbidden(old, new):
    assert not allowed(Status(old), Status(new))
    assert not allowed(Status(old), Status(new), human=True) or old == "FLAGGED"


def test_every_status_reachable():
    reach, todo = {Status.PENDING}, [Status.PENDING]
    while todo:
        for n in AUTO_TRANSITIONS.get(todo.pop(), ()):
            if n not in reach:
                reach.add(n); todo.append(n)
    assert reach | {Status.ACCEPTED, Status.REJECTED} == set(Status)
