import os
from pathlib import Path

import pytest

from aero.foam.container import BASHRC, IMAGE, build_command, run_script


def test_podman_uses_keep_id_and_bash_entrypoint(tmp_path):
    cmd = build_command(tmp_path, "s.sh", "podman")
    assert cmd[:3] == ["podman", "run", "--rm"]
    assert "--userns=keep-id" in cmd and "-u" not in cmd
    assert cmd[cmd.index("--entrypoint") + 1] == "bash"
    assert f"{tmp_path.resolve()}:/case" in cmd
    assert cmd[-2:] == [IMAGE, "/case/s.sh"]


def test_docker_uses_host_uid(tmp_path):
    cmd = build_command(tmp_path, "s.sh", "docker")
    assert cmd[cmd.index("-u") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "--userns=keep-id" not in cmd


def test_unknown_engine(tmp_path):
    with pytest.raises(ValueError):
        build_command(tmp_path, "s.sh", "lxc")


def test_image_pinned_by_digest():
    assert "@sha256:" in IMAGE and "2412" in BASHRC


def test_script_has_preamble(tmp_path, monkeypatch):
    import subprocess

    class R:
        returncode = 0
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    r = run_script(tmp_path, "blockMesh", "blockMesh")
    text = r.script_path.read_text()
    assert text.startswith("#!/bin/bash") and f"source {BASHRC}" in text
    assert text.rstrip().endswith("blockMesh") and r.log_path == tmp_path / "log.blockMesh"
