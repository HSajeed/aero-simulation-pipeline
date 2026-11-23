"""Correction rules from configs/corrections.yaml: failure code -> Change.

A rule is one action, or a list of actions used in order by attempt number
(attempt k uses entry k, the last entry repeats). Actions:
  refine_tessellation {factor}   scale every tessellation count
  add   {param, by}              add (element-wise for lists), ints stay >= 0
  scale {param, by}              multiply
Only numerical settings; the runner refuses anything else. A code without a
rule -> no correction (UNRECOVERABLE with that code).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from aero.runner import Change, get_path


def load(path: Path | str) -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def _add(old, by):
    if isinstance(old, list):
        by = by if isinstance(by, list) else [by] * len(old)
        return [_add(o, b) for o, b in zip(old, by)]
    new = old + by
    return max(new, 0) if isinstance(old, int) and isinstance(by, int) else new


def propose(rules: dict, gate: str, code: str | None, settings: dict,
            attempt: int = 1) -> Change | None:
    rule = (rules.get(gate) or {}).get(code)
    if rule is None:
        return None
    if isinstance(rule, list):
        rule = rule[min(attempt, len(rule)) - 1]
    action = rule["action"]
    if action == "refine_tessellation":
        f = float(rule["factor"])
        new = {k: int(round(v * f)) for k, v in settings["tessellation"].items()}
        return Change(f"refine_tessellation x{f}", "tessellation", new)
    if action in ("add", "scale"):
        param, by = rule["param"], rule["by"]
        old = get_path(settings, param)
        new = _add(old, by) if action == "add" else round(old * by, 6)
        if new == old:
            return None          # no further change possible
        return Change(f"{action} {param} {by}", param, new)
    raise ValueError(f"unknown correction action {action!r}")
