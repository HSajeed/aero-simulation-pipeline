"""Phase 7: GCI procedure, lifting-line slope, level choice."""

import math

import pytest

from aero.validation import GCI, choose_level, gci, helmbold_slope, observed_order


def test_gci_celik_2008_example():
    """Celik et al. (2008) Table 1, column phi = x_r: N = 18000/8000/4500 (2-D),
    phi = 6.063/5.972/5.863 -> p = 1.53, phi_ext = 6.1685, e_a21 = 1.5 %,
    e_ext21 = 1.7 %, GCI_fine21 = 2.2 %."""
    h = tuple((1 / n) ** 0.5 for n in (18000, 8000, 4500))
    g = gci((6.063, 5.972, 5.863), h)
    assert g.convergence == "monotone"
    assert g.p == pytest.approx(1.53, abs=0.01)
    assert g.phi_ext == pytest.approx(6.1685, abs=1e-3)
    assert g.e_a21 == pytest.approx(0.015, abs=5e-4)
    assert g.e_ext21 == pytest.approx(0.017, abs=5e-4)
    assert g.gci_fine == pytest.approx(0.022, abs=1e-3)


def test_gci_exact_second_order():
    """phi = 1 + h^2 sampled exactly -> p = 2, extrapolation = 1."""
    h = (0.5, 0.5 * math.sqrt(2), 1.0)
    g = gci(tuple(1 + x * x for x in h), h)
    assert g.p == pytest.approx(2.0, rel=1e-6)
    assert g.phi_ext == pytest.approx(1.0, rel=1e-9)
    # Roache ratio on relative GCIs = phi1/phi2 here; ~1 when phi changes little
    assert g.asymptotic == pytest.approx(1.25 / 1.5, rel=1e-6)
    # unified form: GCI_k = Fs |phi_ext - phi_k| / |phi_k|
    assert g.gci_coarse == pytest.approx(1.25 * 1 / 2)
    assert g.gci_fine < g.gci_medium < g.gci_coarse


def test_gci_oscillatory_and_divergent_give_no_gci():
    h = (0.5, 0.7, 1.0)
    osc = gci((1.0, 1.1, 0.95), h)
    assert osc.convergence == "oscillatory" and osc.gci_fine is None
    div = gci((1.0, 1.2, 1.25), h)
    assert div.convergence == "divergent" and div.gci_fine is None


def test_gci_rejects_unordered_h():
    with pytest.raises(ValueError):
        gci((1, 2, 3), (1.0, 0.7, 0.5))


def test_observed_order_zero_change():
    assert observed_order(0.0, 0.1, 1.4, 1.4) is None


def _g(f, m, c):
    return GCI((0, 0, 0), (1, 2, 3), 1, 1, 2.0, "monotone", 0, 0, 0, f, m, c, 1)


def test_choose_level_picks_coarsest_passing():
    assert choose_level({"a": _g(0.01, 0.02, 0.05), "b": _g(0.005, 0.01, 0.02)}, 0.03) == "medium"
    assert choose_level({"a": _g(0.01, 0.02, 0.025)}, 0.03) == "coarse"
    assert choose_level({"a": _g(0.04, 0.05, 0.06)}, 0.03) is None
    assert choose_level({"a": _g(None, None, None)}, 0.03) is None     # not computable -> human


def test_helmbold_limits():
    # high aspect ratio, unswept -> approaches 2 pi; known value A = 6: ~4.6 /rad
    s, b = 10.0, 100.0
    assert helmbold_slope(b, b * b / 1000, 1.0, 0.0) == pytest.approx(2 * math.pi, rel=0.01)
    assert helmbold_slope(math.sqrt(6 * s), s, 1.0, 0.0) == pytest.approx(4.6, abs=0.1)
    # sweep lowers the slope
    assert helmbold_slope(12, 20, 0.5, 30) < helmbold_slope(12, 20, 0.5, 0)


def test_notebook_writer(tmp_path):
    import json
    from aero import notebook
    nb = notebook.run_cells([
        ("md", "# t"),
        ("code", "x = 2\nprint(x * 3)"),
        ("code", "import matplotlib.pyplot as plt\nplt.plot([0, 1], [x, x])"),
    ])
    p = notebook.write(tmp_path / "n.ipynb", nb)
    d = json.loads(p.read_text())
    assert d["nbformat"] == 4 and [c["cell_type"] for c in d["cells"]] == ["markdown", "code", "code"]
    assert d["cells"][1]["outputs"][0]["text"] == ["6\n"]
    assert "image/png" in d["cells"][2]["outputs"][0]["data"]


def test_notebook_writer_fails_loudly():
    from aero import notebook
    with pytest.raises(RuntimeError, match="cell 1 failed"):
        notebook.run_cells([("code", "1 / 0")])


def test_choose_level_relaxed():
    from aero.validation import choose_level_relaxed
    cl = {"coarse": 0.2667, "medium": 0.2694, "fine": 0.2719}      # Phase 7 values
    slope = [_g(0.003, 0.010, 0.034), _g(0.003, 0.010, 0.034)]
    assert choose_level_relaxed(slope, cl, 0.03, 0.01) == "medium"   # coarse fails slope GCI
    assert choose_level_relaxed([_g(0.003, 0.010, 0.02)], cl, 0.03, 0.01) == "medium"  # coarse CL change 1.002 %
    assert choose_level_relaxed([_g(0.003, 0.010, 0.02)], {**cl, "coarse": 0.268}, 0.03, 0.01) == "coarse"
    assert choose_level_relaxed(slope, cl, 0.03, 0.005) is None       # CL change 0.93 % > 0.5 %
    assert choose_level_relaxed([_g(None, None, None)], cl, 0.03, 0.01) is None
