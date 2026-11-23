"""Correction rules from configs/corrections.yaml: failure code -> Change.

Only numerical settings. A code without a rule -> no correction (the runner
marks the run UNRECOVERABLE with that code). Design parameters are never touched.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from aero.runner import Change


def load(path: Path | str) -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def propose(rules: dict, gate: str, code: str | None, settings: dict) -> Change | None:
    rule = (rules.get(gate) or {}).get(code)
    if rule is None:
        return None
    action = rule["action"]
    if action == "refine_tessellation":
        f = float(rule["factor"])
        new = {k: int(round(v * f)) for k, v in settings["tessellation"].items()}
        return Change(f"refine_tessellation x{f}", "tessellation", new)
    raise ValueError(f"unknown correction action {action!r}")
