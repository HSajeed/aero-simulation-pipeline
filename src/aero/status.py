"""Run status state machine (CLAUDE.md). The DB rejects any other transition."""

from __future__ import annotations

from enum import StrEnum


class Status(StrEnum):
    PENDING = "PENDING"
    GEOM_OK = "GEOM_OK"
    GEOM_FAIL = "GEOM_FAIL"
    MESH_OK = "MESH_OK"
    MESH_FAIL = "MESH_FAIL"
    PILOT_OK = "PILOT_OK"
    PILOT_FAIL = "PILOT_FAIL"
    PROD_FAIL = "PROD_FAIL"
    PROD_DONE = "PROD_DONE"
    CHECKS_PASS = "CHECKS_PASS"
    FLAGGED = "FLAGGED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNRECOVERABLE = "UNRECOVERABLE"


S = Status
# A *_FAIL may be retried (-> OK / FAIL again) until the budget is spent.
AUTO_TRANSITIONS: dict[Status, set[Status]] = {
    S.PENDING: {S.GEOM_OK, S.GEOM_FAIL},
    S.GEOM_FAIL: {S.GEOM_OK, S.GEOM_FAIL, S.UNRECOVERABLE},
    S.GEOM_OK: {S.MESH_OK, S.MESH_FAIL},
    S.MESH_FAIL: {S.MESH_OK, S.MESH_FAIL, S.UNRECOVERABLE},
    S.MESH_OK: {S.PILOT_OK, S.PILOT_FAIL},
    S.PILOT_FAIL: {S.PILOT_OK, S.PILOT_FAIL, S.UNRECOVERABLE},
    S.PILOT_OK: {S.PROD_DONE, S.PROD_FAIL},
    S.PROD_FAIL: {S.PROD_DONE, S.PROD_FAIL, S.UNRECOVERABLE},
    S.PROD_DONE: {S.CHECKS_PASS, S.FLAGGED},
}
# Only a human (review CLI) may leave FLAGGED. The pipeline never auto-accepts.
HUMAN_TRANSITIONS: dict[Status, set[Status]] = {
    S.FLAGGED: {S.ACCEPTED, S.REJECTED},
}
TERMINAL = {S.CHECKS_PASS, S.ACCEPTED, S.REJECTED, S.UNRECOVERABLE}


def allowed(old: Status, new: Status, *, human: bool = False) -> bool:
    if new in AUTO_TRANSITIONS.get(old, set()):
        return True
    return human and new in HUMAN_TRANSITIONS.get(old, set())
