# Phase 3 — Argon round-3 audit: fixes + measured results

Third external-review round. Argon's report agreed with our two pushbacks
(`d_tree==1` seam warnings are joint noise; 4-decimal recipe pivots are
fine — adopt `max_vertex_error` instead of float64 pivots) and we adopted
the rest. Findings below are measured at HEAD — commits `81758f5` +
`c3b6a95`.

## The catch of the round: stale `bound_box` → +1.315 m float

Found by the *new* PASS3b instrumentation, not by a reviewer. After
`mesh.data.transform(N)` (the normalization bake), `Object.bound_box`
stays **stale** — it returns pre-transform bounds until
`bpy.context.view_layer.update()` runs. `world_bbox()` fed
`body_off` (the grounding shift) those stale bounds, so the whole buster
assembly exported **1.315 m above ground** — invisible in renders because
they have no ground plane.

Fix: `view_layer.update()` before reading world bounds after any
`data.transform`. Measured after fix: body `.ydr` bbox `min.z = 0.0`,
manifest `attach_offset` == recipe pivot exactly.

## Contract A verified: PASS3b = 0.0000 m

PASS3b measures `max_v ||(p_manifest + R_manifest @ v_canon) − v_src_world||`
at `f_start` — indexed per-vertex when source/canonical counts match,
chamfer distance fallback. On buster all four moving parts read
**0.0000 m** (both metrics): the two-frame manifest reconstruction is
exact at the folded pose.

## Mech relock: `CROSS_LATERAL_BIND` 12 → 0

`Cube.006` — the groin plate straddling the midline (verts x ≈ −0.255 →
+0.151) — was bound `SKEL_R_Thigh`, so its −x verts rode the right leg.
Relocked to `SKEL_Pelvis` (axial midline rule). New `nearest_bone` falls
back with a **rest-pose-derived** lateral sign (`SKEL_L_Thigh` /
`SKEL_R_Thigh` `head_local.x` — not hardcoded +x=L) and hard-locks any
bbox straddling `mid_x ± 0.015` to `SKEL_Pelvis`.

Post-fix audit on the shipped `.ydd`:

| flag | before | after |
|---|---|---|
| `CROSS_LATERAL_BIND` | 12 | **0** |
| `PART_BONE_MISMATCH` | — | **0** |
| `FK_PENETRATION` | 1373 | **54** (all real verts-inside-neighbour-AABB at ±25° knee/hip flex — advisory contact zones) |
| `DISJOINT_KINEMATIC_SEAM` | 180 | **121** (all 2-hop plate-joint adjacency — design noise, not defects) |

## `partmap.json` — island→part attribution

`.ydd` export welds `__part_*` vertex-group identity away, so
`rigid_plate_qa` can't name owners by `obj.name`. The worker now emits
`stream/<asset>.partmap.json` (pre-join per-part centroid/bbox/role/
`assigned_bone`) and the QA tool attributes islands by bbox containment →
nearest-centroid fallback. `PART_BONE_MISMATCH` scoped to large pure
islands — boundary slivers shared by adjacent plates no longer false-fire.

## Guard fixes adopted

- **Hemisphere slerp** in `render_check` pose(): `Euler.to_quaternion()`
  can emit antipodal pairs → long-path spin. `dot<0 → negate`.
- **`is_solo` cleared** in `resolve_clip_playback` — a soloed NLA track
  suppresses the active action.
- **Color-attr rescue** (the `UVMap 0` twin): empty `"Color 1"` shells get
  the donor layer renamed in or a `(1,1,1,1)` fill via `color_srgb`, so
  dark-in-game risk is gone. NOTE: Argon said `'Color 0'` — installed
  Sollumz 2.9's `get_color_attr_name(0)` is **1-indexed → `"Color 1"`**;
  we follow the measured contract, not the suggestion.
- **UV retarget widened**: `ShaderNodeUVMap` nodes with empty *or*
  dangling/renamed `uv_map` now retarget to `UVMap 0` (was: only
  non-empty missing names).
- **PASS4 stale-depsgraph fix**: a fetched depsgraph tracks scene state —
  `frame_set` after fetching samples the wrong frame. Now re-fetched per
  frame, and `f_mid` is the integer `(f_start+f_end)//2` with
  `t_mid = (f_mid−f_start)/(f_end−f_start)` — not hardcoded `t=0.5`.

## PASS4 measured finding — the real motion gap

Mid-pose lerp-vs-source-truth on buster's hinge parts:

| part | folded→deployed travel | mid-frame error | what the source actually does |
|---|---|---|---|
| `thruster_tilt_0` | 2.429 m | **1.7503 m** | already 98% deployed at t=0.5 (front-loaded timing) |
| `thruster_tilt_1` | 1.477 m | **2.1309 m** | overshoots ~0.67 m past deployed, then settles back |

This is **staggered sub-clip timing**, not chord-vs-arc sag — the 2-pose
manifest cannot express a part that's already finished moving while the
body is mid-deployment. Open question (documented in MOTION_SPEC.md):
**5-stop quantized keyframes (0/.25/.5/.75/1) vs `parent_part` hierarchy.**
Lean: quantized stops — cheaper than a hierarchy, fits the existing
runtime lerp.

## Full beta pass (all four test models, post-fix)

| model | result |
|---|---|
| `buster_FINAL_*` | body grounded z=0; folded rotors tuck vertical, deployed rings tilt outward — matches REFERENCE_buster |
| `humanoid_FIXED_*` | red/white armor albedo + decals, T-pose symmetric — matches REFERENCE_humanoid |
| `police_drone_*` | white body + stripes, black underside pods, red lens ring — matches REFERENCE_police |
| `mech_PED_*` | grounded, symmetric, dark-olive atlas — cross-lateral defect now fixed, not just flagged |

`test_harness.py`: **ALL CHECKS PASSED** (fixture log shows
`WARN_UV_RESCUE_EMPTY` firing — the rescue path is exercised).

## Format for external findings

Same contract as Phase 2: debug the pipeline however you like — deliverable
is **one dense `.json` report, not a reimplementation** (no apps,
workbenches, ported converters). See `PHASE2_DIAGNOSIS.md` for the schema
(`reviewer`/`pipeline_commit`/`findings[]`/`severity`/`area`/`claim`/
`evidence`/`source_lines`/`repro`/`suggested_fix`/`confidence`/
`open_questions`/`artifacts_checked`).
