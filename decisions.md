# Decisions log

Every decision that touches physics, thresholds, methodology or the pipeline's
behaviour, with who made it and why. **H** = human decision (asked and answered),
**A** = assistant choice shown to the human before the phase commit and accepted.
Physics changes are never automatic (CLAUDE.md rule 1); everything below that
changes physics or a threshold is an H decision.

Dates: 2026-11-23 unless stated (Phase 6: 2026-11-23/24).

---

## Environment and tooling (Phase 0)

| # | Decision | By | Why |
|---|---|---|---|
| E1 | Keep the OpenVSP 3.57.0 `.deb` installed | H (asked), A (advised) | VSPAERO ships only with it; the venv's `_vsp.so` needs `libxml2`/`libcminpack` pulled in by the `.deb`; it is the pinned source for rebuilding `.venv` and the Phase 10 Containerfile. |
| E2 | OpenFOAM image pinned by repo digest `@sha256:1ba02114…d41b50` | A | CLAUDE.md requires a digest pin; the repo digest is what `pull` resolves. |
| E3 | Container runner uses `--entrypoint bash` and sources the bashrc itself | A | Verified: the image entrypoint `/openfoam/run` ignores `-w` and cd's to `$HOME`, so scripts ran in the wrong directory. |
| E4 | Repo-local git identity (`sajied` + account email) | A | No git identity was configured; set for this repo only, not globally. |
| E5 | Debug in scratch run directories before touching `runs/` | A | Run directories are never deleted (rule 6); a bug exercised in `runs/` would leave fake failures and corrections in the real DB forever. |

## Study configuration (Phase 1)

| # | Decision | By | Why |
|---|---|---|---|
| S1 | Freestream U_inf = 10 m/s, ν = 1e-5 m²/s, rho_inf = 1 | H | Physics values; incompressible regime, "small viscosity" inviscid approximation. |
| S2 | Alphas 0° and 4° | H (PLAN default) | PLAN example; second alpha warm-started from the first. |
| S3 | Parameter ranges: span 8–14 m, sweep 0–10°, taper 0.25–1.0, washout 0–4°, fuselage fineness 6–10 | H | Sweep narrowed from the proposed 0–40° to 0–10°. Root chord 2 m and fuselage length 10 m fixed. |
| S4 | LHS sampling, n = 10, seed 42 | A | PLAN: Latin hypercube with fixed seed. |
| S5 | Solver: pilot 300 its (PLAN), max 2000 its, residual target 1e-5, relaxation U 0.7 / p 0.3, 4 workers | H | Numerical settings confirmed as proposed. |
| S6 | Physics v1 encoded as `Literal` types in config | A | Any other turbulence model / wall BC fails validation, so physics cannot drift via config or correction. |
| S7 | Automatic corrections may only touch `tessellation.*`, `mesh.*`, `solver.*` | A | Enforces rule 1 in code; anything else -> `PHYSICS_CHANGE_REFUSED`, run UNRECOVERABLE. |
| S8 | `null` threshold = "not decided" and blocks/flags (`UNDECIDED_THRESHOLD`) | A | An undecided check must never look like a pass. |
| S9 | Add `PROD_FAIL` to the state machine | H | Production can crash; without a fail state a crash would be indistinguishable from "not run yet". Retried like other `*_FAIL`, max 3, then UNRECOVERABLE. |
| S10 | Corrections are replayed from the DB to rebuild a run's effective settings | A | Makes every stage resumable after a crash; the frozen `config.yaml` + `corrections` table fully determine the settings. |

## Geometry, Gate 0, VSPAERO (Phase 2)

| # | Decision | By | Why |
|---|---|---|---|
| G1 | Self-intersections: warn only (recorded, never block) | H | Every OpenVSP CompGeom STL had them (7–45); meshing still succeeded with area ratio ~0.999. Gate 1 catches real consequences. Revisit with more data. |
| G2 | Triangle quality: warn only; illegal (degenerate) triangles must be 0 | H | snappyHexMesh does not need well-shaped STL triangles; truly broken triangles are caught by `surfaceCheck`'s illegal-triangle check. |
| G3 | Tip chord ≥ 12 finest surface cells | H | Chosen over the proposed 8; baseline minimum is 13.3. |
| G4 | VSPAERO models the **wing only** | H (asked twice) | First answer was "wing+body, no fallback" based on "2/10 runs fail". Fuller data then showed wing+body had invalid junction panels in 8/10 runs and unusable CL in 3/10 (two nan, one finite garbage CL = −57116); wing-only had zero panel errors and matched wing+body CL within 0.0002 where both worked. Decision changed to wing-only. Consequence: the reference ignores fuselage lift/moment. |
| G5 | VSPAERO failure guards: any non-finite result or a panel-error message in its log = failure; stale reference rows cleared | A | A nan had been stored as a valid reference; failures must leave no reference, so Gate 3 flags the run (`NO_REFERENCE`). |
| G6 | Wing layout: root LE at 0.35 L, wing z = −0.2 D, NACA 0010 default airfoil, root twist 0 | A | Fixed (non-sampled) geometry; low-mid wing fully intersecting the fuselage. |
| G7 | Reference values: trapezoidal planform from the centreline (half-model Aref = Sref/2), lRef = MAC, moment reference at 0.25 MAC | A | Standard convention; analytic values match OpenVSP's own `TotalArea`/`MAC`. |
| G8 | VSPAERO Mach = U_inf / 340.3 | A | Incompressible regime, consistent with the freestream. |
| G9 | No trailing-edge thickness check | A | The airfoil has a sharp TE by design (thickness 0); only tip chord vs cell size is meaningful. |
| G10 | Gate 0 corrections only re-tessellate; design parameters never changed | A (PLAN) | "Never heal by changing design parameters." `FEATURE_TOO_SMALL` has no correction. |
| G11 | Logs are never overwritten (`log.x`, `log.x.1`, …) | A | A `--force` rerun had overwritten history; failed and earlier attempts are training data. |

## Mesh, Gate 1 (Phase 3)

| # | Decision | By | Why |
|---|---|---|---|
| M1 | Mesh starting settings: base 2.5 m, surface level (5,6), feature 6, near-body 3, wake 3 | H | Confirmed in Phase 1. |
| M2 | `min_cells` lowered 300k -> **50k** (sanity floor) | H | Starting settings gave ~79k cells; 3 automatic corrections reached only 269k and then broke skewness, so every run would end UNRECOVERABLE. Offered: finer start (~600k cells, ~2 min/mesh) or a lower floor. Chosen: lower floor; mesh resolution to be fixed by the Phase 7 GCI study. |
| M3 | Gate 1 thresholds: non-orth < 70, skewness < 4, 0 negative volumes, closed boundary, ≤ 1M cells, wetted-area ratio ≥ 0.98 | H (PLAN) | PLAN values, confirmed. |
| M4 | Domain: 15 root chords upstream, 25 downstream (PLAN minimums), 15 lateral, 15 vertical | A | PLAN fixes upstream/downstream; lateral/vertical chosen symmetric. |
| M5 | Refinement: near-body box (bbox + 0.5 c), wake box (root TE -> 5 c behind body, opened by tan(max alpha)); LE via curvature, TE/tip/junction via feature edges | A | Resolves the regions that matter without a per-run hand setup. |
| M6 | Gate 1 failure priority puts TOO_MANY_CELLS before quality/area fixes | A | A refinement fix must never be chosen while over budget. |
| M7 | No correction for snappyHexMesh crash, checkMesh parse failure, open boundary | A | No safe numerical fix; these need a human. |
| M8 | Correction step = how often *this failure code* was corrected | A | Counting all gate attempts would skip a code's first step after a different code's correction. |
| M9 | STL geometry from OpenVSP's closed single-solid export, triangles labelled wing/fuselage from the tagged export | A | OpenVSP's tagged exports (multi-solid STL and Cart3D .tri) silently drop ~1e-7 m² junction slivers, leaving the surface open. Labelling keeps geometry identical and closed. |
| M10 | Runs 0007/0009/0010 keep the ×1.5 tessellation they received before M9 was found | A (flagged to H) | Correction history is permanent (rule 6); harmless and logged, but those three differ slightly from the other seven. |

## Pilot, Gate 2 (Phase 4)

| # | Decision | By | Why |
|---|---|---|---|
| P1 | CL-settled check: \|ΔCL over 100 its\| / max(\|CL\|, **0.05**) < 5% | H | The pilot runs at α = 0°, where \|CL\| is 0.01–0.12; a pure relative test is ill-conditioned near zero. Below \|CL\| = 0.05 it becomes \|ΔCL\| < 0.0025. |
| P2 | "Forces bounded": \|Cl\|, \|Cd\|, \|Cm\| ≤ **2.0** over the last 100 pilot its | H | ~4× any physical value at these alphas; catches blow-ups before they become nan. |
| P3 | Residual drop = log10(max of first 10 initial residuals / median of last 10), minimum over Ux, Uy, Uz, p | A | Robust to first-iteration normalisation quirks and last-iteration noise. |
| P4 | Schemes: linearUpwind for U, cellLimited grad(U), limited corrected 0.5, 1 non-orth corrector, SIMPLE (consistent no), potentialFoam init | A | Standard robust steady settings for meshes with non-orthogonality up to 65. |
| P5 | Case setup errors (bad/missing BC, missing dictionary entry) are never auto-corrected | A (rule 1) | Fixing them would mean changing the case/physics; verified by the broken-BC integration test. |
| P6 | Gate 2 corrections: divergence -> lower U relaxation, then first-order start-up, then +1 non-orth corrector; slow convergence -> +200 pilot its, +1 corrector, +200 its | A (PLAN) | PLAN's numerical options, in that order. |
| P7 | Each pilot attempt restarts from iteration 0; the previous outputs are moved to `case/attempts/` | A | PLAN: restart from 0; moving instead of deleting keeps failed attempts as data. |
| P8 | Inviscid CD (0.007–0.010 at α = 0) reported as numerical drag, not physical | A | Slip walls give no friction drag; PLAN: don't oversell CD. `min_cd = 0` does not catch it. |

## Production, Gate 3 (Phase 5)

| # | Decision | By | Why |
|---|---|---|---|
| R1 | Coefficient convergence: std(CL) over the last 200 its ≤ 1e-3 (absolute) | H | Absolute, because CL at α = 0 is ~0. |
| R2 | CL–alpha slope within [3.0, 6.0] /rad | H | Helmbold lifting-line estimate gives 3.85–5.25 /rad over the design space; band adds margin for fuselage effects. |
| R3 | min_cd = 0.0: negative CD is flagged | H | Negative drag is unphysical; expect some α = 0 false alarms, tune with review labels. |
| R4 | CL vs VSPAERO: ±25%, denominator floored at **0.05** | H (after an A recommendation) | Equivalent to "check only at α₂" (\|CL\| ≫ 0.05 there) plus a loose absolute check at α = 0 (\|ΔCL\| ≤ 0.0125) that catches offsets the wide slope band would miss. Human rationale: checking only at the 2nd alpha would be case tuning (tied to this study's alpha pair), not a global fix; the floor works for any alphas. Baseline: worst floored difference 0.233 (run 0004, α = 0), which pure-relative would have flagged at 83%. |
| R5 | Cm and dCm/dα: warn only, never flag | H | VSPAERO is wing-only and the moment reference is the wing's aerodynamic centre (dCm/dα ≈ 0); CFD includes the fuselage moment, so a sign comparison mostly compares noise. |
| R6 | Minimum leg length: each alpha runs ≥ 2 × window (400 its) from its own initial condition before the residual stop may end it | H | α₂ met the residual target after ~130 its, so the 200-it window contained the start-up transient: false flag **and** a wrong reported CL (0.2818 instead of 0.3075). Chosen over an adaptive window to keep the confirmed 200-it definition exact. |
| R7 | α₁ statistics use pilot + production iterations | A | One continuous solution; using production rows only failed a run whose pilot had already converged. |
| R8 | α₂ warm start: same case, farfield `freestreamValue` switched with `changeDictionary` (works on binary fields; `foamDictionary` does not); α₁'s final fields archived first | A | PLAN: same mesh, alpha via freestream vector, second alpha initialised from the first. |
| R9 | Mass imbalance = \|Σφ\| / (Σ\|φ\| / 2) on the farfield patch | A | The farfield is the only open boundary (walls slip, symmetry plane), so this is the domain balance relative to inflow. |
| R10 | Reported CL/CD/Cm = mean over the last 200 its | A | Equals the final value when converged; less sensitive than the last iteration when a run oscillates. |
| R11 | Gate 3 lists all violated checks in the failure code (comma separated) | A | The review queue should show every reason, not just the first. |

### Phase 5 outcome (for context on R1–R11)
All 10 baseline runs CHECKS_PASS: CL at 4° within 0.7–8.3% of VSPAERO (CFD consistently higher —
fuselage lift, absent from the wing-only reference), CL–alpha slope 4.45–5.20 /rad, CL std ≤ 1.8e-5,
mass imbalance ≤ 1e-8. α₂ always stops at the 400-iteration minimum (R6), so R6 sets α₂'s cost.

## Reports, review queue (Phase 6)

| # | Decision | By | Why |
|---|---|---|---|
| V1 | Flagged runs for the review acceptance come from a new **harsh** study (`configs/study_harsh.yaml`): span 6–18 m, sweep 0–40°, taper 0.15–1.0, washout 0–8°, fineness 5–12, n = 20, seed 42; everything else as baseline | H | Baseline had 0 flags. Changing thresholds or writing synthetic results into `runs/` to create flags would break rules 5/8. Also starts the decisions open item on harsher ranges. |
| V2 | Review queue severity: sort by number of violated Gate 3 checks, then worst check type, then worst normalised exceedance; all three shown | H | "Both are useful" (count/exceedance and fixed priority). |
| V3 | Check priority: MISSING_RESULTS > NO_REFERENCE > UNDECIDED_THRESHOLD > MASS_IMBALANCE > COEFF_NOT_CONVERGED > CL_ALPHA_SLOPE > CL_VS_VSPAERO > NEGATIVE_CD | A | Missing data first (nothing can be trusted), then conservation and convergence (result itself suspect), then plausibility vs references. |
| V4 | Exceedance = value / limit (slope: value/hi or lo/value); missing value = inf; CD vs a 0 limit has no scale (shown "—", ignored in sorting) | A | Direction-correct "times over the limit" for each Gate 3 family, from gate_results. |
| V5 | No re-queue from review decisions in v1 | H | Keep Phase 6 lean; revisit if review data shows a need. |
| V6 | Report figures matplotlib only; no PyVista / `viz` extra | H | No new dependency. |
| V7 | Labels in `configs/review.yaml` (PLAN list); `other` requires `--note`; any label allowed with either decision; reviewer defaults to the login name | A | Label = cause, decision = verdict; independent. A bare `other` is useless as training data. |
| V8 | Only FLAGGED runs can be decided (DB state machine, `human=True`); review row + status change in one transaction; `failure_code` (the flag reasons) kept on ACCEPTED/REJECTED runs | A | Pipeline never auto-accepts; flag reasons are features for Phase 9. |
| V9 | Reports are derived from the DB and regenerated by `aero report` / `review open` / `review decide` | A | Not results (rule 5 applies to their numbers, which are all read from the DB); regeneration keeps the review section current. |
| V10 | Gate-margin plot: log10(value / threshold) per numeric gate check, colour = the DB pass flag, clipped at ±3 decades | A | Needs no min/max knowledge per metric, so it cannot disagree with the gate. |
| V11 | Harsh study run with Phase 3 mesh correction rules unchanged although the scratch dry run gave 9/18 SKEWNESS_HIGH → UNRECOVERABLE | H | Failures are real data. Offered: n = 40, or fix the correction sequence first. Chosen: run 20 as is; any shortfall of flagged runs is completed in Phase 8. |
| V12 | High-AoA study: baseline design points (same ranges, seed, n = 10), alphas **[4, 20]**, pilot at α₁ = 4° | H (2026-11-24) | To produce real flagged cases (P6-5). Alpha values are physics; chosen by H. |
| V13 | Dashboard/UI (subagent feasibility study, 2026-11-24): no framework now. Phase 8: `aero summary` static study HTML (funnel, corrections, flags/reviews, results, parameter-space plots, links to run reports); later optional stdlib `aero serve` on 127.0.0.1 whose only write is a review form calling `queue.decide()`. No new deps. Launching studies stays CLI-only (a new study = a committed `configs/study_*.yaml`). Datasette outside the venv is fine for ad-hoc browsing | H (noted, accepted) | Small single-user data; reuses `report/html.py`, `report/plots.py`, `review/queue.py`. Rejected: Streamlit/Dash/Panel (~150–250 MB deps, estimated), MLflow/Aim (second data store, no 3.14), Grafana (cannot call `decide`). |
| V14 | P6-2 closed: Gate 3 thresholds unchanged | H (2026-11-25) | 0 flags at alphas 0°/4° is expected: easy cases, no wrong physics expected there. Not evidence that Gate 3 is too loose; the 20° runs show it does flag. |
| V15 | P6-4 accepted: add a human label for UNRECOVERABLE runs (label only, status unchanged) | H (2026-11-25) | Separates e.g. infeasible designs from mesher limitations for the Phase 9 classifier. Implementation timing and failure-specific labels: see P6-6. |
| V16 | P6-5 closed: data are enough for Phases 6–7; varied flags come from the Phase 8 batch (mixed alphas, e.g. 8–16°, plus harsher geometry) | H (2026-11-25) | All 10 high_aoa flags are one failure mode, perfectly predicted by alpha = 20, so they add little for Phase 9; the planned 50–100-run batch is the right place for variety. |

### Phase 6 outcome
Harsh study: 9 CHECKS_PASS, 9 SKEWNESS_HIGH and 2 FEATURE_TOO_SMALL UNRECOVERABLE, **0 FLAGGED**.
Across baseline + harsh, 0 of 19 converged runs were flagged; the closest call is CL vs VSPAERO
0.233 (baseline-0004, α = 0) against 0.25.
High-AoA study (V12): **10/10 FLAGGED**, all with COEFF_NOT_CONVERGED, CL_VS_VSPAERO and CL_ALPHA_SLOPE
(20° leg: CL ≈ 0.65–0.82 oscillating vs VLM 1.33–1.65; see PHASE_REPORT.md).

## Pending decisions (human)

| # | Question | Context / options |
|---|---|---|
| P6-1 | **Human review of the 10 flagged high_aoa runs** (Phase 6 acceptance) | `aero review list high_aoa`, `aero review open <run>`, `aero review decide <run> --accept|--reject --label L --note ...`. All 10 are qualitatively the same (20° CL collapse + oscillation, likely spurious separation in the inviscid solver); the label choice (e.g. `separated_flow`, `not_converged`, `reference_wrong`) is the human's. |
| P6-2 | ~~Is Gate 3 too loose?~~ Closed (V14). | |
| P6-3 | SKEWNESS_HIGH correction sequence | Step 3 (`n_relax_iter` 5→10) is ineffective; step 2 costs +25–30 % cells for no gain. Numerical rule change (e.g. replace with `feature_level` +1 or `snap_tolerance`), to be tested in scratch first. Separately: keep the skewness limit at 4 (PLAN) or not, a threshold decision. 9/18 harsh meshes fail on it, mostly by 4.1–5.2. |
| P6-5 | ~~High-AoA study~~ Done (V12); closed (V16). | |
| P6-4 | ~~Human labelling of UNRECOVERABLE runs?~~ Accepted (V15). | |
| P6-6 | When to build the V15 UNRECOVERABLE labelling, and its labels | Proposal: `aero review label <run> --label L --note`, only for UNRECOVERABLE runs, status unchanged; extra labels e.g. `mesher_limitation`, `design_infeasible`. |
| P6-7 | Field contours for review (separation real vs numerical?) | H (2026-11-25): the 20° separation is probably an inviscid-solver artifact, but could be real (high AoA); hard to judge without contours. Proposal: OpenFOAM `surfaces` function object samples a few cutting planes (raw text) -> matplotlib contours of U/p in the report; no new dependency; images for review only (rule 4). |

### Open items for later phases (no decision yet)
- **Runner has no lock** (found by the dashboard study): two concurrent `aero run` on the same study can
  pick up the same PENDING/PILOT_OK run and run it twice. Needs a per-run (or per-study) lockfile in
  `runner.py` before any automated or UI launching; until then, run one `aero run` per study at a time.
- Dashboard (V13) is a Phase 8 deliverable: `aero summary` first, `aero serve` optional.
- With sweep narrowed to 0–10° (S3) the baseline produced **no** Gate 0–3 failures or flags; the
  Phase 8 batch / Phase 9 classifier will need a wider or deliberately harsher parameter range to
  generate real failures. Human decision when Phase 8 starts.
- Mesh resolution (M2) is unvalidated until the Phase 7 GCI study.
- SKEWNESS_HIGH correction sequence (Phase 6 harsh dry run): step 3 `n_relax_iter` 5→10 changed neither
  cell count nor skewness in most runs (a wasted attempt); step 2 `n_cells_between_levels` +1 costs
  +25–30 % cells for no skewness gain. Most failures are marginal (4.1–5.2 vs 4). Revisit in Phase 7/8.
