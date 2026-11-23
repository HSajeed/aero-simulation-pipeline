"""Mesh stage: blockMesh + surfaceFeatureExtract + snappyHexMesh + checkMesh in
the container (one run per attempt), then Gate 1."""

from __future__ import annotations

import json

from aero import db
from aero.corrections import rules
from aero.foam.case import write_mesh_case
from aero.foam.container import run_script, unique_path
from aero.foam.parse import parse_check_mesh, parse_snappy
from aero.gates import gate1
from aero.geom.openvsp_build import stl_triangles, tri_area
from aero.runner import Change, GateResult, Metric, RunContext, StageResult


def _tag(case, attempt: int) -> str:
    """Unique log tag per execution: a<attempt>, a<attempt>.1, ... (logs are history)."""
    p = unique_path(case / f"log.snappyHexMesh.a{attempt}")
    return p.name.removeprefix("log.snappyHexMesh.")


class MeshStage:
    def run(self, ctx: RunContext) -> StageResult:
        row = ctx.con.execute("SELECT data FROM geometry WHERE run=?", (ctx.run_id,)).fetchone()
        if row is None:
            raise RuntimeError("no geometry record; run the geom stage first")
        geom = json.loads(row["data"])
        mesh = ctx.settings["mesh"]
        case = ctx.dir / "mesh"
        stl = ctx.dir / "geom" / "body.stl"
        if set(geom.get("stl_solid_area", {})) != {"wing", "fuselage"}:
            raise RuntimeError("STL has no wing/fuselage solids; rerun geom with --force")
        tag = _tag(case, ctx.attempt)
        layout = write_mesh_case(case, stl, geom, mesh, list(ctx.settings["alphas"]), tag)
        unique_path(case / f"settings_used.{tag}.json").write_text(
            json.dumps({"mesh": mesh, "layout": {k: v for k, v in layout.items() if k != "script"}},
                       indent=2))
        r = run_script(case, layout["script"], f"mesh.{tag}",
                       engine=ctx.settings["container_engine"], image=ctx.settings["image"],
                       timeout=3600)

        def read(name):
            p = case / f"log.{name}.{tag}"
            return p.read_text(errors="replace") if p.exists() else ""

        shm = parse_snappy(read("snappyHexMesh"))
        if r.returncode != 0:
            shm["finished"] = False
        cm = parse_check_mesh(read("checkMesh"))
        snapped, ratio = {}, None
        try:
            for k in ("wing", "fuselage"):
                snapped[k] = tri_area(stl_triangles(case / f"snapped_{k}.stl"))
            stl_half = geom["stl_area"] / 2.0      # body symmetric about y = 0
            ratio = sum(snapped.values()) / stl_half
        except (OSError, ValueError):
            pass
        return StageResult(True, {"shm": shm, "cm": cm, "area_ratio": ratio,
                                  "snapped_area": snapped, "wall_time_s": r.wall_time_s,
                                  "background_cells": layout["background_cells"]})

    def check(self, ctx: RunContext, result: StageResult) -> GateResult:
        d = result.data
        g = gate1.evaluate(d["shm"], d["cm"], d["area_ratio"], ctx.gates["gate1"])
        g.metrics.append(Metric("mesh_wall_time_s", d["wall_time_s"], None, True, "warn"))
        for k, v in d["snapped_area"].items():
            g.metrics.append(Metric(f"snapped_area_{k}", v, None, True, "warn"))
        return g

    def correct(self, ctx: RunContext, gate: GateResult, attempt: int) -> Change | None:
        # step through this code's action list by how often *this code* was corrected
        n = sum(1 for c in db.corrections_for(ctx.con, ctx.run_id, "gate1")
                if c["code"] == gate.failure_code)
        return rules.propose(ctx.corrections, "gate1", gate.failure_code, ctx.settings, n + 1)
