# Phase report — Phases 0–6

## Phase 0 — environment spike (d4ec268, 2026-11-23)

Smoke: `.venv/bin/python scripts/smoke.py` → exit 0. Output in
`runs/_smoke/<timestamp>/` (`smoke.log`, `smoke.json`, all stage logs).
Reference run: `runs/_smoke/20261123-200119/`.

## Versions / installs
| Item | Result |
|---|---|
| System Python | 3.14.4 (`/usr/bin/python3`), venv at `.venv/` — OK |
| `pip install -e .[dev]` | OK (pydantic 2.14.4, PyYAML 6.0.4, typer 0.29.2, Jinja2 3.1.6, numpy 2.5.5, scipy 1.18.1, matplotlib 3.11.2, pytest 9.2.0) |
| OpenVSP | 3.57.0 `.deb` (already installed, `/opt/OpenVSP`). `_vsp.so` links `libpython3.14.so.1.0` → exact match. Installed `openvsp`, `openvsp_config`, `utilities`, `degen_geom` from `/opt/OpenVSP/python` (copied out first; `/opt` is root-owned). `import openvsp` → `OpenVSP 3.57.0` — OK. No source build needed. |
| Podman | 5.7.0, rootless — OK. Docker not installed. |
| OpenFOAM image | `docker.io/opencfd/openfoam-default:2412`, pinned as `@sha256:1ba02114b1c025c370f2e269a07677c16c9bea8d990fcd75ac8378aff9d41b50` (repo digest; local image ID `5c64f40b9d1f`) — OK |

## Verified facts (inside the image)
- bashrc: `/usr/lib/openfoam/openfoam2412/etc/bashrc` (`WM_PROJECT_VERSION=v2412`).
- **Image entrypoint `/openfoam/run` ignores `-w` and cd's to `$HOME`.**
  `container.py` therefore uses `--entrypoint bash` and sources the bashrc itself.
- `--userns=keep-id`: case files are owned by host uid 1000 — verified (`host_ownership_ok: true`).
- Turbulence dict: **`turbulenceProperties`** (all 15 simpleFoam tutorials; `momentumTransport` not used in ESI v2412).
- `freestreamVelocity`, `freestreamPressure` present and load fine (3-iteration simpleFoam probe, no warnings).
- forceCoeffs output: **`postProcessing/<fo-name>/0/coefficient.dat`**, columns
  `Time Cd Cd(f) Cd(r) Cl Cl(f) Cl(r) CmPitch CmRoll CmYaw Cs Cs(f) Cs(r)`;
  `rho rhoInf; rhoInf 1;` + `liftDir/dragDir/pitchAxis/CofR` syntax accepted without deprecation warnings.
- solverInfo output: `postProcessing/<fo-name>/0/solverInfo.dat`.
- Available: `surfaceCheck`, `blockMesh`, `surfaceFeatureExtract`, `snappyHexMesh`, `checkMesh`,
  `simpleFoam`, `potentialFoam`, `foamDictionary`, `surfaceMeshExtract` (not `surfaceMeshTriangulate`).
- `surfaceCheck` only tests self-intersection with `-checkSelfIntersection`.

## Smoke result (reference run)
- Geometry: fuselage L=10 m + wing (half span 5, c_root 2, c_tip 1, sweep 10°), CompGeom → STL,
  4848 triangles, area 94.004 m² (= CompGeom total wetted area).
- surfaceCheck: closed, 1 part, 1 consistent-normal zone, no illegal triangles;
  **self-intersecting at 23 locations**; 2.1% of triangles have quality < 0.05 (min 1e-5).
- Mesh: half box x[-25,45] y[0,30] z[-30,30], base 2.5 m, surface level (5 6), features 6, near-body box level 3.
  **114,674 cells**, checkMesh **Mesh OK** (max non-orth 45.1, max skewness 1.92, max AR 6.36).
  Patches: `symmetry`, `farfield`, `body`.
- Mesh wall time (blockMesh + sFE + sHM): **23.8 s** (sHM 22.7 s, 1 core). Total smoke 26.1 s.
- Snapped wetted area 46.955 m² vs STL/2 47.002 m² → **ratio 0.9990**.

## Phase 0 — open issues and how they were resolved
| # | Issue | Resolution |
|---|---|---|
| 1 | CompGeom STL self-intersections (23 in smoke run) | Kept as a warn-only metric in Gate 0 (human decision); 7–45 per baseline run. |
| 2 | Single STL solid gives one `body` patch | Resolved in P3: STL has solids `wing` and `fuselage`, giving patches wing, fuselage, symmetry, farfield. |
| 3 | 115k cells below the 0.3–1 M target | Resolved by human decision: `min_cells` = 50k (sanity floor; resolution to be set by the Phase 7 GCI study). Baseline meshes 78.5k–131k cells. |
| 4 | Half model from full symmetric STL clipped at y=0 | Works; area ratios 0.9977–0.9988 over the baseline runs. |

Real outputs saved as parser fixtures in `tests/fixtures/phase0/`.

## Phase 1 — skeleton (fde869f)
Built: `config.py` (pydantic v2, physics v1 Literal-locked), `db.py` (SQLite, `PRAGMA user_version`
migrations), state machine (`PROD_FAIL` added by human decision), `runner.py` (at most 3 numerical
corrections per gate, replayed from the DB so runs are resumable; physics-change refusal with
`PHYSICS_CHANGE_REFUSED`; case-level process pool), `cli.py` (`init-study`, `run`, `status`, `doctor`),
`doctor.py`, `status.py`, LHS sampling (seed 42), `configs/gates.yaml`, `configs/study_baseline.yaml`.

Acceptance: `init-study` creates 10 PENDING runs; `status` lists them. 37 tests at commit.

## Phase 2 — geometry, Gate 0, VSPAERO (3c91dcb)
Built: `geom/openvsp_build.py` (params to wing + fuselage, CompGeom STL, analytic Sref/MAC/x_ref at 0.25 MAC),
`geom/vspaero.py` (wing-only VLM reference), `geom/stage.py`, surfaceCheck parser in `foam/parse.py`,
`gates/gate0.py`, `corrections/rules.py` (tessellation-refinement corrections).

Gate 0: closed, illegal triangles = 0, tip chord >= 12 finest surface cells; self-intersections and
triangle quality warn-only.

Acceptance: all 10 baseline runs GEOM_OK. VSPAERO CL-alpha slope 4.21–5.00 /rad. 58 tests at commit.

Bugs found and fixed:
- VSPAERO `nan` was stored as a valid reference. Non-finite values or a panel-error log are now a failure.
- Wing+body VSPAERO had invalid panels in 8/10 runs and unusable CL in 3/10 (one finite garbage CL = -57116).
  Switched to wing-only (human decision).
- `--force` overwrote logs. Logs are now never overwritten (`.1`, `.2` suffixes).

## Phase 3 — mesh and Gate 1 (05300d4)
Built: Jinja templates in `templates/openfoam/mesh/` (blockMesh half box 15 chords upstream / 25 downstream /
15 lateral / 15 vertical, surfaceFeatureExtract, snappyHexMesh with near-body and wake boxes, `addLayers false`),
`foam/mesh_stage.py`, `foam/case.py`, checkMesh and snappyHexMesh parsers, `gates/gate1.py`, per-code
correction sequences.

Gate 1: non-orth < 70, skewness < 4, no negative volumes, closed boundary, 50k–1M cells,
wetted-area ratio >= 0.98.

Acceptance: all 10 baseline runs MESH_OK.

| Metric | Range over 10 runs |
|---|---|
| Cells | 78.5k–131k |
| Max non-orthogonality | 47.2–65.0 |
| Max skewness | 2.02–3.50 |
| Wetted-area ratio | 0.9977–0.9988 |
| Mesh time (single core) | 20–42 s |

Corrections: runs 0003 and 0007 hit SKEWNESS_HIGH (6.9), fixed by `n_smooth_patch` 3 -> 5.
Initial confirmed mesh settings gave ~79k cells against the then-minimum of 300k; 3 corrections could not
close the gap, so the human lowered `min_cells` to 50k. 80 tests at commit.

Bugs found and fixed:
- OpenVSP tagged exports (multi-solid STL and Cart3D `.tri`) drop ~1e-7 m2 junction slivers, leaving the
  surface open. Geometry now comes from the closed single-solid export, with triangles labelled from the
  tagged export. Runs 0007/0009/0010 had received a tessellation x1.5 correction before this was found; kept as history.
- Template log-name bug (`log.*.aa0`).
- Stale snapped-surface files could be read after a crash; they are now deleted first.

## Phase 4 — case, pilot, Gate 2 (c618190)
Built: solve templates in `templates/openfoam/solve/` (U: slip walls / symmetryPlane / freestreamVelocity;
p: zeroGradient walls / freestreamPressure; laminar; forceCoeffs with `rho rhoInf; rhoInf 1;`, half-model Aref,
lift/drag directions rotated by alpha; solverInfo), potentialFoam `-writep` initialisation,
`foam/pilot_stage.py` (pilot = first 300 iterations at alpha 0 in `case/`; each attempt restarts from
iteration 0 with previous outputs archived to `case/attempts/`), per-iteration history table in the DB,
`gates/gate2.py`, forceCoeffs/solverInfo parsers.

Gate 2: setup error never auto-fixed, crash, NaN, |coeff| <= 2.0, residual drop >= 2 orders,
CL change over the last 100 iterations < 5% with |CL| floor 0.05.

Acceptance: all 10 baseline runs PILOT_OK.

| Metric | Result |
|---|---|
| Residual drop | 2.75–6.63 orders |
| CL change (last 100 its) | 2e-5–6e-4 |
| Time per 100 iterations | 35–66 s with 4 concurrent cases (~20 s alone) |

Corrections: run 0005 needed one (RESIDUAL_DROP_LOW, pilot iterations 300 -> 500).
Broken-BC integration test (farfield entry removed): CASE_SETUP_ERROR, zero corrections, BC left as written.
101 tests at commit.

Bugs found and fixed:
- FPE startup banner "Floating point exception trapping enabled" was parsed as a crash.
- Log glob for tag `a0` also matched `a0.1`.
- `fvSchemes` was missing `div(div(phi,U))` for potentialFoam (correctly classified as a setup error).

Observation: inviscid slip-wall CD of 0.007–0.010 at alpha 0 is numerical drag, not physical.

## Phase 5 — production, extraction, Gate 3
Built: `foam/prod_stage.py` (α₁ continues the pilot case to the residual target 1e-5 or 2000 iterations;
α₂ warm-started from the converged α₁ field with the farfield freestream vector switched by
`changeDictionary`; α₁'s final fields archived to `case/attempts/`), farfield mass-balance function objects
(`surfaceFieldValue` sum / sumMag of phi), `results` table extended (DB v4: std, mass imbalance, converged
flag, iteration range), `gates/gate3.py` + `gates/checks_stage.py` (PROD_DONE -> CHECKS_PASS | FLAGGED, no
retries, all violated checks listed), `prod` correction rules (relaxation / non-orth correctors).

Gate 3: std(CL) over the last 200 its <= 1e-3; mass imbalance <= 1e-3 of inflow; |CL - CL_VSPAERO| /
max(|CL_VSPAERO|, 0.05) <= 0.25; CL-alpha slope 3.0–6.0 /rad; CD > 0; Cm and dCm/dalpha warn only.
Each alpha runs >= 400 iterations from its own initial condition before the residual stop may end it.

Acceptance: all 10 baseline runs CHECKS_PASS; each has results and a Gate 3 verdict in the DB.
Production wall time for all 10 runs: 19.7 min with 4 workers.

| Metric | Range over 10 runs |
|---|---|
| CL at 4°, CFD vs VSPAERO | 0.7–8.3 % (CFD higher on every run) |
| CL at 0°, floored difference | 0.025–0.233 (limit 0.25) |
| CL-alpha slope | 4.45–5.20 /rad |
| std(CL), last 200 its | 6e-8–1.8e-5 |
| Farfield mass imbalance | 3e-11–1e-8 |
| CD | 0.0069–0.0181 (numerical drag, slip walls) |
| α₁ iterations | 401–1976 |
| α₂ iterations | 401–402 |

Bugs found and fixed (scratch runs, before production on `runs/`):
- α₁ statistics used production rows only: a run whose pilot had already converged produced one row and
  failed as MISSING_OUTPUT. α₁ now uses pilot + production (one continuous solution).
- α₂ met the residual target after ~130 iterations, so the 200-iteration window contained the start-up
  transient: false COEFF_NOT_CONVERGED flag and a wrong reported CL (0.2818 instead of 0.3075). Fixed by
  the 400-iteration minimum leg (human decision).
- α₂ retry after a partial leg would have taken the wrong origin; origin now comes from α₁'s end iteration
  in the DB.
- `foamDictionary` cannot edit binary fields; switched to `changeDictionary` for the alpha switch.

Observation: no baseline run failed or was flagged at any gate after Phase 3; with sweep 0–10° there is
no aggressive corner. See decisions.md, open items.

## Phase 6 — reports and Tier 3 review queue (2026-11-24)
Built: `report/plots.py` (matplotlib: residual history, CL/CD history with the Gate 3 window shaded,
CFD vs VSPAERO CL bars, gate margins log10(value/threshold) coloured by the DB pass flag),
`report/html.py` + `templates/report/run.html.j2` (one self-contained HTML per run, PNGs embedded,
every number read from the DB: params, reference values, results, flag/failure reasons with exceedance,
per-gate metrics vs thresholds with earlier attempts, corrections, review decisions),
`review/queue.py` (severity ranking, transactional FLAGGED -> ACCEPTED | REJECTED + reviews row),
`configs/review.yaml` (fixed label vocabulary), CLI: `aero report <study> [--run]`,
`aero review list <study>`, `aero review open <run>`, `aero review decide <run> --accept|--reject
--label L [--note] [--reviewer]`. 129 tests (14 new, synthetic DB in tmp_path).

Severity order in `review list`: violated-check count, then worst check type, then worst exceedance
(decisions V2–V4).

### Harsh study (to produce flagged runs, decision V1)
`configs/study_harsh.yaml`: span 6–18 m, sweep 0–40°, taper 0.15–1.0, washout 0–8°, fineness 5–12,
n = 20, seed 42; everything else as baseline. Dry run of geom + mesh in a scratch runs-dir first (E5),
then the real run: 27 min wall time with 4 workers. Log: `runs/_logs/harsh_run.log`.

| Outcome | Runs |
|---|---|
| GEOM_FAIL -> UNRECOVERABLE, FEATURE_TOO_SMALL (taper 0.163, 0.211; no correction by design, G10) | 2 |
| MESH_FAIL -> UNRECOVERABLE, SKEWNESS_HIGH after 3 corrections | 9 |
| CHECKS_PASS (2 of them after one `n_smooth_patch` correction) | 9 |
| FLAGGED | **0** |

Gate 1: 29 SKEWNESS_HIGH corrections over 11 runs; 2 recovered. Final skewness of the failed runs
mostly 4.1–5.2 (limit 4); worst 13.7 (sweep 38.9°). Step 3 of the sequence (`n_relax_iter` 5 -> 10)
left cell count and skewness unchanged in most runs (open item).

Converged runs (9): CL vs VSPAERO floored difference 0.002–0.047 at α = 0 and 0.027–0.202 at 4°
(limit 0.25; worst run 0006, washout 7.9°); CL–alpha slope 3.72–5.04 /rad; std(CL) <= 2.6e-5;
mass imbalance <= 1.1e-8; CD 0.0058–0.019 (numerical drag).

### High-AoA study (to produce flagged runs, decision P6-5)
`configs/study_high_aoa.yaml`: the baseline design points (same ranges, seed 42, n = 10) at alphas
**4° and 20°** (pilot / Gate 2 at 4°). Scratch trial of one variant first (E5), then the real run:
started 2026-11-24, paused by the user after 2 runs and resumed 2026-11-25 (`aero run high_aoa`;
runner killed before the containers, so no false PROD_FAIL; the interrupted 20° legs of 0003–0006
restarted from the stored 4° solution). Log: `runs/_logs/high_aoa_run.log`.

Outcome: **10/10 FLAGGED**, every run for the same three reasons
(COEFF_NOT_CONVERGED, CL_VS_VSPAERO, CL_ALPHA_SLOPE). No geometry, mesh, pilot or production failures;
2 mesh corrections (SKEWNESS_HIGH, `n_smooth_patch` 3 -> 5, runs 0003 and 0007). Cells 88.6k–146k.

| Metric | 4° | 20° |
|---|---|---|
| CL CFD | 0.194–0.324 | 0.651–0.815 |
| CL VSPAERO (linear VLM, wing only) | 0.181–0.303 | 1.33–1.65 |
| CL vs VSPAERO (floored, limit 0.25) | 0.007–0.082 (pass) | 0.46–0.58 (flag) |
| std(CL), last 200 its (limit 1e-3) | 2e-6–3.5e-5 (pass) | 0.017–0.043 (flag) |
| CD (numerical) | 0.011–0.018 | 0.23–0.27 |
| Iterations | 401 | 2000 (max, residual target not met) |
| Mass imbalance | | <= 1.6e-6 (pass) |

CL–alpha slope (4°–20°): 1.17–1.91 /rad (band 3.0–6.0, flag). Wall time of the 20° leg: 14–27 min
per run with 4 concurrent cases.

Observation (for the reviewer, not a conclusion): in every run the 20° leg first reaches CL ≈ 1.5
(close to the linear VLM) within ~50 iterations, then collapses to CL ≈ 0.7 and oscillates, with CD
jumping to ~0.25. Consistent with spurious (numerical) separation in the inviscid slip-wall solver,
e.g. at the sharp leading edge; physics v1 has no viscosity, so this is not a stall prediction.
All 10 flags are qualitatively identical, so this review set has little diversity.

### Additions after the first review discussion (2026-11-25)
- **Cutting-plane contours** (V18): `aero sample <study>` (postProcess `surfaces`, cuttingPlane at
  30 % / 70 % semi-span, U and p, ~1 s per run, no solve); reports show u·e∞/U∞ (reversed flow) and Cp
  per alpha. All 29 finished runs sampled. At 4° flow is attached; at 20° every high_aoa run shows
  reversed flow over the whole upper surface from just behind the LE, resolved by fairly coarse cells
  (~0.1–0.15 m against a 2 m chord).
- **Failure labels** (V17): `aero review failures <study>`, `aero review label <run> --label L` for
  UNRECOVERABLE runs (status unchanged).
- 140 tests (25 in `tests/test_review.py`), new fixture `tests/fixtures/phase6/` (real plane VTK + log).

### Acceptance
- Reports: written for all 40 runs (baseline, harsh, high_aoa), including failed runs.
- Review CLI end to end: verified by tests on a synthetic DB (list ordering, open, decide, bad/missing
  label, double decision, non-flagged refusal, status mirror, report shows the decision).
- "Review 10 flagged runs end to end": **10 real FLAGGED runs are now in the queue**
  (`aero review list high_aoa`). **Met (2026-11-25):** all 10 reviewed via `aero review decide`:
  REJECTED, label `separated_flow` (decision V20); every decision is in the `reviews` table.
  Still pending (not part of the acceptance): failure labels for the 11 harsh UNRECOVERABLE runs (P6-6).

## Status
Phases 0–6 done (Phase 6 acceptance met 2026-11-25). Pending human item: failure labels for the
11 harsh UNRECOVERABLE runs (P6-6). Next: Phase 7 (validation and mesh convergence; includes P6-3).
