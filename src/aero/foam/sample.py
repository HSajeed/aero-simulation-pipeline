"""Report-only field sampling: spanwise cutting planes (y = const, PLAN Phase 6:
30 % / 70 % semi-span) of U and p at each alpha's final iteration, written by
the OpenFOAM `surfaces` function object (postProcess on existing time
directories; no solve, results untouched). Gates never read these (rule 4).

Times: each alpha's `end_iter` from the results table. The alpha_1 time
directory still holds alpha_1's interior field (only its farfield BC value was
switched for the alpha_2 warm start), and cell values are sampled
(`interpolate false`), so the boundary value does not enter.
"""

from __future__ import annotations

from pathlib import Path

import jinja2

from aero import db
from aero.foam.case import TEMPLATES
from aero.foam.container import IMAGE, run_script

PLANE_FRACTIONS = (("y30", 0.30), ("y70", 0.70))   # of the semi-span
OUT = Path("postProcessing") / "planes"


def plane_positions(span_full: float) -> list[tuple[str, float]]:
    return [(n, f * span_full / 2) for n, f in PLANE_FRACTIONS]


def plane_files(case: Path, time: int) -> dict[str, Path]:
    d = case / OUT / str(time)
    return {n: d / f"{n}.vtk" for n, _ in PLANE_FRACTIONS if (d / f"{n}.vtk").exists()}


def sample_run(con, run_dir: Path, run_id: str, *, engine: str = "podman", image: str = IMAGE,
               force: bool = False) -> dict:
    """Sample a finished run. Idempotent: skipped when every plane file exists,
    unless force. Returns {'status': 'sampled'|'skipped'|'no_results'|'failed', ...}."""
    case = run_dir / "case"
    results = db.get_results(con, run_id)
    times = sorted({int(r["end_iter"]) for r in results.values() if r["end_iter"] is not None})
    times = [t for t in times if (case / str(t)).is_dir()]
    if not times:
        return {"status": "no_results"}
    if not force and all(len(plane_files(case, t)) == len(PLANE_FRACTIONS) for t in times):
        return {"status": "skipped", "times": times}
    span = con.execute("SELECT span FROM geometry WHERE run=?", (run_id,)).fetchone()["span"]
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES / "solve"),
                             keep_trailing_newline=True)
    (case / "system" / "samplePlanes").write_text(
        env.get_template("samplePlanes.j2").render(planes=plane_positions(span)))
    tlist = ",".join(str(t) for t in times)
    r = run_script(case, f"postProcess -dict system/samplePlanes -fields '(U p)' -time '{tlist}'",
                   "sample", engine=engine, image=image, timeout=1800)
    ok = r.returncode == 0 and all(len(plane_files(case, t)) == len(PLANE_FRACTIONS)
                                   for t in times)
    return {"status": "sampled" if ok else "failed", "times": times, "log": str(r.log_path),
            "rc": r.returncode}
