"""Run OpenFOAM stage scripts inside the pinned container.

One `<engine> run --rm` per stage. The generated script is written into the
case dir, executed there, and its combined stdout/stderr goes to
`log.<name>` in the case dir.

Verified facts (Phase 0, image below):
- The image entrypoint `/openfoam/run` ignores `-w` and cd's to $HOME, so we
  override it with `--entrypoint bash` and source the bashrc ourselves.
- Rootless Podman + `--userns=keep-id` -> files written in the case dir are
  owned by the host user. Docker needs `-u uid:gid` instead.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

IMAGE = (
    "docker.io/opencfd/openfoam-default"
    "@sha256:1ba02114b1c025c370f2e269a07677c16c9bea8d990fcd75ac8378aff9d41b50"
)  # tag 2412
BASHRC = "/usr/lib/openfoam/openfoam2412/etc/bashrc"
MOUNT = "/case"

_PREAMBLE = f"""#!/bin/bash
source {BASHRC} || exit 90
set -eo pipefail
cd {MOUNT}
"""


@dataclass(frozen=True)
class ContainerResult:
    returncode: int
    log_path: Path
    script_path: Path
    wall_time_s: float


def unique_path(p: Path) -> Path:
    """p if free, else p.1, p.2, ...: logs are history and are never overwritten."""
    if not p.exists():
        return p
    n = 1
    while p.with_name(f"{p.name}.{n}").exists():
        n += 1
    return p.with_name(f"{p.name}.{n}")


def build_command(case_dir: Path, script_name: str, engine: str = "podman",
                  image: str = IMAGE) -> list[str]:
    if engine == "podman":
        user = ["--userns=keep-id"]
    elif engine == "docker":
        user = ["-u", f"{os.getuid()}:{os.getgid()}"]
    else:
        raise ValueError(f"unknown container engine: {engine!r}")
    return [
        engine, "run", "--rm", *user,
        "-v", f"{case_dir.resolve()}:{MOUNT}",
        "-w", MOUNT,
        "--entrypoint", "bash",
        image,
        f"{MOUNT}/{script_name}",
    ]


def run_script(case_dir: Path | str, body: str, name: str, *,
               engine: str = "podman", image: str = IMAGE,
               timeout: float | None = None) -> ContainerResult:
    """Write `<name>.sh` (preamble + body) into case_dir and run it in the container."""
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    script = case_dir / f"{name}.sh"
    script.write_text(_PREAMBLE + body.strip() + "\n")
    script.chmod(0o755)
    log = unique_path(case_dir / f"log.{name}")
    cmd = build_command(case_dir, script.name, engine, image)
    t0 = time.monotonic()
    with log.open("w") as fh:
        try:
            rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            fh.write(f"\n[aero] TIMEOUT after {timeout}s\n")
            rc = 124
    return ContainerResult(rc, log, script, time.monotonic() - t0)
