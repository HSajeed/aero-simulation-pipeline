from typer.testing import CliRunner

from aero.cli import app
from conftest import ROOT

cli = CliRunner()


def test_init_status_run(tmp_path):
    cfg = str(ROOT / "configs/study_baseline.yaml")
    r = cli.invoke(app, ["init-study", cfg, "--runs-dir", str(tmp_path)])
    assert r.exit_code == 0 and "created 10 PENDING" in r.output
    r = cli.invoke(app, ["init-study", cfg, "--runs-dir", str(tmp_path)])
    assert r.exit_code == 0 and "already exists" in r.output
    r = cli.invoke(app, ["status", "baseline", "--runs-dir", str(tmp_path)])
    assert r.exit_code == 0 and r.output.count("PENDING") == 11   # 10 rows + total line
    r = cli.invoke(app, ["run", "baseline", "--runs-dir", str(tmp_path), "--workers", "1",
                         "--gates", str(ROOT / "configs/gates.yaml")])
    finals = [l for l in r.output.splitlines() if l.startswith("baseline-") and "  " in l]
    assert r.exit_code == 0 and len(finals) == 10 and all(l.endswith("PENDING") for l in finals)
    r = cli.invoke(app, ["status", "nope", "--runs-dir", str(tmp_path)])
    assert r.exit_code == 2
