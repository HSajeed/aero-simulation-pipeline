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
