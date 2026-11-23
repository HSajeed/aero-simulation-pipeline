"""Run database (SQLite, stdlib). Single source of truth for run state.

Schema changes: append to MIGRATIONS; never drop/recreate tables.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aero.status import Status, allowed

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE studies (
        name TEXT PRIMARY KEY,
        config_yaml TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        study TEXT NOT NULL REFERENCES studies(name),
        idx INTEGER NOT NULL,
        params TEXT NOT NULL,
        status TEXT NOT NULL,
        failure_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (study, idx)
    );
    CREATE TABLE gate_results (
        id INTEGER PRIMARY KEY,
        run TEXT NOT NULL REFERENCES runs(id),
        gate TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        metric TEXT NOT NULL,
        value REAL,
        threshold REAL,
        pass INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE corrections (
        id INTEGER PRIMARY KEY,
        run TEXT NOT NULL REFERENCES runs(id),
        gate TEXT NOT NULL,
        code TEXT NOT NULL,
        action TEXT NOT NULL,
        param TEXT NOT NULL,
        old TEXT,
        new TEXT,
        attempt INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE results (
        id INTEGER PRIMARY KEY,
        run TEXT NOT NULL REFERENCES runs(id),
        alpha REAL NOT NULL,
        CL REAL, CD REAL, Cm REAL,
        iterations INTEGER,
        wall_time REAL,
        UNIQUE (run, alpha)
    );
    CREATE TABLE reviews (
        id INTEGER PRIMARY KEY,
        run TEXT NOT NULL REFERENCES runs(id),
        decision TEXT NOT NULL,
        label TEXT NOT NULL,
        note TEXT,
        reviewer TEXT,
        time TEXT NOT NULL
    );
    CREATE INDEX idx_runs_study ON runs(study);
    CREATE INDEX idx_corr_run ON corrections(run, gate);
    """,
    # v2: warn-only metrics, geometry reference values, VSPAERO reference results
    """
    ALTER TABLE gate_results ADD COLUMN severity TEXT NOT NULL DEFAULT 'gate';
    CREATE TABLE geometry (
        run TEXT PRIMARY KEY REFERENCES runs(id),
        sref_full REAL NOT NULL,
        sref_half REAL NOT NULL,
        span REAL NOT NULL,
        mac REAL NOT NULL,
        x_ref REAL NOT NULL,
        tip_chord REAL NOT NULL,
        stl_area REAL,
        n_triangles INTEGER,
        data TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE reference_results (
        run TEXT NOT NULL REFERENCES runs(id),
        source TEXT NOT NULL,
        alpha REAL NOT NULL,
        CL REAL, CDi REAL, Cm REAL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run, source, alpha)
    );
    """,
    # v3: per-iteration solver history (residuals + coefficients)
    """
    CREATE TABLE history (
        run TEXT NOT NULL REFERENCES runs(id),
        phase TEXT NOT NULL,
        tag TEXT NOT NULL,
        alpha REAL NOT NULL,
        iteration INTEGER NOT NULL,
        Ux REAL, Uy REAL, Uz REAL, p REAL,
        Cl REAL, Cd REAL, Cm REAL,
        PRIMARY KEY (run, phase, tag, iteration)
    );
    """,
]


class TransitionError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; explicit txns
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")   # concurrent workers
    migrate(con)
    return con


def migrate(con: sqlite3.Connection) -> int:
    version = con.execute("PRAGMA user_version").fetchone()[0]
    for v, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        con.execute("BEGIN")
        try:
            for stmt in filter(str.strip, sql.split(";")):
                con.execute(stmt)
            con.execute(f"PRAGMA user_version = {v}")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return con.execute("PRAGMA user_version").fetchone()[0]


# ----------------------------------------------------------------- studies/runs
def study_exists(con, name: str) -> bool:
    return con.execute("SELECT 1 FROM studies WHERE name=?", (name,)).fetchone() is not None


def create_study(con, name: str, config_yaml: str,
                 runs: list[tuple[str, int, dict]]) -> None:
    """Create a study and its PENDING runs atomically."""
    t = now()
    con.execute("BEGIN")
    try:
        con.execute("INSERT INTO studies VALUES (?,?,?)", (name, config_yaml, t))
        con.executemany(
            "INSERT INTO runs (id, study, idx, params, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            [(rid, name, idx, json.dumps(p), Status.PENDING, t, t) for rid, idx, p in runs])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def get_run(con, run_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return row


def list_runs(con, study: str) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM runs WHERE study=? ORDER BY idx", (study,)).fetchall()


def set_status(con, run_id: str, new: Status, *, failure_code: str | None = None,
               human: bool = False) -> sqlite3.Row:
    old = Status(get_run(con, run_id)["status"])
    if not allowed(old, new, human=human):
        raise TransitionError(f"{run_id}: {old} -> {new} not allowed")
    con.execute("UPDATE runs SET status=?, failure_code=?, updated_at=? WHERE id=?",
                (new, failure_code, now(), run_id))
    return get_run(con, run_id)


def force_reset(con, run_id: str, to: Status) -> sqlite3.Row:
    """--force: rewind a run to an earlier stage's input status. Explicit
    human action only; history (gate results, corrections) is kept."""
    con.execute("UPDATE runs SET status=?, failure_code=NULL, updated_at=? WHERE id=?",
                (to, now(), run_id))
    return get_run(con, run_id)


# -------------------------------------------------------------- gates/corrections
def add_gate_results(con, run_id: str, gate: str, attempt: int,
                     metrics: list[dict[str, Any]]) -> None:
    t = now()
    con.executemany(
        "INSERT INTO gate_results (run, gate, attempt, metric, value, threshold, pass,"
        " severity, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [(run_id, gate, attempt, m["metric"], m.get("value"), m.get("threshold"),
          int(bool(m["pass"])), m.get("severity", "gate"), t) for m in metrics])


def put_geometry(con, run_id: str, g: dict[str, Any]) -> None:
    keys = ("sref_full", "sref_half", "span", "mac", "x_ref", "tip_chord", "stl_area", "n_triangles")
    con.execute(
        "INSERT OR REPLACE INTO geometry (run, %s, data, created_at) VALUES (?, %s, ?, ?)"
        % (", ".join(keys), ", ".join("?" * len(keys))),
        (run_id, *(g.get(k) for k in keys), json.dumps(g), now()))


def put_history(con, run_id: str, phase: str, tag: str, alpha: float,
                rows: list[dict[str, Any]]) -> None:
    keys = ("Ux", "Uy", "Uz", "p", "Cl", "Cd", "Cm")
    con.executemany(
        "INSERT OR REPLACE INTO history VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(run_id, phase, tag, alpha, r["iteration"], *(r.get(k) for k in keys)) for r in rows])


def clear_reference(con, run_id: str, source: str) -> None:
    """Drop a run's stale reference rows (e.g. a later attempt's VSPAERO failed)."""
    con.execute("DELETE FROM reference_results WHERE run=? AND source=?", (run_id, source))


def put_reference(con, run_id: str, source: str, rows: list[dict[str, float]]) -> None:
    clear_reference(con, run_id, source)
    t = now()
    con.executemany(
        "INSERT OR REPLACE INTO reference_results VALUES (?,?,?,?,?,?,?)",
        [(run_id, source, r["alpha"], r.get("CL"), r.get("CDi"), r.get("Cm"), t) for r in rows])


def add_correction(con, run_id: str, gate: str, code: str, action: str, param: str,
                   old: Any, new: Any, attempt: int) -> None:
    con.execute(
        "INSERT INTO corrections (run, gate, code, action, param, old, new, attempt, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, gate, code, action, param, json.dumps(old), json.dumps(new), attempt, now()))


def corrections_for(con, run_id: str, gate: str | None = None) -> list[sqlite3.Row]:
    if gate is None:
        return con.execute("SELECT * FROM corrections WHERE run=? ORDER BY id", (run_id,)).fetchall()
    return con.execute("SELECT * FROM corrections WHERE run=? AND gate=? ORDER BY id",
                       (run_id, gate)).fetchall()
