# PLAN.md — Automated, self-verifying CFD pipeline for aircraft configurations

## Goal
Run N parametric aircraft variants through OpenVSP → OpenFOAM (snappyHexMesh +
simpleFoam) **with no manual steps**, catch and correct failures at gates before
expensive runs, and route physically suspicious results to a human review queue
(Tier 3 — me). Then use the logged runs to train a failure classifier and a
surrogate.

## Headline result to aim for (README, one figure)
"N variants, 0 manual steps until review. X% failed at Gate 0/1/2 and Y% were
auto-corrected. Z% of converged runs were flagged physically suspicious; on
review W% were genuinely wrong. Classifier predicts failure before solving with
precision P. Surrogate error E% in-distribution vs E2% out-of-distribution."

## Scope decisions (v1)
- **Configuration:** wing–body (+ optional horizontal tail), low-speed, M < 0.3.
- **Physics v1:** incompressible, steady, `simpleFoam`, laminar with small ν,
  **slip walls** (inviscid approximation), half model with `symmetryPlane`.
  Outer boundary `freestreamVelocity` / `freestreamPressure` on all farfield
  faces. No boundary-layer mesh in v1.
- **Alphas:** two per variant (e.g. 0° and 4°, set in study config). Same mesh;
  alpha set by the freestream vector; second alpha warm-started from the first.
- **Physics v2 (Phase 11):** snappyHexMesh `addLayers` + Spalart–Allmaras with
  wall functions. Separate stage, not mixed into v1.
- **Plausibility reference:** OpenVSP **VSPAERO** (vortex lattice) on the same
  geometry, run automatically — a cheap oracle for Gate 3.
- **Parameters (5):** span, sweep (LE), taper ratio, twist (tip washout),
  fuselage fineness ratio. Ranges include a deliberately aggressive corner
  (high taper + high sweep) to generate real failures.

## Lean-build decisions (2026-11-23)
Chosen to minimize moving parts; see CLAUDE.md for the binding rules.
- **Two external tools only:** OpenVSP (geometry, STL, VSPAERO) and OpenFOAM
  ESI v2412 in a container (surface checks, meshing, solving).
- **Rootless Podman** (already installed) instead of Docker: no daemon, no
  sudo. `--userns=keep-id` for file ownership; engine configurable so Docker
  still works (CI, other users).
- **Meshing with snappyHexMesh, not Gmsh:** works directly from a watertight
  STL (OpenVSP CompGeom), no CAD Booleans, no format conversion, half model is
  just a blockMesh box with y ≥ 0, hex-dominant cells, prism layers built in for
  v2. Gmsh may return later only as a comparison.
- **Python:** system Python 3.14 + plain venv (`.venv/`), an exact match for the
  OpenVSP .deb bindings; deps via pip from `pyproject.toml`. ~8 core deps.
  Dropped: conda, pythonocc-core, gmsh, trimesh, foamlib.
- **Pilot = first ~300 iterations of the real case**; on Gate 2 pass the same
  case continues to convergence (no recomputation).
- **Case-level parallelism** (N single-core cases concurrently), no MPI in v1.
- **Custom runner** (state machine + retries, ~100–200 lines) instead of
  Snakemake/Prefect; pydantic v2 + YAML instead of Hydra; stdlib sqlite3 with
  `PRAGMA user_version` migrations instead of an ORM.
- **Residuals from the `solverInfo` function object** (`.dat` files); log
  parsing only as fallback.
- **Reports start with matplotlib + Jinja HTML**; PyVista 3D views optional.

## Repository layout
```
aero-pipeline/
  CLAUDE.md  PLAN.md  README.md  pyproject.toml
  configs/   study_baseline.yaml  gates.yaml  corrections.yaml
  templates/ openfoam/ (Jinja: mesh/ = blockMesh, sHM, surfaceFeatureExtract;
                        solve/ = 0/, constant/, system/; *.sh stage scripts)
  scripts/   smoke.py
  src/aero/  cli.py config.py db.py runner.py doctor.py
             geom/ (openvsp_build.py, vspaero.py)
             foam/ (container.py, case.py, parse.py)
             gates/ (gate0.py gate1.py gate2.py gate3.py)
             corrections/ (rules.py)
             report/ (plots.py, html.py)
             review/ (queue.py)
             ml/ (features.py classifier.py surrogate.py ood.py)
  runs/  tests/ (fixtures/)  notebooks/
```

---

## Phase 0 — Environment spike (do first, timebox 1 day)
Biggest risk is installs and the geometry→mesh handoff. Prove both first.
**Tasks**
- `git init`; `.gitignore` (`.venv/`, `runs/`, caches).
- `/usr/bin/python3 -m venv .venv` + `pip install -e .[dev]`.
- OpenVSP: download the pinned Ubuntu 26.04 `.deb`, confirm which Python its
  bindings target, pip-install its `python/` packages into `.venv`, confirm
  `import openvsp` and the version. If the Python versions don't match, try the
  headless source build (`VSP_NO_GRAPHICS=ON`, against `.venv`'s Python). If
  that fails too: stop and report.
- `podman pull docker.io/opencfd/openfoam-default:2412`; record the image
  digest. Write `foam/container.py`: runs a script in a mounted case dir
  (`podman run --rm --userns=keep-id -v case:/case -w /case ...`), returns exit
  code + log path; engine from config. Verify host file ownership after a run.
- Inside the image, verify (don't assume): bashrc path, turbulence dict name
  (`momentumTransport` vs `turbulenceProperties`), forceCoeffs output file name,
  availability of `freestreamVelocity`/`freestreamPressure`, `surfaceCheck`.
- Smoke script: hard-coded **wing + fuselage** in OpenVSP → CompGeom → STL →
  `surfaceCheck` → `blockMesh` (half-model box) + `surfaceFeatureExtract` +
  `snappyHexMesh` → `checkMesh` OK. Record cell count, mesh wall time, and
  snapped-vs-STL wetted area.
**Acceptance:** `python scripts/smoke.py` completes end to end; log saved; a
short report of what worked, what failed, versions, and the verified facts above.
**Prompt to Claude Code:** "Read CLAUDE.md and PLAN.md. Do Phase 0 only. Report
exactly which installs worked, which failed, and the smoke-test output."

## Phase 1 — Skeleton: config, run DB, runner, CLI
**Tasks**
- pydantic models for study config (parameters, ranges, sampling, freestream,
  alphas, mesh settings, solver settings, worker count) and gates config.
- SQLite schema: `runs` (id, study, params JSON, status, timestamps),
  `gate_results` (run, gate, metric, value, threshold, pass),
  `corrections` (run, gate, code, action, old, new, attempt),
  `results` (run, alpha, CL, CD, Cm, iterations, wall_time),
  `reviews` (run, decision, label, note, reviewer, time).
  Migrations via `PRAGMA user_version`.
- Runner: state machine from CLAUDE.md; each stage implements
  `run(run) → StageResult`, `check(...) → GateResult`, `correct(...) → change`;
  runner loops ≤ 3 attempts; idempotent; resumable. Worker pool for N cases.
- CLI: `aero init-study`, `aero run <study> [--run ID] [--stage S] [--force]`,
  `aero status <study>`, `aero doctor`.
- Latin hypercube sampling (`scipy.stats.qmc`) with fixed seed.
**Acceptance:** `aero init-study configs/study_baseline.yaml` creates 10 PENDING
runs; `aero status` lists them; tests for state transitions pass.

## Phase 2 — Geometry stage + Gate 0
**Tasks**
- `openvsp_build.py`: params → OpenVSP model → CompGeom → STL (half model or
  full, clipped by the mesh box) + `.vsp3`; extract reference area (half), span,
  MAC for coefficients.
- Gate 0: `surfaceCheck` in the container on the STL (closed, self-intersections,
  degenerate/sliver triangles, min triangle quality) parsed by `parse.py`; plus
  min feature size (e.g. tip chord, TE thickness) vs planned min cell size.
- `vspaero.py`: run VSPAERO at the study's alpha values; store CL, CDi, Cm as
  the plausibility reference.
- Gate 0 corrections (corrections.yaml): only *safe* ones (e.g. re-export with
  finer tessellation). Geometry that fails checks → `GEOM_FAIL` with code;
  never "heal" by changing the design parameters.
**Acceptance:** 10 runs processed; failure codes recorded for any bad variant;
VSPAERO results in DB; tests with one good and one deliberately bad geometry.

## Phase 3 — Meshing + Gate 1
**Tasks**
- Templates: `blockMeshDict` (half-model box y ≥ 0, ≥ 15 chords upstream,
  ≥ 25 downstream, sized from geometry bounds), `surfaceFeatureExtractDict`,
  `snappyHexMeshDict` (surface refinement levels, feature-edge level, LE/TE
  and tip refinement, wake refinement box, `addLayers false` in v1).
- Patches: `wing`, `fuselage` (slip walls), `symmetry` (`symmetryPlane`),
  `farfield` (all other box faces). Types set by templates — no fix-up step.
- `parse.py`: checkMesh → max non-orthogonality, max skewness, max aspect
  ratio, negative volumes, open boundaries, cell count; snappyHexMesh log →
  snapping success, cell count; snapped wetted area vs STL area.
- Gate 1 thresholds (gates.yaml): non-orth < 70, skewness < 4, no negative
  volumes, closed boundary, cell count within budget, wetted-area ratio ≥ ~0.98.
- Correction rules (numerical only): poor snapping / area ratio low → raise
  surface or feature refinement level; high non-orth/skewness → adjust snap
  tolerance / smoothing iterations / `nCellsBetweenLevels`; cell count over
  budget → lower farfield/wake refinement. Max 3 attempts, all logged.
**Acceptance:** all Gate-0-passing runs either reach MESH_OK or MESH_FAIL with a
code; corrections.jsonl populated where applied; parser unit tests on saved
real outputs.

## Phase 4 — Case generation + Gate 2 (pilot)
**Tasks**
- Jinja templates: `0/` (U, p: slip walls, `symmetryPlane`,
  `freestreamVelocity`/`freestreamPressure`), `constant/` (transportProperties,
  turbulence dict `laminar` — file name as verified in Phase 0), `system/`
  (controlDict with forceCoeffs and solverInfo function objects, reference
  values from Phase 2; fvSchemes; fvSolution).
- Pilot: `potentialFoam -writep`, then `simpleFoam` for ~300 iterations on the
  first alpha, in `case/`.
- Gate 2 checks: no crash/NaN, initial residuals drop ≥ 2 orders, forces
  bounded, CL change over last 100 iterations < 5%.
- Corrections (numerical only): lower relaxation factors; first-order upwind
  start-up then switch; more non-orthogonal correctors. Restart from iteration
  0. Max 3 attempts.
- Measure wall time per 100 iterations — this sizes the batch study.
**Acceptance:** pilot outcome + residual history in DB for every MESH_OK run;
deliberately broken case (bad BC) is caught and NOT auto-fixed (physics rule).

## Phase 5 — Production run + extraction + Gate 3 (automatic part)
**Tasks**
- Production: continue the pilot case to convergence (residuals or max
  iterations); then second alpha warm-started from the first. Runs are executed
  by the worker pool (N single-core cases at once).
- Extract: CL, CD, Cm per alpha, convergence history, wall time; mass-flux
  imbalance over the farfield patch.
- Gate 3 automatic checks → set CHECKS_PASS or FLAGGED, with reasons:
  - force coefficients converged (std over last N iters below threshold)
  - mass imbalance < 1e-3 of inflow
  - |CL_CFD − CL_VSPAERO| / |CL_VSPAERO| within band (start ±25%, tune later)
  - CL–alpha slope (from the two alphas) within plausible range for the AR
  - CD > 0 (careful: inviscid CD is near zero — threshold to be decided),
    Cm sign consistent with geometry
- Any single violation → FLAGGED. Pipeline never auto-accepts a FLAGGED run.
**Acceptance:** each completed run has results + Gate 3 verdict with reasons.

## Phase 6 — Reports + Tier 3 review queue (human in the loop)
**Tasks**
- `plots.py` (matplotlib): residuals, CL convergence, CFD vs VSPAERO bars,
  gate metrics vs thresholds. Optional (`viz` extra, PyVista offscreen):
  surface Cp top/bottom, slices at 30%/70% span.
- `html.py`: one self-contained HTML report per run: params, gate metrics vs
  thresholds, corrections log, images, flag reasons.
- `review/queue.py` + CLI:
  - `aero review list <study>` → FLAGGED runs, sorted by severity
  - `aero review open <run_id>` → prints report path
  - `aero review decide <run_id> --accept|--reject --label <code> --note "..."`
- Label vocabulary (fixed list, in config): `ok_false_alarm`, `mesh_too_coarse`,
  `separated_flow`, `geometry_artifact`, `not_converged`, `reference_wrong`,
  `other`. Labels go to the `reviews` table — this is supervised training data.
- Optional: decisions can trigger a re-queue with a human-specified change
  (e.g., refine wake) — logged as a human correction, distinct from auto ones.
**Acceptance:** I can review 10 flagged runs end to end from the CLI; every
decision is in the DB with a label.

## Phase 7 — Validation and mesh convergence
**Tasks**
- Baseline variant: 3-level mesh refinement study (coarse/medium/fine via
  blockMesh base size + sHM levels), Richardson extrapolation / GCI for CL, CD.
  MPI only if the fine level needs it.
- Compare CL–alpha slope against VSPAERO and lifting-line estimate for the
  baseline. Document agreement and why inviscid CD is not physical drag.
- Freeze the mesh level used for the batch study based on GCI.
**Acceptance:** `notebooks/validation.ipynb` with GCI table and plots; chosen
mesh settings written back into study config.

## Phase 8 — Batch study (the demo)
**Tasks**
- 50–100 variants, unattended. Summary report: funnel (how many passed each
  gate), corrections by type, flagged rate, my review outcomes, wall time.
- Funnel figure + CL/CD maps over parameter space.
**Acceptance:** one command produces the study summary HTML from the DB.

## Phase 9 — ML layer
**Tasks**
- `features.py`: geometry params + Gate 0/1 metrics (+ pilot metrics).
- `classifier.py`: gradient boosting predicting {fails before prod, flagged,
  passes}; grouped cross-validation; report precision/recall and CPU-hours
  that would have been saved by skipping predicted failures.
- `surrogate.py`: Gaussian process (CL, CD vs params) with uncertainty.
- `ood.py`: flag queries outside training support (Mahalanobis or GP variance
  threshold); show surrogate error inside vs outside support.
- Active learning: choose next variants by GP uncertainty vs random; compare
  error at equal run budgets.
**Acceptance:** `notebooks/ml.ipynb` with the four results above, all computed
from the DB.

## Phase 10 — Packaging
- Containerfile (Podman/Docker compatible) for the Python side (OpenVSP .deb + Python + pip deps); `make demo` runs a
  10-variant study end to end.
- README: problem, architecture diagram, gates table, headline figure, how the
  human review loop works, limitations (inviscid v1, no BL), next steps.
- GitHub Actions: unit tests on parsers/gates (no CFD in CI).

## Phase 11 (v2) — Viscous upgrade
- snappyHexMesh `addLayers`, SA + wall functions, y⁺ check added to Gate 1/3.
  Re-run a subset; compare against v1. (Optional: Gmsh mesh comparison.)

---

## Order and timeboxes (realistic)
| Phase | Timebox |
|---|---|
| 0 Environment + geometry→mesh smoke | 1 day |
| 1 Skeleton | 1 day |
| 2 Geometry + Gate 0 | 1–2 days |
| 3 Mesh + Gate 1 | 1–2 days |
| 4 Pilot + Gate 2 | 1–2 days |
| 5 Prod + Gate 3 | 1–2 days |
| 6 Reports + review | 1–2 days |
| 7 Validation | 1 day |
| 8 Batch study | 1 day + compute |
| 9 ML | 2–3 days |
| 10 Packaging | 1 day |

**Shareable checkpoint:** end of Phase 6 + one small batch (≈ 2 weeks).
That is enough to send to SoftAge. Phases 7–9 make it strong.

## Known risks
- OpenVSP Python bindings must match the venv's Python (the 26.04 .deb most
  likely targets system Python 3.14). Resolve in Phase 0 or stop.
- OpenVSP CompGeom STL quality (watertightness at the wing–body junction,
  tessellation of thin TE) — checked by Gate 0.
- snappyHexMesh resolving thin trailing edges and wing tips needs high local
  refinement, which pushes cell count; expect most Gate 1 corrections here.
- Inviscid slip-wall `simpleFoam` gives lift trends, not drag — state this
  plainly; don't oversell CD.
- Compute: laptop CPU only. Keep half-model meshes at ~0.3–1 M cells, measure
  wall time per run in Phase 4, and size the batch study from that number.
  Watch laptop thermals on long unattended batches.
- OpenVSP releases every 1–2 weeks: pin the version; pin the OpenFOAM image by
  digest.

## Rule for using Claude Code
One phase per session. Start each with: "Read CLAUDE.md and PLAN.md. Do Phase
N only. Meet its acceptance criteria. Show me test output and any decisions you
made that touch physics or thresholds before committing."
