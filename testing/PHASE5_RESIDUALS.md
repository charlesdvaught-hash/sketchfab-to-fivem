# Phase 5 — residual audit fixes (Argon F-01..F-05)

Machine-readable report: [`PHASE5_RESIDUALS.json`](PHASE5_RESIDUALS.json).
Code checkpoint `872b08f`; verification commit follows.

## Measured (buster_drone rebuild)

| Metric | Phase 4 | Phase 5 |
|---|---|---|
| PASS4 worst probe `leg_l` | ~0.25 m plateau | **0.1015 m** @ t=0.383 |
| PASS4 worst probe `leg_r` | ~0.25 m plateau | **0.1080 m** @ t=0.383 |
| Single-member hinges | ≤0.015 m | ≤0.0146 m |
| PASS3b all parts | 0.0000–0.0001 m | **unchanged** |
| `WARN_NONRIGID_CHAIN` | — | leg_f 0.70/0.47 m (spin), leg_l 0.67/0.31 m, leg_r 0.67/0.40 m |
| PASS3c deployed per-member | n/a (new) | leg_l 0.19–0.54 m, leg_r 0.18–0.64 m — honest permanent residual |

Synthetic `rest_rot` (15° on `panel_l`): `repose` ramps 0→1.0 by **t=0.27**
(deployment-complete), PASS4 0.0000–0.0146 m — no phantom ramp error.

Mech `rigid_plate_qa`: `CROSS_LATERAL_BIND` 0, `FK_PENETRATION` 0,
`DISJOINT_KINEMATIC_SEAM` 121 (unchanged), **`FK_SCISSOR_CLIP` 41** — new
tri-tri crossings at knee/hip/foot joints the vert-enclosure check was
structurally blind to. `test_harness.py`: ALL CHECKS PASSED.

Renders: `buster_PHASE5_{folded,mid,deployed}_iso.png` — deployed mounts
identical to Phase 4 (proximal-weighted fit opened no hip gap);
deployed stance matches `REFERENCE_buster.jpg`.

## What changed

- **F-01 group fit**: `assembly_config.group_fit=procrustes` (default) fits
  a weighted Kabsch delta `T'(k)` over all member probe verts (driver ×3,
  normalized weights, det<0 fix, delta space — never composed into `W_k`
  slots). Endpoint epsilon-snap (<1e-4) keeps rigid groups bit-identical;
  fitted groups emit group best-fit `deployed_*`. `WARN_NONRIGID_CHAIN`
  splits `interior_dev`/`endpoint_dev`; endpoint_dev >0.02 m recommends a
  `part_map` split. `group_fit=driver` restores single-driver behavior.
- **F-02 scissor check**: `FK_SCISSOR_CLIP` — lazy posed `BVHTree` per
  (pose, moved island), `overlap()` tri-tri pairs vs rest islands under
  the same `rest_touch` + 0.02 m AABB prefilter. `_vert_enclosed`
  untouched — open-shell filtering preserved.
- **F-03 motion-progress repose**: `rest_rot` ramp is parameterized by
  `s(k)` = normalized pose distance, not clock t — re-pose completes with
  deployment. Manifest emits `rest_rot_deg` + per-keyframe `repose`;
  PASS4/PASS3c conjugate truth by `S @ slerp(I,D_rot,s) @ S⁻¹` (exact).
- **F-04 rigid carrier**: `_rigid_se3` strips scale/shear from carrier
  refs in the worker (W_rel, D_b_inv, spin rel_travel, fit) and
  `diagnose_trs.carrier_rel`.
- **F-05 probe/quat**: PASS4 drops probe frames on baked stop frames;
  `keyframes[].quat` [w,x,y,z] sequentially hemisphere-chained.

## Remaining

- **R-01**: leg chains still need `part_map` splits for sub-0.05 m —
  endpoint_dev is permanent in the joined .ydr. Needs `max_parts` ≥ 18.
- **R-02**: 41 scissor clips unreviewed (real joint clipping vs design
  overlap).
- **R-05**: 121 disjoint seams still unreviewed design noise.
