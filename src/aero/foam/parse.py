"""Parsers for OpenFOAM text output. Message strings verified against the
v2412 sources (Phase 0/2). A field that can't be found is None, never a
guessed default; gates treat None as a failure."""

from __future__ import annotations

import re


def _num(pattern: str, text: str, cast=float):
    m = re.search(pattern, text, re.M)
    return cast(m.group(1)) if m else None


def parse_surface_check(text: str) -> dict:
    """surfaceCheck [-checkSelfIntersection] log -> metrics."""
    out: dict = {
        "n_triangles": _num(r"^Triangles\s*:\s*(\d+)", text, int),
        "min_quality": _num(r"^\s*min\s+([\d.eE+-]+)\s+for triangle", text),
        "n_parts": _num(r"^Number of unconnected parts\s*:\s*(\d+)", text, int),
        "n_zones": _num(r"^Number of zones \(connected area with consistent normal\)\s*:\s*(\d+)",
                        text, int),
        "finished": bool(re.search(r"^End\s*$", text, re.M)),
    }
    if "Surface has no illegal triangles." in text:
        out["illegal_triangles"] = 0
    else:
        out["illegal_triangles"] = _num(r"^Surface has (\d+) illegal triangles\.", text, int)

    if "Surface is closed. All edges connected to two faces." in text:
        out["closed"] = True
    elif "Surface is not closed" in text:
        out["closed"] = False
    else:
        out["closed"] = None

    if "Checking self-intersection." not in text:
        out["self_intersections"] = None          # check not run
    elif "Surface is not self-intersecting" in text:
        out["self_intersections"] = 0
    else:
        out["self_intersections"] = _num(r"^Surface is self-intersecting at (\d+)", text, int)
    return out


def parse_check_mesh(text: str) -> dict:
    """checkMesh log -> metrics."""
    neg = re.search(r"\*\*\*Zero or negative cell volume detected.*?"
                    r"Number of negative volume cells: (\d+)", text)
    if neg:
        n_neg = int(neg.group(1))
    elif "Cell volumes OK." in text:
        n_neg = 0
    else:
        n_neg = None
    if re.search(r"^\s*Boundary openness .* OK\.", text, re.M):
        open_boundary = False
    elif "***Boundary openness" in text:
        open_boundary = True
    else:
        open_boundary = None
    return {
        "cells": _num(r"^\s*cells:\s+(\d+)", text, int),
        "max_non_orth": _num(r"Mesh non-orthogonality Max:\s+([\d.eE+-]+)", text),
        "max_skewness": _num(r"Max skewness = ([\d.eE+-]+)", text),
        "max_aspect_ratio": _num(r"Max aspect ratio = ([\d.eE+-]+)", text),
        "negative_volumes": n_neg,
        "open_boundary": open_boundary,
        "open_cells": "***Open cells found" in text,
        "mesh_ok": True if "\nMesh OK." in text else
                   (False if re.search(r"Failed \d+ mesh checks", text) else None),
        "failed_checks": _num(r"Failed (\d+) mesh checks", text, int) or 0,
        "patches": dict((m.group(1), int(m.group(2))) for m in re.finditer(
            r"^\s+(\w+)\s+(\d+)\s+\d+\s+ok \(", text, re.M)),
    }


def parse_snappy(text: str) -> dict:
    """snappyHexMesh log -> completion, final cell count, wall time."""
    m = list(re.finditer(r"^Snapped mesh : cells:(\d+)", text, re.M))
    return {
        "finished": "Finished meshing without any errors" in text,
        "snapped_cells": int(m[-1].group(1)) if m else None,
        "mesh_time_s": _num(r"Finished meshing in = ([\d.eE+-]+) s", text),
        "fatal": "FOAM FATAL" in text,
    }


def parse_dat(text: str) -> list[dict]:
    """postProcessing .dat (header line '# Time ...', tab separated) -> rows.
    Numeric cells become float (nan/inf kept as float); others stay strings."""
    header, rows = None, []
    for ln in text.splitlines():
        if ln.startswith("#"):
            cols = ln[1:].split()
            if cols and cols[0] == "Time":
                header = cols
            continue
        if header is None or not ln.strip():
            continue
        row = {}
        for k, v in zip(header, ln.split()):
            try:
                row[k] = float(v)
            except ValueError:
                row[k] = v
        rows.append(row)
    return rows


def read_function_object(case, name: str, filename: str) -> list[dict]:
    """All rows of postProcessing/<name>/<startTime>/<filename>*.dat, ordered by
    Time; a later restart overrides overlapping times."""
    from pathlib import Path
    base = Path(case) / "postProcessing" / name
    by_time: dict[float, dict] = {}
    if not base.is_dir():
        return []
    starts = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: float(d.name))
    for d in starts:
        for f in sorted(d.glob(f"{filename}*.dat")):
            for r in parse_dat(f.read_text()):
                if isinstance(r.get("Time"), float):
                    by_time[r["Time"]] = r
    return [by_time[t] for t in sorted(by_time)]


def parse_solver_log(text: str) -> dict:
    """simpleFoam / potentialFoam log -> crash markers and timing."""
    times = re.findall(r"^Time = ([\d.eE+-]+)\s*$", text, re.M)
    exe = re.findall(r"ExecutionTime = ([\d.eE+-]+) s", text)
    return {
        "fatal": "FOAM FATAL" in text,
        "fatal_io": "FOAM FATAL IO ERROR" in text,
        # a real FPE prints a stack through sigFpe::sigHandler; the startup banner
        # "trapFpe: Floating point exception trapping enabled" is NOT a crash
        "fpe": bool(re.search(r"sigFpe::sigHandler|Floating point exception \(core dumped\)",
                              text)),
        "nan_residual": bool(re.search(r"Initial residual = -?nan", text, re.I)),
        "ended": bool(re.search(r"^End\s*$", text, re.M)),
        "last_time": float(times[-1]) if times else None,
        "execution_time_s": float(exe[-1]) if exe else None,
        "n_iterations": len(times),
        "setup_error": bool(re.search(
            r"FOAM FATAL (IO )?ERROR.*?(Unknown patchField type|Cannot find patchField entry"
            r"|keyword .* is undefined|Unknown .*Model|cannot find file|Entry .* not found)",
            text, re.S)),
    }


def simple_converged(text: str) -> bool:
    """simpleFoam stopped by residualControl."""
    return bool(re.search(r"SIMPLE solution converged in", text))


# ------------------------------------------------------------- sampled planes
def parse_vtk_polydata(text: str) -> dict:
    """Legacy ASCII VTK POLYDATA as written by the `surfaces` function object
    (surfaceFormat vtk, legacy true): points (n,3), polygons (list of index
    lists) and CELL_DATA fields {name: array (n_cells, n_comp)}."""
    import numpy as np
    tok = text.split()
    i, out = 0, {"points": None, "polygons": [], "cell": {}}

    def take(n):
        nonlocal i
        vals = tok[i:i + n]
        if len(vals) < n:
            raise ValueError("truncated VTK file")
        i += n
        return vals

    while i < len(tok):
        t = tok[i]
        if t == "POINTS":
            n = int(tok[i + 1]); i += 3
            out["points"] = np.array(take(3 * n), dtype=float).reshape(n, 3)
        elif t == "POLYGONS":
            n = int(tok[i + 1]); i += 3
            polys = []
            for _ in range(n):
                k = int(take(1)[0])
                polys.append([int(v) for v in take(k)])
            out["polygons"] = polys
        elif t == "CELL_DATA":
            ncell = int(tok[i + 1]); i += 2
            if tok[i] == "FIELD":
                nf = int(tok[i + 2]); i += 3
                for _ in range(nf):
                    name, ncomp, ntup = tok[i], int(tok[i + 1]), int(tok[i + 2]); i += 4
                    if ntup != ncell:
                        raise ValueError(f"field {name}: {ntup} tuples for {ncell} cells")
                    out["cell"][name] = np.array(take(ncomp * ntup), dtype=float).reshape(ntup, ncomp)
        else:
            i += 1
    if out["points"] is None:
        raise ValueError("no POINTS in VTK file")
    return out
