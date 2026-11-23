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
