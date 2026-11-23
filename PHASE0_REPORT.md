# Phase 0 report — environment spike (2026-11-23)

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

## Open issues for later phases
1. **CompGeom STL self-intersections (23)** at fuselage tessellation stations, point-symmetric
   pairs, not on the wing–body junction line. Likely slivers from CompGeom trimming. sHM coped,
   but Gate 0 (Phase 2) must decide whether this is a fail or a warning — threshold decision for you.
2. Single STL solid → single `body` patch. Separate `wing`/`fuselage` patches (Phase 3) need a
   multi-solid / tagged STL export; not investigated yet.
3. 115k cells is below the 0.3–1 M target; refinement levels to be set in Phase 3/7.
4. Half model used the full symmetric STL clipped by the y=0 box face; area ratio says that works.
   The STL/2 reference assumes a symmetric body.

Real outputs saved as parser fixtures in `tests/fixtures/phase0/`.
