# Phase 4 — Adaptive manifest keyframes + Argon F-02..F-05

Follow-on to `PHASE3_RESULTS.md`. Implements the audit verdict: **Option (c),
adaptive SE(3) RDP keyframe stops** in `assembly_manifest.lua.json`, plus the
four supporting findings. Commit: `1ab136d` (code checkpoint), docs commit
follows.

## What changed

| Finding | Fix | Where |
|---|---|---|
| F-01 staggered timing / overshoot | `keyframes[{t,pos,rot}]` per hinge part; greedy RDP split at max-vertex-error frame until ≤ `keyframe_tol_m` (0.02 m) or `keyframe_max_stops` (5). `keyframe_mode`: `adaptive` (default) / `uniform` (0/.25/.5/.75/1) / `off` | `blender_worker.py` (`rdp_hinge_stops`, driver-traj capture, emission) |
| D4 step trap | Reference basis ramps `rest_rot` comp: `R @ W_rel(k) @ slerp(D_rot⁻¹ → I, t)` — t=0 ≡ `folded_rot`, t=1 ≡ `deployed_rot` bit-for-bit, continuous in between | worker emission block |
| F-02 hemisphere | Sequential chaining `dot(q[i-1],q[i])<0 → negate q[i]` — never anchor to q[0]; also applied per candidate interval inside the RDP evaluator | worker, `render_check.py`, `diagnose_trs.py` |
| F-03 PASS4 tautology | Probes = midpoint of every adjacent stop pair ∪ fixed sweep {0.125,0.375,0.625,0.875}; `[0.5]` fallback on 2-pose manifests; dedup'd through integer frames | `tools/diagnose_trs.py` |
| F-04 stale `bound_box` | All reads → `foreach_get('co')` vert min/max + 8-corner transform: `world_bbox`, snapshot-mode `data.transform(N)` site (2nd latent bug), recipe-space AABB, `scene_bounds` | worker ×3, `render_check.py` |
| F-05 open-shell penetration | Island face polys recorded at decompose → lazy `BVHTree.FromPolygons` → enclosed iff ≥2 of 3 axes hit faces in BOTH directions; depth precondition >0.02 m kept | `tools/rigid_plate_qa.py` |
| (found in verify) PASS4 measured absolute vs carrier-relative truth | True source verts now mapped through `C_k = ref0 @ refk⁻¹` per probe frame — carrier motion no longer reads as phantom part error | `tools/diagnose_trs.py` |

## Buster rebuild — emitted stops

9 hinge parts, stops clustered at the actual deploy phase (RDP placed them,
not the grid); spin parts + `_body` carry none:

```
leg_l/leg_r:   [0, 0.2217, 0.2567, 0.27, 1.0]   (cap=5 hit)
panel_l/r:     [0, 0.2217, 0.2567, 0.27, 1.0]   (cap=5 hit)
gen_l/r:       [0, 0.2533, 0.27, 1.0]
upanel_l/r, upart: [0, 0.28, 0.3, 1.0]
```

Spin (leg_f, eye, turb_l/r, blade_l/r): no `keyframes` — correct.

## PASS4 before/after (buster_drone, t-probe worst case)

| Part | Phase-3 t=0.5 | Phase-4 worst interior probe |
|---|---|---|
| leg_l / leg_r | 1.75–2.13 m | **0.246–0.248 m** (multi-member limit, below) |
| panel_l / panel_r | 1.33–1.34 m | **0.0146–0.0147 m** |
| gen_l / gen_r | ~1.25 m | **0.0099 m** |
| upanel_l/r, upart | ~1.14–1.27 m | **0.0101–0.0110 m** |

All single-member hinges: **≤0.015 m** — target (≈0.05 m) met with 3× margin.
PASS1 clean, PASS3b unchanged at 0.0000–0.0001 m (Contract A still exact).

### Leg residual: a part-map granularity limit, not interpolation error

`leg_l`/`leg_r` join 8 independently-animated segments (`Drone_leg_L_leg_0` +
`L_P1…L_P7`) into one rigid `.ydr` baked at f_start. Measured per-member
deviation from the driver's rigid track: `L_P2…L_P7` ≤ **0.38 m** — the
segments genuinely move differently mid-clip. No single rigid keyframe set
can express a shape change; the flat ~0.25 m plateau is the joined centroid's
unrepresentable remainder, not missed stops.

Fix path (no code change needed): split leg segments into their own
`part_map` entries — each gets its own driver + adaptive track. Needs
`max_parts` headroom (currently 16).

## Mech `rigid_plate_qa`

| Flag | Phase 3 | Phase 4 |
|---|---|---|
| `CROSS_LATERAL_BIND` | 0 | **0** (held) |
| `FK_PENETRATION` | 54 ("advisory contact zones") | **0** — all 54 were open-shell false positives (verts behind single-sided armor plates); verified enclosure machinery engages: interior centroid → enclosed, exterior point → not |
| `DISJOINT_KINEMATIC_SEAM` | 121 | **121** (unchanged design noise) |

## Renders + harness

- `_assembly_mid_*` now draws the piecewise path; on buster mid ≈ deployed —
  consistent with the source being ~98% deployed at t=0.5.
- Re-rendered humanoid, police, mech, `sf_drone_multi` (legacy 2-pose
  fallback path) — all `[render_check] done`.
- `python test_harness.py` → `ALL CHECKS PASSED` (incl. the synthetic
  multi-part fixture which exercised the emission path end-to-end).

## Consumer contract (unchanged elsewhere)

`runtime_status` stays `manifest_only_requires_consumer`. A runtime
interpolates `keyframes` piecewise (lerp pos + chained-hemisphere slerp rot
at local alpha between bracketing stops); absence of `keyframes` = legacy
2-pose fallback. `attach_offset`/`deployed_offset`/`folded_rot`/
`deployed_rot` are bit-identical to before.
