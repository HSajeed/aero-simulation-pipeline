# CLAUDE.md — aero-pipeline

Automated CFD pipeline: OpenVSP → OpenFOAM (snappyHexMesh + simpleFoam) with
verification gates, correction loops, a run database, and a human review queue
(Tier 3). Full plan and phase acceptance criteria: `PLAN.md`. Work one phase at
a time. Guiding principle: **keep it lean** — two external tools (OpenVSP,
OpenFOAM in a Podman container), few Python dependencies, no framework where ~100 lines of
our own code will do.

## Environment
- Native Ubuntu 26.04 LTS (bare metal) on a Lenovo Legion laptop: i7-9750H
  (6 cores / 12 threads), 14 GB RAM, GTX 1650 (useless for OpenFOAM). CFD is
  CPU-bound: keep half-model meshes at ~0.3–1 M cells.
- Python: **system Python (`/usr/bin/python3`, 3.14) + plain venv** at `.venv/`
  (`/usr/bin/python3 -m venv .venv`; needs apt `python3-venv`). Use the full
  path: a conda env may shadow `python3` on PATH. The OpenVSP .deb bindings are
  built against the system Python, so this is an exact match. No conda.
  Packages via pip from `pyproject.toml`.
- OpenVSP: official Ubuntu 26.04 `.deb`, pinned version; Python packages
  pip-installed from its `python/` folder into `.venv`. Source build only as a
  fallback. If `import openvsp` fails, stop and report — do not substitute
  another geometry tool.
- OpenFOAM ESI v2412 in a container (`docker.io/opencfd/openfoam-default:2412`,
  full name, pinned by digest), run with **rootless Podman** (5.7, installed;
  Docker not installed). Called via `src/aero/foam/container.py`: one
  `podman run --rm` per stage that executes a generated shell script in the
  mounted case dir. Use `--userns=keep-id` so files stay owned by the host user
  (never `-u $(id -u)` under rootless Podman). Engine is configurable
  (`container_engine: podman | docker`) so CI/others can use Docker. Never
  assume OpenFOAM is on the host PATH.
- Meshing: `blockMesh` + `surfaceFeatureExtract` + `snappyHexMesh` (inside the
  OpenFOAM container), from the STL exported by OpenVSP. No Gmsh in v1.
- Parallelism: **case-level** (N independent single-core cases at once, N from
  config). No MPI/decomposePar in v1.

## Non-negotiable rules
1. **Never change physics silently.** The pipeline may auto-adjust *numerical*
   settings (mesh refinement levels, snapping/feature settings, relaxation,
   schemes, iteration counts). It must NEVER auto-change *physical* settings
   (turbulence model, BC types, freestream, alpha values, viscosity,
   compressibility, steady/unsteady). Physical changes = human decision.
2. **Every automatic correction is logged** to the run's `corrections.jsonl`
   (gate, failure code, action, old value, new value, attempt number).
3. **Retry budget: max 3 correction attempts per gate.** Then mark the run
   `UNRECOVERABLE` with a failure code. Never loop forever.
4. **Gates decide on numbers, never on images.** Images are for reports only.
5. **Never fabricate or hand-edit results.** All numbers in reports come from
   the run database or solver output files.
6. **Never delete run directories.** Failed runs are training data.
7. Every stage is **idempotent** and resumable: rerunning a stage on a finished
   run is a no-op unless `--force`.
8. Thresholds live in `configs/gates.yaml`, not hard-coded.

## Physics v1 (fixed; changes are human decisions)
- Incompressible, steady `simpleFoam`, laminar with small kinematic viscosity
  (inviscid approximation), **slip** walls.
- Half model: `symmetryPlane` at y = 0.
- Outer boundary: `freestreamVelocity` / `freestreamPressure` on all farfield
  faces, so alpha is set by the freestream vector — same mesh for every alpha.
- Two alphas per variant (values in study config); the second alpha is
  initialized from the converged first.
- forceCoeffs with `rho rhoInf; rhoInf 1;` and half-model reference area.

## Code conventions
- Package: `src/aero/`. CLI: Typer, entry point `aero`. `aero doctor` checks the
  environment (container engine, image, `import openvsp`, paths).
- Core deps only: openvsp, pydantic, pyyaml, typer, jinja2, numpy, scipy,
  matplotlib (+ pytest). Optional extras: `ml` (pandas, scikit-learn),
  `viz` (pyvista). Ask before adding anything else.
- Config: YAML validated by pydantic v2 models in `src/aero/config.py`. Each run
  stores a frozen copy of its resolved config.
- Run DB: SQLite (stdlib `sqlite3`) at `runs/runs.db` via `src/aero/db.py`. The
  DB is the single source of truth; `status.json` is a write-only mirror.
  Schema changes need a migration function (`PRAGMA user_version`), not a
  dropped table.
- OpenFOAM case files: Jinja templates in `templates/openfoam/`.
- One directory per run: `runs/<study>/<run_id>/` with subfolders
  `geom/ mesh/ case/ report/` plus `status.json`, `corrections.jsonl`,
  `config.yaml`. The pilot is the first N iterations of the real case in
  `case/`, not a separate run.
- Parsers for all OpenFOAM text output (surfaceCheck, snappyHexMesh, checkMesh,
  solver logs, postProcessing `.dat` files) live in `src/aero/foam/parse.py`
  and have unit tests using saved real fixtures in `tests/fixtures/`.
- Tests: pytest. Each phase adds tests; `pytest -q` must pass before commit.
- Small commits, one logical change each, message prefixed with phase: `P3: ...`.

## Run status state machine
PENDING → GEOM_OK | GEOM_FAIL → MESH_OK | MESH_FAIL → PILOT_OK | PILOT_FAIL
→ PROD_DONE | PROD_FAIL → CHECKS_PASS | FLAGGED → (human) ACCEPTED | REJECTED
Any *_FAIL after retry budget → UNRECOVERABLE.
FLAGGED runs wait in the review queue; the pipeline never auto-accepts them.

## When unsure
Stop and ask. Especially: physics choices, threshold values, anything that would
make a failed run look like a passed one.
