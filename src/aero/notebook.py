"""Minimal notebook writer (decision Phase 7: no Jupyter dependency).

Executes code cells in one shared namespace, captures their stdout and every
matplotlib figure they open, and writes a standard nbformat-4 .ipynb with the
outputs embedded. The code cells are exactly what produced the outputs, so the
notebook can also be re-run in Jupyter by anyone who has it installed.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import traceback
from pathlib import Path


def _lines(text: str) -> list[str]:
    parts = text.splitlines(keepends=True)
    return parts or [""]


def run_cells(cells: list[tuple[str, str]], cwd: Path | None = None) -> dict:
    """cells: [("md" | "code", source)] -> nbformat-4 dict. A failing cell
    stops execution (its traceback is the cell output) and raises."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ns: dict = {"__name__": "__main__"}
    out_cells, count = [], 0
    for kind, src in cells:
        src = src.strip("\n")
        if kind == "md":
            out_cells.append({"cell_type": "markdown", "metadata": {}, "source": _lines(src)})
            continue
        count += 1
        outputs, buf = [], io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.chdir(cwd or Path.cwd()):
                exec(compile(src, f"<cell {count}>", "exec"), ns)
        except Exception:
            err = traceback.format_exc()
            raise RuntimeError(f"notebook cell {count} failed:\n{src}\n\n{err}") from None
        if buf.getvalue():
            outputs.append({"output_type": "stream", "name": "stdout",
                            "text": _lines(buf.getvalue())})
        for num in plt.get_fignums():
            png = io.BytesIO()
            plt.figure(num).savefig(png, format="png", dpi=110, bbox_inches="tight")
            outputs.append({"output_type": "display_data", "metadata": {},
                            "data": {"image/png": base64.b64encode(png.getvalue()).decode(),
                                     "text/plain": ["<Figure>"]}})
        plt.close("all")
        out_cells.append({"cell_type": "code", "execution_count": count, "metadata": {},
                          "outputs": outputs, "source": _lines(src)})
    return {"cells": out_cells, "nbformat": 4, "nbformat_minor": 4,
            "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python",
                                        "name": "python3"},
                         "language_info": {"name": "python"}}}


def write(path: Path, nb: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(nb, indent=1) + "\n")
    return path
